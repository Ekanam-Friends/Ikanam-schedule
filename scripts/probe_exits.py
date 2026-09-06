#!/usr/bin/env python3
"""Проба выходов до кабинета РАНХиГС.

Берёт файл со ссылками vless:// vmess:// ss:// trojan:// (по одной на строку,
пустые и начинающиеся с # пропускаются), собирает конфиг sing-box, где каждому
ключу отведён свой socks-порт, поднимает его в docker и через каждый выход,
плюс через прямой выход машины, делает три запроса:

  1. геолокация адреса выхода — что кабинет увидит на входе;
  2. GET  n-api/version   — проходит ли WAF вообще;
  3. POST n-api/auth/login с заведомо неверной парой — пускает ли кабинет
     к форме входа без капчи. Настоящие учётки не нужны: ответ «неверный
     логин» в JSON и есть признак, что выход годится.

Запуск на сервере, где будет работать бот (с другой машины результат ничего не
говорит о хостинге):

    python3 scripts/probe_exits.py proxy/ru-keys.txt

Нужны docker и curl. Секреты не печатает; сгенерированный конфиг лежит в
proxy/probe.json — каталог proxy/ в .gitignore.
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

BASE_PORT = 21000
IMAGE = "ghcr.io/sagernet/sing-box:latest"
CONTAINER = "ranepa-probe"
RANEPA = "https://my.ranepa.ru/lk/n-api/"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
GEO_URLS = [
    "http://ip-api.com/json/?fields=query,country,isp,org",
    "https://ipinfo.io/json",
]


# --- Разбор ссылок ---


def _b64(s: str) -> str:
    s = s.strip()
    s += "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s.encode()).decode(errors="replace")


def _q(qs: dict[str, list[str]], key: str, default: str = "") -> str:
    return qs.get(key, [default])[0]


def _tls_block(qs: dict[str, list[str]], host: str) -> dict | None:
    security = _q(qs, "security")
    if security not in ("tls", "reality", "xtls"):
        return None
    tls: dict = {"enabled": True, "server_name": _q(qs, "sni") or _q(qs, "host") or host}
    if _q(qs, "allowInsecure") in ("1", "true"):
        tls["insecure"] = True
    if _q(qs, "fp"):
        tls["utls"] = {"enabled": True, "fingerprint": _q(qs, "fp")}
    if security == "reality" or _q(qs, "pbk"):
        tls["reality"] = {"enabled": True, "public_key": _q(qs, "pbk"), "short_id": _q(qs, "sid")}
        tls.setdefault("utls", {"enabled": True, "fingerprint": "chrome"})
    return tls


def _transport_block(net: str, path: str, host: str, service: str) -> dict | None:
    if net in ("", "tcp", "raw"):
        return None
    if net == "ws":
        t: dict = {"type": "ws", "path": path or "/"}
        if host:
            t["headers"] = {"Host": host}
        return t
    if net == "grpc":
        return {"type": "grpc", "service_name": service or path.strip("/")}
    if net in ("h2", "http"):
        t = {"type": "http", "path": path or "/"}
        if host:
            t["host"] = [host]
        return t
    if net == "httpupgrade":
        t = {"type": "httpupgrade", "path": path or "/"}
        if host:
            t["host"] = host
        return t
    raise ValueError(f"транспорт {net!r} не поддержан")


def parse_vless(url: str, tag: str) -> dict:
    u = urlsplit(url)
    qs = parse_qs(u.query)
    out: dict = {
        "type": "vless",
        "tag": tag,
        "server": u.hostname,
        "server_port": u.port,
        "uuid": unquote(u.username or ""),
    }
    if _q(qs, "flow"):
        out["flow"] = _q(qs, "flow")
    tls = _tls_block(qs, u.hostname or "")
    if tls:
        out["tls"] = tls
    tr = _transport_block(
        _q(qs, "type"), unquote(_q(qs, "path")), _q(qs, "host"), _q(qs, "serviceName")
    )
    if tr:
        out["transport"] = tr
    return out


def parse_trojan(url: str, tag: str) -> dict:
    u = urlsplit(url)
    qs = parse_qs(u.query)
    qs.setdefault("security", ["tls"])
    out: dict = {
        "type": "trojan",
        "tag": tag,
        "server": u.hostname,
        "server_port": u.port,
        "password": unquote(u.username or ""),
    }
    out["tls"] = _tls_block(qs, u.hostname or "")
    tr = _transport_block(
        _q(qs, "type"), unquote(_q(qs, "path")), _q(qs, "host"), _q(qs, "serviceName")
    )
    if tr:
        out["transport"] = tr
    return out


def parse_vmess(url: str, tag: str) -> dict:
    cfg = json.loads(_b64(url[len("vmess://") :]))
    out: dict = {
        "type": "vmess",
        "tag": tag,
        "server": cfg["add"],
        "server_port": int(cfg["port"]),
        "uuid": cfg["id"],
        "security": cfg.get("scy") or "auto",
        "alter_id": int(cfg.get("aid") or 0),
    }
    if cfg.get("tls") in ("tls", "reality"):
        out["tls"] = {
            "enabled": True,
            "server_name": cfg.get("sni") or cfg.get("host") or cfg["add"],
        }
        if cfg.get("fp"):
            out["tls"]["utls"] = {"enabled": True, "fingerprint": cfg["fp"]}
    tr = _transport_block(
        cfg.get("net", "tcp"), cfg.get("path", ""), cfg.get("host", ""), cfg.get("path", "")
    )
    if tr:
        out["transport"] = tr
    return out


def parse_ss(url: str, tag: str) -> dict:
    body = url[len("ss://") :].split("#", 1)[0]
    if "@" not in body:
        body = _b64(body)  # legacy: всё целиком в base64
    userinfo, _, hostport = body.rpartition("@")
    hostport = hostport.split("/", 1)[0].split("?", 1)[0]
    if ":" not in userinfo:
        userinfo = _b64(unquote(userinfo))
    method, _, password = userinfo.partition(":")
    host, _, port = hostport.rpartition(":")
    return {
        "type": "shadowsocks",
        "tag": tag,
        "server": host.strip("[]"),
        "server_port": int(port),
        "method": method,
        "password": unquote(password),
    }


def parse_link(line: str, idx: int) -> dict:
    scheme = line.split("://", 1)[0].lower()
    name = unquote(line.rsplit("#", 1)[1]) if "#" in line and scheme != "vmess" else ""
    if scheme == "vmess":
        try:
            name = json.loads(_b64(line[len("vmess://") :])).get("ps", "")
        except Exception:
            name = ""
    tag = f"{idx:02d}-{scheme}" + (f"-{name}" if name else "")
    parsers = {"vless": parse_vless, "vmess": parse_vmess, "ss": parse_ss, "trojan": parse_trojan}
    if scheme not in parsers:
        raise ValueError(f"схема {scheme!r} не поддержана")
    return parsers[scheme](line, tag)


# --- Конфиг sing-box: по inbound на каждый outbound ---


def build_config(outbounds: list[dict]) -> dict:
    all_out = [{"type": "direct", "tag": "direct"}] + outbounds
    inbounds, rules = [], []
    for i, ob in enumerate(all_out):
        inbounds.append(
            {"type": "mixed", "tag": f"in-{i}", "listen": "127.0.0.1", "listen_port": BASE_PORT + i}
        )
        rules.append({"inbound": [f"in-{i}"], "outbound": ob["tag"]})
    return {
        "log": {"level": "warn"},
        "inbounds": inbounds,
        "outbounds": all_out,
        "route": {"rules": rules, "final": "direct"},
    }


# --- Проба ---


def curl(proxy_port: int, url: str, *args: str, timeout: int = 25) -> tuple[str, str]:
    """Возвращает (код HTTP, тело до 300 символов). Код 000 — соединение не удалось."""
    cmd = [
        "curl",
        "-sS",
        "--max-time",
        str(timeout),
        "--proxy",
        f"socks5h://127.0.0.1:{proxy_port}",
        "-o",
        "-",
        "-w",
        "\n__CODE__%{http_code}",
        *args,
        url,
    ]
    p = subprocess.run(cmd, capture_output=True, text=True)
    out = p.stdout
    if "__CODE__" in out:
        body, code = out.rsplit("__CODE__", 1)
    else:
        body, code = (p.stderr or out), "000"
    return code.strip(), " ".join(body.split())[:300]


def probe(port: int) -> dict:
    r: dict = {}
    for g in GEO_URLS:
        code, body = curl(port, g, "-H", f"User-Agent: {UA}", timeout=15)
        if code == "200":
            try:
                j = json.loads(body)
                r["ip"] = j.get("query") or j.get("ip")
                r["country"] = j.get("country")
                r["isp"] = j.get("isp") or j.get("org")
            except json.JSONDecodeError:
                pass
            break
    hdr = ["-H", f"User-Agent: {UA}", "-H", "version: 1.0", "-H", "Accept: application/json"]
    r["version"] = curl(port, RANEPA + "version", *hdr)
    r["login"] = curl(
        port,
        RANEPA + "auth/login",
        *hdr,
        "-F",
        "login=probe-nonexistent@example.invalid",
        "-F",
        "password=probe",
        "-F",
        "remember_me=true",
    )
    return r


def verdict(r: dict) -> str:
    vcode, _ = r["version"]
    lcode, lbody = r["login"]
    if vcode == "000" or lcode == "000":
        return "НЕТ СВЯЗИ"
    if "captcha" in lbody.lower() or "капч" in lbody.lower():
        return "КАПЧА"
    if lcode == "403" and "{" not in lbody:
        return "WAF 403"
    if lcode in ("401", "422", "400") and "{" in lbody:
        return "ОК: пускает к форме входа"
    return f"НЕЯСНО ({lcode})"


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    keys = Path(sys.argv[1])
    lines = [ln.strip() for ln in keys.read_text().splitlines()]
    lines = [ln for ln in lines if ln and not ln.startswith("#")]

    outbounds = []
    for i, ln in enumerate(lines, 1):
        try:
            outbounds.append(parse_link(ln, i))
        except Exception as e:  # noqa: BLE001
            print(f"[{i:02d}] пропущен: {e}")
    cfg = build_config(outbounds)
    cfg_path = Path("proxy") / "probe.json"
    cfg_path.parent.mkdir(exist_ok=True)
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))

    subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)
    run = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            CONTAINER,
            "--network",
            "host",
            "-v",
            f"{cfg_path.resolve()}:/cfg.json:ro",
            IMAGE,
            "run",
            "-c",
            "/cfg.json",
        ],
        capture_output=True,
        text=True,
    )
    if run.returncode != 0:
        print("docker run не удался:", run.stderr.strip())
        return 1
    try:
        time.sleep(3)
        logs = subprocess.run(["docker", "logs", CONTAINER], capture_output=True, text=True)
        if "FATAL" in logs.stderr or "FATAL" in logs.stdout:
            print("sing-box не стартовал:\n", logs.stderr or logs.stdout)
            return 1

        print(f"{'выход':34} {'страна':10} {'ISP':28} ver login вердикт")
        print("-" * 110)
        for i, ob in enumerate(cfg["outbounds"]):
            r = probe(BASE_PORT + i)
            print(
                f"{ob['tag'][:34]:34} {str(r.get('country'))[:10]:10} {str(r.get('isp'))[:28]:28} "
                f"{r['version'][0]:>3} {r['login'][0]:>5} {verdict(r)}"
            )
            print(f"{'':34} ip={r.get('ip')}  login-body: {r['login'][1][:160]}")
    finally:
        subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

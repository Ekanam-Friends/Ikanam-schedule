"""Захват QR из вебинара MTS Link через headless Chromium.

Цель — дистанционная отметка: QR преподаватель показывает демонстрацией экрана
внутри вебинара MTS Link, поэтому единственный способ его добыть — сидеть в
вебинаре браузером и снимать кадры. Персональную ссылку на живой вебинар даёт
`LmsClient.find_active_webinar`; сюда приходит уже она.

Проверено вживую 2026-09-28 (Микроэкономика, лекция 4):

* по персональной ссылке МТС Линк просит пароль своей учётки или SSO — пароли
  мы не вводим, поэтому входим **гостем** под именем пользователя;
* `chromium-headless-shell` демонстрацию отрисовывает (video 1722×1080);
* нагрузка на 1 vCPU — ~47% ядра, почти всё — декод 7 видеопотоков (демонстрация
  + 6 камер участников) и программная отрисовка страницы. Поэтому здесь камеры
  участников выключаются, кадр берётся прямо из элемента демонстрации, а не
  скриншотом всей страницы, и снимается раз в 10 с (QR живёт 40–60 с).

Запуск на сервере (проба и слежение):
    docker compose exec bot python -m app.ranepa.webinar_probe probe "<url>"
    docker compose exec bot python -m app.ranepa.webinar_probe watch "<url>" "Фамилия Имя"
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import re
import sys
import time
from dataclasses import dataclass, field

from app.ranepa.lms import extract_qr_hash

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# К флагам песочницы (как в challenge.py) добавлены медиа-флаги: без жеста
# разрешить автозапуск и не спрашивать разрешения на медиа.
BROWSER_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--autoplay-policy=no-user-gesture-required",
    "--use-fake-ui-for-media-stream",
]

# Окно поменьше — дешевле отрисовка. Кадр берём из самого видео в его родном
# разрешении, поэтому размер окна на читаемость QR не влияет.
VIEWPORT = {"width": 1280, "height": 720}

# Кнопки лобби по порядку: «Подключиться» → модалка → «Войти как гость» → имя →
# «Присоединиться». Дальше бывают подсказки, их закрываем.
_LOBBY_BUTTONS = ("гость", "Присоединиться", "Понятно", "Пропустить", "Подключиться")

# Найти элемент демонстрации — самый большой видимый video/canvas, — остальные
# видео выключить (дорожки off, пауза, скрыть), а кадр демонстрации отдать PNG в
# base64. Если элемент — canvas WebGL без сохранения буфера, он читается пустым:
# тогда возвращаем его прямоугольник, и Python снимет скриншот только этой области.
_GRAB_JS = """
() => {
  const px = el => el.tagName === 'VIDEO' ? el.videoWidth * el.videoHeight : el.width * el.height;
  const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  // Демонстрация — поток с наибольшим разрешением. Смотрим и на скрытые нами
  // video: плеер может переиспользовать элемент под демонстрацию.
  const vids = [...document.querySelectorAll('video')].filter(v => v.videoWidth > 0);
  const cans = [...document.querySelectorAll('canvas')].filter(vis);
  const els = [...vids, ...cans].sort((a, b) => px(b) - px(a));
  if (!els.length) return {kind: 'none'};
  const demo = els[0];
  demo.style.display = '';
  if (demo.tagName === 'VIDEO' && demo.paused) demo.play().catch(() => {});
  let muted = 0;
  // Гасим прочие видео, только когда демонстрация явно крупнее камер (≥ 960 px).
  if ((demo.videoWidth || demo.width) >= 960) {
    for (const v of document.querySelectorAll('video')) {
      if (v === demo) continue;
      const s = v.srcObject;
      if (s && s.getVideoTracks) {
        s.getVideoTracks().forEach(t => { if (t.enabled) { t.enabled = false; muted++; } });
      }
      if (!v.paused) v.pause();
      v.style.display = 'none';
    }
  }
  const r = demo.getBoundingClientRect();
  const rect = {x: r.x, y: r.y, width: r.width, height: r.height};
  let w, h, png = null;
  try {
    w = demo.videoWidth || demo.width; h = demo.videoHeight || demo.height;
    if (w && h) {
      const c = document.createElement('canvas'); c.width = w; c.height = h;
      c.getContext('2d').drawImage(demo, 0, 0, w, h);
      png = c.toDataURL('image/png').split(',')[1];
    }
  } catch (e) {}
  return {kind: demo.tagName.toLowerCase(), w, h, rect, png, muted};
}
"""


@dataclass(slots=True)
class ProbeReport:
    final_url: str
    title: str
    videos: int
    canvases: int
    iframes: list[str]
    inputs: list[str]
    buttons: list[str]
    console_errors: list[str] = field(default_factory=list)
    screenshot: str | None = None

    def pretty(self) -> str:
        lines = [
            f"Итоговый URL : {self.final_url}",
            f"Заголовок    : {self.title}",
            f"video/canvas : {self.videos} / {self.canvases}",
            f"iframes      : {self.iframes or '—'}",
            f"поля ввода   : {self.inputs or '—'}",
            f"кнопки       : {self.buttons or '—'}",
            f"скриншот     : {self.screenshot or '—'}",
        ]
        if self.console_errors:
            lines.append("ошибки консоли:")
            lines += [f"  · {e}" for e in self.console_errors[:10]]
        return "\n".join(lines)


def _playwright():
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "Playwright не установлен: pip install '.[browser]' && playwright install chromium"
        ) from exc
    return async_playwright()


async def _new_page(pw):
    browser = await pw.chromium.launch(headless=True, args=BROWSER_ARGS)
    context = await browser.new_context(
        user_agent=USER_AGENT, locale="ru-RU", viewport=VIEWPORT
    )
    return browser, await context.new_page()


async def probe(
    url: str, *, screenshot: str = "/tmp/webinar.png", settle: float = 8.0
) -> ProbeReport:
    """Открыть ссылку, снять кадр и собрать, что на странице. Ничего не нажимает."""
    errors: list[str] = []
    async with _playwright() as pw:
        browser, page = await _new_page(pw)
        try:
            page.on(
                "console",
                lambda msg: errors.append(msg.text) if msg.type == "error" else None,
            )
            await page.goto(url, wait_until="load", timeout=60_000)
            await asyncio.sleep(settle)
            iframes = await page.eval_on_selector_all(
                "iframe", "els => els.map(e => e.src).filter(Boolean)"
            )
            inputs = await page.eval_on_selector_all(
                "input", "els => els.map(e => e.placeholder || e.name || e.type).filter(Boolean)"
            )
            buttons = await page.eval_on_selector_all(
                "button, a[role=button]",
                "els => els.map(e => (e.innerText||'').trim()).filter(Boolean).slice(0,20)",
            )
            await page.screenshot(path=screenshot, full_page=False)
            return ProbeReport(
                final_url=page.url,
                title=await page.title(),
                videos=await page.locator("video").count(),
                canvases=await page.locator("canvas").count(),
                iframes=list(iframes)[:10],
                inputs=list(inputs)[:10],
                buttons=list(buttons)[:20],
                console_errors=errors,
                screenshot=screenshot,
            )
        finally:
            await browser.close()


async def join_as_guest(page, url: str, name: str, *, steps: int = 8) -> bool:
    """Пройти лобби МТС Линк гостем. True — если оказались в эфире (`/stream`)."""
    await page.goto(url, wait_until="load", timeout=90_000)
    await asyncio.sleep(5)
    for _ in range(steps):
        if "/stream" in page.url:
            return True
        name_input = page.locator("input[type=text]:visible")
        if await name_input.count() and not await name_input.first.input_value():
            await name_input.first.fill(name)
        for label in _LOBBY_BUTTONS:
            button = page.locator("button:visible", has_text=re.compile(label, re.I))
            if await button.count():
                await button.first.click(timeout=5_000)
                break
        await asyncio.sleep(6)
    return "/stream" in page.url


async def grab_demo_frame(page) -> tuple[bytes | None, dict]:
    """PNG кадра демонстрации и сведения о нём (заодно гасит камеры участников)."""
    info = await page.evaluate(_GRAB_JS)
    png = info.pop("png", None)
    if png:
        data = base64.b64decode(png)
        # Пустой кадр (WebGL-canvas без буфера) весит сотни байт — тогда снимаем
        # скриншот одной области демонстрации.
        if len(data) > 5_000:
            return data, info
    rect = info.get("rect")
    if rect and rect["width"] > 0:
        info["kind"] += "+clip"
        return await page.screenshot(clip=rect), info
    return None, info


async def watch(
    url: str,
    name: str,
    *,
    minutes: float = 90.0,
    interval: float = 10.0,
    keep_frame: str | None = "/tmp/webinar_qr.png",
    last_frame: str | None = None,
) -> str | None:
    """Войти гостем и раз в `interval` с искать QR в кадре демонстрации.

    Возвращает хеш при первой находке, иначе None по истечении `minutes` или
    если вебинар закончился. Отметку не делает — хеш отдаётся наверх
    (`LmsClient.mark`). Кадр с QR сохраняется в `keep_frame` для отчёта.
    """
    deadline = time.monotonic() + minutes * 60
    async with _playwright() as pw:
        browser, page = await _new_page(pw)
        try:
            if not await join_as_guest(page, url, name):
                log.error("Не удалось войти в вебинар гостем: %s", page.url)
                return None
            log.info("В эфире: %s", page.url)
            await _snapshot(page, "/tmp/webinar_inside.png")
            frames = 0
            while time.monotonic() < deadline:
                if "/stream" not in page.url:
                    # 2026-09-28 страница ушла с эфира за секунды до/после показа
                    # QR, и слежение умерло. Теперь: снять, куда увело, и зайти
                    # заново — сдаёмся, только если обратно не пускает.
                    log.warning("Выкинуло из эфира: %s", page.url)
                    await _snapshot(page, "/tmp/webinar_left.png")
                    if not await join_as_guest(page, url, name):
                        log.info("Обратно не пускает — вебинар закончился")
                        return None
                    continue
                # Подсказки МТС Линк («Поддерживайте ведущего… ПОНЯТНО») всплывают
                # посреди эфира и закрывают низ слайда — там может быть QR.
                await _dismiss_tips(page)
                try:
                    data, info = await grab_demo_frame(page)
                except Exception as exc:  # noqa: BLE001 — навигация посреди evaluate
                    log.warning("Кадр не снят: %s", str(exc)[:120])
                    await asyncio.sleep(1)
                    continue
                frames += 1
                if frames % 30 == 1:
                    log.info("кадр %d: %s", frames, info)
                if data and last_frame:
                    with open(last_frame, "wb") as fh:
                        fh.write(data)
                found = decode_qr(data) if data else None
                if not found:
                    # Слайды бывают не демонстрацией, а загруженной в МТС Линк
                    # презентацией (картинка, не video) — 2026-09-28 на макро.
                    # Поэтому ещё и снимок окна: 1280×720 декодируется быстро.
                    try:
                        shot = await page.screenshot()
                    except Exception:  # noqa: BLE001
                        shot = None
                    found = decode_qr(shot) if shot else None
                    if shot and last_frame:
                        with open(last_frame, "wb") as fh:
                            fh.write(shot)
                    if found:
                        data = shot
                if found:
                    log.info("QR пойман на кадре %d", frames)
                    if keep_frame:
                        with open(keep_frame, "wb") as fh:
                            fh.write(data)
                    return found
                await asyncio.sleep(interval)
        finally:
            await browser.close()
    return None


async def _dismiss_tips(page) -> None:
    """Закрыть всплывающие подсказки эфира, не роняя слежение."""
    try:
        tip = page.locator("button:visible", has_text=re.compile(r"^\s*Понятно\s*$", re.I))
        if await tip.count():
            await tip.first.click(timeout=2_000)
    except Exception:  # noqa: BLE001
        pass


async def _snapshot(page, path: str) -> None:
    """Скриншот для разбора, не роняя слежение, если страница в переходе."""
    try:
        await page.screenshot(path=path)
    except Exception:  # noqa: BLE001
        pass


def decode_qr(png: bytes) -> str | None:
    """Найти хеш отметки в PNG-кадре: pyzbar (как в боте), иначе zxing-cpp."""
    try:
        from PIL import Image, ImageOps
    except ImportError:  # pragma: no cover
        log.error("Нет Pillow — QR не декодировать")
        return None
    try:
        base = Image.open(io.BytesIO(png))
        base.load()
    except Exception:  # noqa: BLE001
        return None
    gray = ImageOps.grayscale(base)
    texts: list[str] = []
    try:
        from pyzbar.pyzbar import decode as zbar_decode

        for variant in (gray, ImageOps.autocontrast(gray)):
            texts += [f.data.decode("utf-8", "ignore") for f in zbar_decode(variant)]
    except ImportError:
        try:
            import zxingcpp

            texts += [r.text for r in zxingcpp.read_barcodes(gray)]
        except ImportError:
            log.error("Нет ни pyzbar, ни zxing-cpp — QR не декодировать")
    for text in texts:
        candidate = extract_qr_hash(text)
        if candidate:
            return candidate
    return None


def _main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if len(argv) < 2 or argv[0] not in {"probe", "watch"}:
        print(
            "usage: python -m app.ranepa.webinar_probe probe <url> | watch <url> <имя>",
            file=sys.stderr,
        )
        return 2
    command, url = argv[0], argv[1]
    if command == "probe":
        print(asyncio.run(probe(url)).pretty())
    else:
        name = argv[2] if len(argv) > 2 else "Гость"
        found = asyncio.run(watch(url, name))
        print("QR-хеш:", found or "не пойман")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))

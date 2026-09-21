"""Разведка и захват вебинара MTS Link через headless Chromium.

Цель — дистанционная отметка: QR преподаватель показывает демонстрацией экрана
внутри вебинара MTS Link, поэтому единственный способ его добыть — открыть
вебинар браузером и снимать кадры. Ссылку на живой вебинар даёт
`LmsClient.find_active_webinar`; сюда приходит уже она.

**Это ещё не доказанная фича, а harness для проверки.** Главный неизвестный —
потянет ли `chromium-headless-shell` из образа WebRTC-видео вебинара: он собран
без полного медиастека, и демонстрация экрана может просто не отрисоваться, а на
сервере всего 1 ГБ RAM. Поэтому порядок такой:

1. `probe` — открыть ссылку, снять один кадр и рассказать, что на странице
   (куда увёл MTS, есть ли форма входа/имени, есть ли video/iframe, что в
   консоли). Отвечает на «пускает ли и видно ли демонстрацию вообще».
2. `watch` — только если `probe` показал живую картинку: снимать кадры раз в
   N секунд, искать в них QR, при находке вернуть хеш (дальше отметка — через
   `LmsClient.mark`).

Запуск на сервере:
    docker compose exec bot python -m app.ranepa.webinar_probe probe "<mts-url>"
"""

from __future__ import annotations

import asyncio
import logging
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
# разрешить автозапуск и не спрашивать разрешения на медиа. Кодеки они не
# добавляют — если видео не отрисуется, дело в сборке headless-shell, и это
# ровно то, что проверяем.
BROWSER_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--autoplay-policy=no-user-gesture-required",
    "--use-fake-ui-for-media-stream",
]


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


async def _new_browser(playwright):
    return await playwright.chromium.launch(headless=True, args=BROWSER_ARGS)


async def probe(
    url: str, *, screenshot: str = "/tmp/webinar.png", settle: float = 8.0
) -> ProbeReport:
    """Открыть вебинар, снять кадр и собрать, что на странице.

    Отвечает на завтрашний вопрос: пускает ли headless внутрь и видно ли
    демонстрацию экрана. Ничего не отмечает.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "Playwright не установлен: pip install '.[browser]' && playwright install chromium"
        ) from exc

    errors: list[str] = []
    async with async_playwright() as pw:
        browser = await _new_browser(pw)
        try:
            context = await browser.new_context(user_agent=USER_AGENT, locale="ru-RU")
            page = await context.new_page()
            page.on(
                "console",
                lambda msg: errors.append(msg.text) if msg.type == "error" else None,
            )
            await page.goto(url, wait_until="load", timeout=60_000)
            # Дать плееру время подняться и запросить видео.
            await asyncio.sleep(settle)
            title = await page.title()
            videos = await page.locator("video").count()
            canvases = await page.locator("canvas").count()
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
                title=title,
                videos=videos,
                canvases=canvases,
                iframes=list(iframes)[:10],
                inputs=list(inputs)[:10],
                buttons=list(buttons)[:20],
                console_errors=errors,
                screenshot=screenshot,
            )
        finally:
            await browser.close()


async def watch(
    url: str,
    *,
    minutes: float = 90.0,
    interval: float = 10.0,
    shots_dir: str = "/tmp/webinar_shots",
) -> str | None:
    """Снимать кадры вебинара раз в `interval` c и искать в них QR.

    Возвращает хеш при первой находке, иначе None по истечении `minutes`. Кадры
    без QR удаляются сразу — на диске не копится. Отметку не делает: хеш
    отдаётся наверх, где его откроет `LmsClient.mark`.

    Запускать только после удачного `probe`: если демонстрация не отрисовалась,
    декодировать нечего.
    """
    import os

    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("Playwright не установлен") from exc

    os.makedirs(shots_dir, exist_ok=True)
    deadline = time.monotonic() + minutes * 60
    async with async_playwright() as pw:
        browser = await _new_browser(pw)
        try:
            context = await browser.new_context(user_agent=USER_AGENT, locale="ru-RU")
            page = await context.new_page()
            await page.goto(url, wait_until="load", timeout=60_000)
            n = 0
            while time.monotonic() < deadline:
                shot = os.path.join(shots_dir, f"f{n}.png")
                try:
                    await page.screenshot(path=shot, full_page=False)
                    found = _decode_png(shot)
                finally:
                    n += 1
                if found:
                    log.info("QR пойман на кадре %d: %s", n, found)
                    return found
                _remove_quietly(shot)  # мусор без QR — сразу
                await asyncio.sleep(interval)
        finally:
            await browser.close()
    return None


def _decode_png(path: str) -> str | None:
    """Найти хеш отметки в PNG-кадре (тем же pyzbar, что и в боте)."""
    try:
        from PIL import Image, ImageOps
        from pyzbar.pyzbar import decode as zbar_decode
    except Exception as exc:  # noqa: BLE001
        log.error("Декодер QR недоступен: %s", exc)
        return None
    try:
        base = Image.open(path)
    except Exception:  # noqa: BLE001
        return None
    gray = ImageOps.grayscale(base)
    for variant in (base, gray, ImageOps.autocontrast(gray)):
        for found in zbar_decode(variant):
            candidate = extract_qr_hash(found.data.decode("utf-8", "ignore"))
            if candidate:
                return candidate
    return None


def _remove_quietly(path: str) -> None:
    import os

    try:
        os.remove(path)
    except OSError:
        pass


def _main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if len(argv) < 2 or argv[0] not in {"probe", "watch"}:
        print("usage: python -m app.ranepa.webinar_probe {probe|watch} <mts-url>", file=sys.stderr)
        return 2
    command, url = argv[0], argv[1]
    if command == "probe":
        report = asyncio.run(probe(url))
        print(report.pretty())
    else:
        found = asyncio.run(watch(url))
        print("QR-хеш:", found or "не пойман")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))

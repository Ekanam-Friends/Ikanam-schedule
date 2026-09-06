"""Прохождение JS-проверки антибота кабинета настоящим браузером.

С адресов дата-центров кабинет отдаёт вместо API страницу со скриптом
`get_jhash`: тот считает хеш, ставит cookie `__jhash_` и `__jua_` и
перезагружает страницу — и только со второго захода приходит JSON. Скрипт
исполняется в браузере, поэтому здесь и запускается браузер: headless Chromium
через Playwright открывает кабинет, честно выполняет его же проверку и отдаёт
полученные cookie. Дальше с ними ходит обычный httpx-клиент.

Почему не посчитать хеш самим: это значило бы подделать проверку, выдавая
скрипт за браузер. Настоящий браузер ничего не подделывает — он и есть то,
для чего проверка написана.

Что важно знать о cookie:

* `__jua_` привязана к User-Agent — браузер и httpx обязаны представляться
  одинаково, иначе проверка возвращается;
* `__jhash_` живёт около 30 минут — ночная синхронизация нескольких десятков
  человек в это окно не укладывается, поэтому решатель умеет перерешивать;
* проверка одна на адрес, а не на пользователя — решатель общий на процесс,
  и параллельные синхронизации ждут одного решения, а не запускают по Chromium.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

log = logging.getLogger(__name__)

PAGE_PATH = "/lk/"
"""Что открывать в браузере: страница кабинета, а не эндпоинт API. Проверка
одна на весь сайт, а страницу браузер умеет перезагружать сам."""

COOKIE_PREFIX = "__"
"""Cookie проверки начинаются с двух подчёркиваний: `__js_p_`, `__jhash_`,
`__jua_`, `__hash_`, `__lhash_`. Остальное на странице — метрика, её не несём."""

PASS_MARKER = "__jhash_"
"""Появление этой cookie означает, что скрипт отработал."""

DEFAULT_TTL = 25 * 60.0
"""Срок cookie, если кабинет не назвал свой: чуть меньше наблюдаемых 30 минут."""

SAFETY_MARGIN = 60.0
"""За сколько секунд до истечения считать cookie уже негодными."""

MIN_LIFETIME = 30.0
"""Нижняя граница срока cookie в кэше: даже если кабинет назвал срок меньше
запаса безопасности, браузер не должен подниматься на каждый запрос."""

CONFIRM_TIMEOUT = 25.0
SETTLE_SECONDS = 1.5

BROWSER_ARGS = [
    # В контейнере нет user namespaces для песочницы Chromium, а /dev/shm
    # там 64 МБ — без этих флагов браузер либо не стартует, либо падает.
    "--no-sandbox",
    "--disable-dev-shm-usage",
]


class ChallengeSolveError(Exception):
    """Браузер открылся, но проверка не прошла: cookie так и не появились."""


class CookieSource(Protocol):
    """То, что нужно клиенту кабинета: отдать cookie, а если кабинет отверг
    выданные — свежие.

    `rejected` — снимок, который кабинет не принял. Решатель сравнивает его с
    тем, что у него на руках: если это тот же снимок, решает заново; если уже
    другой (перерешал кто-то параллельно), отдаёт его без нового браузера.
    """

    async def cookies(self, *, rejected: dict[str, str] | None = None) -> dict[str, str]: ...


@dataclass(slots=True)
class _Solved:
    cookies: dict[str, str]
    solved_at: float
    valid_until: float


class ChallengeSolver:
    """Общий на процесс решатель: кэширует cookie и не запускает браузер зря."""

    def __init__(
        self,
        *,
        base_url: str,
        user_agent: str,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._user_agent = user_agent
        self._clock = clock
        self._lock = asyncio.Lock()
        self._solved: _Solved | None = None

    async def cookies(self, *, rejected: dict[str, str] | None = None) -> dict[str, str]:
        async with self._lock:
            cached = self._solved
            if cached is not None:
                fresh = self._clock() < cached.valid_until
                # Отвергнут именно наш текущий снимок — значит, он негоден,
                # сколько бы ни было ему секунд: кабинет иногда бракует cookie
                # через пару секунд после выдачи. Отвергнут какой-то другой —
                # его уже заменили, и повторно решать незачем.
                superseded = rejected is not None and rejected != cached.cookies
                if (fresh and rejected is None) or superseded:
                    return dict(cached.cookies)
            cookies, ttl = await self._solve()
            solved_at = self._clock()
            self._solved = _Solved(
                cookies=cookies,
                solved_at=solved_at,
                valid_until=solved_at + max(ttl - SAFETY_MARGIN, MIN_LIFETIME),
            )
            log.info("Проверка кабинета пройдена, cookie действуют ещё %.0f мин", ttl / 60)
            return dict(cookies)

    async def _solve(self) -> tuple[dict[str, str], float]:
        """Открыть кабинет в Chromium и дождаться cookie проверки."""
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover — зависит от окружения
            raise ChallengeSolveError(
                "Playwright не установлен: pip install '.[browser]' && playwright install chromium"
            ) from exc

        started = time.monotonic()
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True, args=BROWSER_ARGS)
            try:
                context = await browser.new_context(user_agent=self._user_agent, locale="ru-RU")
                page = await context.new_page()
                await page.goto(self._base_url + PAGE_PATH, wait_until="load", timeout=60_000)
                raw = await _wait_for_pass(context, self._base_url)
            finally:
                await browser.close()
        log.debug("Chromium прошёл проверку за %.1f с", time.monotonic() - started)
        return pick_challenge_cookies(raw)


async def _wait_for_pass(context: Any, base_url: str) -> list[dict[str, Any]]:
    """Ждать, пока скрипт проверки поставит cookie и страница перезагрузится."""
    deadline = time.monotonic() + CONFIRM_TIMEOUT
    while True:
        raw = await context.cookies(base_url)
        if any(cookie["name"] == PASS_MARKER for cookie in raw):
            break
        if time.monotonic() > deadline:
            names = sorted(cookie["name"] for cookie in raw)
            raise ChallengeSolveError(
                f"cookie {PASS_MARKER} не появилась за {CONFIRM_TIMEOUT:.0f} с, есть: {names}"
            )
        await asyncio.sleep(0.5)
    # Скрипт ставит cookie и тут же перезагружает страницу; серверные cookie
    # второго захода (`__hash_`, `__lhash_`) приходят уже после — даём им время.
    await asyncio.sleep(SETTLE_SECONDS)
    return await context.cookies(base_url)


def pick_challenge_cookies(
    raw: list[dict[str, Any]], *, now: float | None = None
) -> tuple[dict[str, str], float]:
    """Из cookie браузера оставить только cookie проверки и посчитать их срок.

    Срок — самый короткий из названных кабинетом: истечение любой из них
    возвращает проверку. Сессионные (без срока) на расчёт не влияют.
    """
    now = time.time() if now is None else now
    picked: dict[str, str] = {}
    ttl: float | None = None
    for cookie in raw:
        if not cookie["name"].startswith(COOKIE_PREFIX):
            continue
        picked[cookie["name"]] = cookie["value"]
        expires = cookie.get("expires") or -1
        if expires > 0:
            ttl = expires - now if ttl is None else min(ttl, expires - now)
    if PASS_MARKER not in picked:
        raise ChallengeSolveError(f"среди cookie нет {PASS_MARKER}: {sorted(picked)}")
    return picked, DEFAULT_TTL if ttl is None else ttl

"""Решатель JS-проверки кабинета: кэш, срок cookie, защита от лишних запусков.

Сам Chromium здесь не поднимается — проверяется логика вокруг него: когда
решать заново, а когда отдать то, что уже есть. Это ровно то, что дорого
ошибиться: лишний браузер на каждую синхронизацию — минуты и гигабайты.
"""

from __future__ import annotations

import pytest

from app.ranepa.challenge import (
    DEFAULT_TTL,
    REFRESH_DEBOUNCE,
    SAFETY_MARGIN,
    ChallengeSolveError,
    ChallengeSolver,
    pick_challenge_cookies,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class StubSolver(ChallengeSolver):
    """Вместо браузера — счётчик запусков и заранее заданный ответ."""

    def __init__(self, clock: FakeClock, *, ttl: float = 1800.0) -> None:
        super().__init__(base_url="https://my.ranepa.ru", user_agent="ua", clock=clock)
        self.launches = 0
        self.ttl = ttl

    async def _solve(self) -> tuple[dict[str, str], float]:
        self.launches += 1
        return {"__jhash_": f"h{self.launches}", "__jua_": "ua"}, self.ttl


async def test_cookies_are_solved_once_and_served_from_cache():
    clock = FakeClock()
    solver = StubSolver(clock)

    first = await solver.cookies()
    clock.now += 600
    second = await solver.cookies()

    assert first == second == {"__jhash_": "h1", "__jua_": "ua"}
    assert solver.launches == 1


async def test_cookies_are_resolved_after_expiry():
    clock = FakeClock()
    solver = StubSolver(clock, ttl=1800.0)

    await solver.cookies()
    clock.now += 1800.0 - SAFETY_MARGIN + 1
    renewed = await solver.cookies()

    assert renewed["__jhash_"] == "h2"
    assert solver.launches == 2


async def test_refresh_forces_a_new_solve_when_cookies_are_not_brand_new():
    clock = FakeClock()
    solver = StubSolver(clock)

    await solver.cookies()
    clock.now += REFRESH_DEBOUNCE + 1
    renewed = await solver.cookies(refresh=True)

    assert renewed["__jhash_"] == "h2"
    assert solver.launches == 2


async def test_refresh_right_after_solve_does_not_launch_again():
    """Четыре синхронизации разом поймали истёкшие cookie и просят «перерешай»:
    первая запускает браузер, остальные получают её результат."""
    clock = FakeClock()
    solver = StubSolver(clock)

    await solver.cookies()
    clock.now += 5
    for _ in range(3):
        again = await solver.cookies(refresh=True)
        assert again["__jhash_"] == "h1"

    assert solver.launches == 1


async def test_short_ttl_still_keeps_cookies_for_debounce_window():
    """Кабинет назвал срок меньше запаса безопасности — не превращаем это в
    браузер на каждый запрос."""
    clock = FakeClock()
    solver = StubSolver(clock, ttl=10.0)

    await solver.cookies()
    clock.now += REFRESH_DEBOUNCE - 1
    await solver.cookies()

    assert solver.launches == 1


# --- pick_challenge_cookies: что брать из браузера и на сколько ---


def browser_cookies(*items: tuple[str, str, float]) -> list[dict]:
    return [{"name": n, "value": v, "expires": e} for n, v, e in items]


def test_only_challenge_cookies_are_picked_and_ttl_is_the_shortest():
    now = 1_000_000.0
    raw = browser_cookies(
        ("__js_p_", "293,1800,0,0,0", -1),
        ("__jhash_", "42", now + 1795),
        ("__jua_", "ua", now + 1795),
        ("__lhash_", "x", now + 604795),
        ("_ym_uid", "metrika", now + 10**8),
        ("yandexuid", "metrika", now + 10**8),
    )

    cookies, ttl = pick_challenge_cookies(raw, now=now)

    assert set(cookies) == {"__js_p_", "__jhash_", "__jua_", "__lhash_"}
    assert ttl == pytest.approx(1795)


def test_session_only_cookies_fall_back_to_default_ttl():
    cookies, ttl = pick_challenge_cookies(browser_cookies(("__jhash_", "42", -1)), now=0.0)

    assert cookies == {"__jhash_": "42"}
    assert ttl == DEFAULT_TTL


def test_missing_pass_marker_means_the_check_was_not_passed():
    with pytest.raises(ChallengeSolveError, match="__jhash_"):
        pick_challenge_cookies(browser_cookies(("__js_p_", "293,1800,0,0,0", -1)), now=0.0)

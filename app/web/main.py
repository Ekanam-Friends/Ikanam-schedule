"""HTTP-сервис подписки на календарь.

Здесь живёт единственный по-настоящему публичный адрес проекта: ссылка, по
которой календарь пользователя сам ходит за расписанием. У этого есть три
следствия, определяющие весь модуль.

* **Ссылка — это и есть аутентификация.** Календари не умеют логиниться, поэтому
  секрет лежит прямо в пути. Значит: длинный случайный токен, `noindex`, и
  никаких токенов в логах.
* **Ходят не люди, а программы.** Google Calendar опрашивает фид по своему
  расписанию, Apple — по своему, и оба уважают `ETag`: если содержимое не
  менялось, отвечаем `304`, и трафик становится бесплатным для обеих сторон.
* **Пустой ответ хуже устаревшего.** Если синхронизация сломалась, отдаём
  последний удачный снапшот, а не пустой календарь: иначе у пользователя молча
  исчезнут все пары.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse

from app.calendar.ics import build_calendar
from app.core.config import get_settings
from app.core.crypto import CredentialsCipher
from app.db.migrate import upgrade_to_head
from app.db.repo import UserRepository
from app.db.session import make_engine, make_session_factory, session_scope
from app.ranepa.models import DaySchedule, Lesson, LessonFormat, Schedule

app = FastAPI(
    title="Ikanam Schedule",
    description="Подписка на расписание РАНХиГС для календаря",
    docs_url=None,
    redoc_url=None,
)

DEMO_TOKEN = "demo"
"""Пока нормализация ответа кабинета не написана, по этому токену отдаётся
показательное расписание. Оно нужно, чтобы проверить самое хрупкое звено —
как настоящий календарь на телефоне ведёт себя с нашим фидом."""


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return INDEX_PAGE


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


def configure_database(application: FastAPI, database_url: str, credentials_key: str) -> None:
    """Подключить базу к приложению.

    Вызывается из lifespan с настройками из окружения, а в тестах — напрямую с
    временной SQLite: так тесты не зависят от `.env` разработчика.
    """
    application.state.session_factory = make_session_factory(make_engine(database_url))
    application.state.cipher = CredentialsCipher(credentials_key)


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    await upgrade_to_head(settings.database_url)
    configure_database(
        application, settings.database_url, settings.credentials_key.get_secret_value()
    )
    yield


app.router.lifespan_context = lifespan
app.state.session_factory = None
app.state.cipher = None


@app.get("/feed/{token}.ics")
async def feed(token: str, request: Request) -> Response:
    """Отдать календарь по секретной ссылке."""
    schedule, reminder, sequences = await _load_schedule(token)
    if schedule is None:
        # Одинаковый ответ и на несуществующий, и на отозванный токен: разница в
        # ответах позволила бы перебором отличать живые ссылки от мёртвых.
        raise HTTPException(status_code=404, detail="Календарь не найден")

    body = build_calendar(
        schedule,
        calendar_name="Расписание РАНХиГС",
        for_subscription=True,
        reminder_minutes=reminder,
        sequences=sequences,
    )

    etag = f'"{hashlib.sha1(body).hexdigest()}"'
    if request.headers.get("if-none-match") == etag:
        # Календарь уже видел эту версию — не гоняем её повторно.
        return Response(status_code=304, headers={"ETag": etag})

    return Response(
        content=body,
        media_type="text/calendar; charset=utf-8",
        headers={
            "ETag": etag,
            "Cache-Control": "private, max-age=3600",
            "X-Robots-Tag": "noindex, nofollow",
            "Content-Disposition": 'inline; filename="ranepa-schedule.ics"',
        },
    )


async def _load_schedule(
    token: str,
) -> tuple[Schedule | None, int | None, dict[str, int] | None]:
    """Расписание по токену подписки: последний удачный снапшот из базы.

    Пользователь без снапшотов получает пустой календарь, а не 404: подписка
    у него есть, просто пар пока нет. 404 — только для чужих и отозванных
    токенов. Демо-токен остаётся для проверки клиентов без базы.

    Третий элемент — номера ревизий пар (`SEQUENCE`): без них календарь не
    примет правку уже известного события.
    """
    if token == DEMO_TOKEN:
        return _demo_schedule(), None, None

    factory = app.state.session_factory
    if factory is None:
        return None, None, None

    async with session_scope(factory) as session:
        repo = UserRepository(session, app.state.cipher)
        user = await repo.get_by_feed_token(token)
        if user is None:
            return None, None, None
        # Прошлые дни календарю не нужны, но неделя назад полезна: человек
        # видит, что было, и клиент не удаляет события задним числом.
        since = date.today() - timedelta(days=7)
        schedule = await repo.load_schedule(user, since=since)
        return schedule, user.reminder_minutes, await repo.sequences_for(user)


def _demo_schedule() -> Schedule:
    """Показательная неделя: обычная пара, дистант, отменённое занятие."""
    monday = date.today() - timedelta(days=date.today().weekday())

    def at(day_offset: int, hour: int, minute: int) -> datetime:
        day = monday + timedelta(days=day_offset)
        return datetime(day.year, day.month, day.day, hour, minute)

    days = [
        DaySchedule(
            day=monday,
            lessons=[
                Lesson(
                    subject="Математический анализ",
                    start=at(0, 9, 0),
                    end=at(0, 10, 20),
                    teacher="Козко Артем Иванович",
                    room="5 - 406 (24) П+ПК",
                    building="Вернадского, 82 - корпус 5",
                    lesson_type="Лекция",
                ),
                Lesson(
                    subject="История России",
                    start=at(0, 10, 40),
                    end=at(0, 12, 0),
                    teacher="Иванова Мария Петровна",
                    room="1 - 3406 (26)",
                    building="Вернадского, 84 - корпус 1",
                    lesson_type="Семинар",
                ),
            ],
        ),
        DaySchedule(
            day=monday + timedelta(days=2),
            lessons=[
                Lesson(
                    subject="Иностранный язык",
                    start=at(2, 13, 0),
                    end=at(2, 14, 20),
                    teacher="Смирнов Олег Викторович",
                    lesson_format=LessonFormat.DISTANT,
                    lesson_type="Практическое занятие",
                ),
                Lesson(
                    subject="Физическая культура",
                    start=at(2, 15, 40),
                    end=at(2, 17, 0),
                    teacher="Петров Андрей Игоревич",
                    room="Спортивный зал",
                    building="Вернадского, 82",
                    cancelled=True,
                ),
            ],
        ),
    ]
    # Время «получения» данных фиксировано: иначе демо-лента отличалась бы при
    # каждом запросе и подписка никогда не попадала бы в кэш.
    return Schedule(days=days, fetched_at=datetime(monday.year, monday.month, monday.day, 3, 0))


INDEX_PAGE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Ikanam Schedule</title>
<style>
  :root { color-scheme: light dark; }
  body { font: 16px/1.6 system-ui, -apple-system, "Segoe UI", sans-serif;
         max-width: 42rem; margin: 0 auto; padding: 2rem 1.25rem; }
  code { background: rgba(128,128,128,.16); padding: .15em .4em; border-radius: .3em;
         word-break: break-all; }
  h2 { margin-top: 2rem; font-size: 1.1rem; }
  ol { padding-left: 1.2rem; }
  .muted { opacity: .7; font-size: .9rem; }
</style>
</head>
<body>
<h1>Расписание РАНХиГС в календаре</h1>
<p>Сервис отдаёт расписание по личной ссылке, на которую подписывается календарь.
Подписка обновляется сама — переимпортировать ничего не нужно.</p>

<p>Демонстрационная лента для проверки:<br>
<code id="url">/feed/demo.ics</code></p>

<h2>iPhone и iPad</h2>
<ol>
  <li>Настройки → Календарь → Учётные записи → Добавить учётную запись</li>
  <li>Другое → Подписной календарь</li>
  <li>Вставить ссылку → Далее → Сохранить</li>
</ol>

<h2>Google Calendar</h2>
<ol>
  <li>Открыть календарь на компьютере: calendar.google.com</li>
  <li>Слева «Другие календари» → «+» → «Добавить по URL»</li>
  <li>Вставить ссылку → «Добавить календарь»</li>
</ol>
<p class="muted">Google обновляет подписки по своему расписанию, обычно раз в
несколько часов, и игнорирует напоминания внутри событий — их задают в настройках
календаря.</p>

<h2>Android</h2>
<p>Календарь на Android показывает то, что лежит в аккаунте Google, — добавьте
подписку по инструкции выше, и она появится на телефоне.</p>

<script>
  document.getElementById('url').textContent = location.origin + '/feed/demo.ics';
</script>
</body>
</html>
"""

"""Проверки эндпоинта подписки.

Ленту читают не люди, а календари, и ведут они себя буквально: неверный
content-type — файл не распознан, нестабильное содержимое — бесконечная
перекачка, разные коды ответов на живой и мёртвый токен — возможность перебора.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.web.main import app

client = TestClient(app)


def test_feed_is_served_as_calendar():
    response = client.get("/feed/demo.ics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/calendar")
    assert response.text.startswith("BEGIN:VCALENDAR")


def test_feed_content_is_stable_between_requests():
    """Нестабильный ответ ломает кэширование и заставляет клиента считать
    все пары изменёнными при каждой проверке."""
    first = client.get("/feed/demo.ics")
    second = client.get("/feed/demo.ics")

    assert first.content == second.content
    assert first.headers["etag"] == second.headers["etag"]


def test_unchanged_feed_answers_not_modified():
    etag = client.get("/feed/demo.ics").headers["etag"]

    response = client.get("/feed/demo.ics", headers={"If-None-Match": etag})

    assert response.status_code == 304


def test_unknown_token_is_not_found():
    assert client.get("/feed/unknown-token.ics").status_code == 404


def test_feed_asks_search_engines_to_stay_away():
    """Ссылка — это и есть аутентификация: попадание в индекс равно утечке."""
    response = client.get("/feed/demo.ics")

    assert "noindex" in response.headers.get("x-robots-tag", "")


def test_index_explains_how_to_subscribe():
    response = client.get("/")

    assert response.status_code == 200
    assert "iPhone" in response.text
    assert "Google Calendar" in response.text


def test_healthcheck():
    assert client.get("/healthz").json() == {"status": "ok"}


# --- Фид из базы ---

import base64  # noqa: E402
import os  # noqa: E402
from datetime import date, datetime, timezone  # noqa: E402

import pytest  # noqa: E402

from app.db.repo import UserRepository  # noqa: E402
from app.db.session import create_schema, make_engine, session_scope  # noqa: E402
from app.ranepa.models import DaySchedule, EduGroup, Lesson, Schedule, StudentProfile  # noqa: E402
from app.web.main import app, configure_database  # noqa: E402

PROFILE = StudentProfile(
    student_uid="s", org_uid="o", group_name="ЭИ-25", groups=(EduGroup(uid="g", name="ЭИ-25"),)
)


@pytest.fixture
def db_client(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'feed.db'}"
    key = base64.urlsafe_b64encode(os.urandom(32)).decode()

    async def prepare() -> str:
        engine = make_engine(url)
        await create_schema(engine)
        await engine.dispose()
        configure_database(app, url, key)
        async with session_scope(app.state.session_factory) as session:
            repo = UserRepository(session, app.state.cipher)
            user = await repo.connect(
                7, refresh_token="r", access_valid_until=None, profile=PROFILE
            )
            today = date.today()
            await repo.save_schedule(
                user,
                Schedule(
                    days=[
                        DaySchedule(
                            day=today,
                            lessons=[
                                Lesson(
                                    subject="Философия",
                                    start=datetime.combine(today, datetime.min.time()).replace(
                                        hour=9
                                    ),
                                    end=datetime.combine(today, datetime.min.time()).replace(
                                        hour=10, minute=20
                                    ),
                                    teacher="Иванов Иван Иванович",
                                    room="1 - 3406 (26) П+ПК Блок 3G",
                                    building="Вернадского, 84 - корпус 1",
                                )
                            ],
                        )
                    ],
                    fetched_at=datetime.now(timezone.utc),
                ),
            )
            empty = await repo.connect(
                8, refresh_token="r", access_valid_until=None, profile=PROFILE
            )
            return user.feed_token, empty.feed_token

    import asyncio

    tokens = asyncio.run(prepare())
    try:
        yield TestClient(app), tokens
    finally:
        app.state.session_factory = None
        app.state.cipher = None


def test_feed_serves_user_snapshot_by_token(db_client):
    client, (token, _) = db_client

    response = client.get(f"/feed/{token}.ics")

    assert response.status_code == 200
    # Сырой текст сравнивать нельзя: запятые в ICS экранируются как `\,`, а
    # строки длиннее 75 символов складываются переносом. Разбираем документ.
    from icalendar import Calendar

    event = next(c for c in Calendar.from_ical(response.content).walk() if c.name == "VEVENT")
    assert str(event["SUMMARY"]) == "Философия · Иванов И. И."
    assert str(event["LOCATION"]) == "ауд. 3406, блок 3G, корпус 1, Вернадского 84"
    assert "П+ПК" not in str(event["LOCATION"])


def test_user_without_snapshots_gets_empty_calendar_not_404(db_client):
    client, (_, empty_token) = db_client

    response = client.get(f"/feed/{empty_token}.ics")

    assert response.status_code == 200
    assert "BEGIN:VEVENT" not in response.text


def test_foreign_token_is_404_even_with_database(db_client):
    client, _ = db_client

    assert client.get("/feed/not-a-real-token.ics").status_code == 404

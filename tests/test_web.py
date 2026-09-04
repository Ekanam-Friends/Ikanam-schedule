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

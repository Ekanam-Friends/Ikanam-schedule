"""Проверки нормализации ответа кабинета.

Фикстура снята с живого ответа и обезличена: имена преподавателей, почты,
логины и все идентификаторы заменены. Структура и форматы строк — настоящие,
именно они здесь и проверяются.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pytest

from app.ranepa.models import LessonFormat
from app.ranepa.parser import ScheduleParseError, parse_schedule, split_room

FIXTURE = Path(__file__).parent / "fixtures" / "schedule_response.json"


@pytest.fixture
def payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_days_are_sorted_and_empty_day_is_kept(payload):
    """Кабинет отдаёт дни вперемешку, а пустой день — это «пар нет», не «нет данных»."""
    schedule = parse_schedule(payload)

    assert [d.day for d in schedule.days] == [date(2026, 9, 1), date(2026, 9, 5), date(2026, 9, 23)]
    assert schedule.day_for(date(2026, 9, 5)).is_empty


def test_lessons_within_day_are_sorted_by_start(payload):
    day = parse_schedule(payload).day_for(date(2026, 9, 23))

    assert [lesson.start.hour for lesson in day.lessons] == [14, 15]


def test_onsite_lesson_fields(payload):
    lesson = parse_schedule(payload).day_for(date(2026, 9, 23)).lessons[1]

    assert lesson.subject == "Математический анализ"
    assert lesson.start == datetime(2026, 9, 23, 15, 40)
    assert lesson.end == datetime(2026, 9, 23, 17, 0)
    assert lesson.teacher == "Сидоров Пётр Алексеевич"
    assert lesson.room == "5 - 122 (22) П+ПК"
    assert lesson.building == "Вернадского, 82 - корпус 5"
    assert lesson.lesson_type == "Практические занятия"
    assert lesson.lesson_format is LessonFormat.ONSITE


def test_pending_room_becomes_empty_location(payload):
    """«Уточняется» в поле адреса календаря выглядело бы как название места."""
    lesson = parse_schedule(payload).day_for(date(2026, 9, 23)).lessons[0]

    assert lesson.room is None
    assert lesson.building is None
    assert lesson.location == ""


def test_distant_lesson_is_recognised(payload):
    lesson = parse_schedule(payload).day_for(date(2026, 9, 1)).lessons[0]

    assert lesson.lesson_format is LessonFormat.DISTANT
    assert lesson.room is None
    assert "Дистанционно" in lesson.location


def test_teacher_personal_data_is_not_carried_over(payload):
    """`sam_login` и `sam_email` есть в ответе, но их нет в модели — и не будет в базе."""
    lesson = parse_schedule(payload).lessons[0]

    dumped = repr(lesson)
    assert "sam_" not in dumped
    assert "@example.org" not in dumped
    assert "lecturer1" not in dumped


def test_nothing_is_cancelled_by_default(payload):
    """Кабинет не сообщает отмены — этот признак вычисляет диффер, не парсер."""
    assert not any(lesson.cancelled for lesson in parse_schedule(payload).lessons)


def test_fetched_at_is_passed_through(payload):
    stamp = datetime(2026, 9, 4, 3, 0)

    assert parse_schedule(payload, fetched_at=stamp).fetched_at == stamp


def test_broken_lesson_is_skipped_not_fatal(payload):
    payload["days"][0]["discs"].append({"disc": "Без времени"})
    payload["days"][0]["discs"].append("не объект")

    day = parse_schedule(payload).day_for(date(2026, 9, 23))

    assert len(day.lessons) == 2


def test_day_without_date_is_skipped(payload):
    payload["days"].append({"date": None, "discs": []})

    assert len(parse_schedule(payload).days) == 3


def test_unrecognised_structure_is_an_error():
    """Другая структура — это смена формата кабинетом; тихо вернуть пустоту нельзя."""
    with pytest.raises(ScheduleParseError):
        parse_schedule({"data": []})
    with pytest.raises(ScheduleParseError):
        parse_schedule(None)


@pytest.mark.parametrize(
    ("room", "lms", "expected"),
    [
        (
            "5 - 122 (22) П+ПК (Вернадского, 82 - корпус 5)",
            None,
            ("5 - 122 (22) П+ПК", "Вернадского, 82 - корпус 5", LessonFormat.ONSITE),
        ),
        (
            "1 - 3406 (26) П+ПК Блок 3G (Вернадского, 84 - корпус 1)",
            None,
            ("1 - 3406 (26) П+ПК Блок 3G", "Вернадского, 84 - корпус 1", LessonFormat.ONSITE),
        ),
        ("Уточняется", None, (None, None, LessonFormat.UNKNOWN)),
        ("Уточняется", "https://lms", (None, None, LessonFormat.DISTANT)),
        ("СДО - СДО / дистанционно (СДО / дистанционно)", None, (None, None, LessonFormat.DISTANT)),
        ("Спортивный зал", None, ("Спортивный зал", None, LessonFormat.ONSITE)),
        ("", None, (None, None, LessonFormat.UNKNOWN)),
    ],
)
def test_split_room(room, lms, expected):
    assert split_room(room, lms) == expected


def test_capacity_parentheses_are_not_mistaken_for_address():
    """«(22)» — вместимость, а не адрес: важна именно последняя пара скобок."""
    auditorium, building, _ = split_room("5 - 122 (22) П+ПК")

    assert auditorium == "5 - 122 (22) П+ПК"
    assert building is None

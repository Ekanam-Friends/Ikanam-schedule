"""Проверки генератора календаря.

Здесь важны не столько поля сами по себе, сколько свойства, от которых зависит
поведение чужих календарей: стабильность UID между выгрузками, корректный сдвиг
московского времени в UTC и то, что отменённая пара остаётся в ленте с пометкой.
"""

from __future__ import annotations

from datetime import date, datetime

from icalendar import Calendar as ParsedCalendar

from app.calendar.ics import build_calendar, short_name
from app.ranepa.models import DaySchedule, Lesson, LessonFormat, Schedule


def make_lesson(**overrides) -> Lesson:
    defaults = dict(
        subject="Математический анализ",
        start=datetime(2026, 9, 4, 9, 0),
        end=datetime(2026, 9, 4, 10, 20),
        teacher="Козко Артем Иванович",
        room="5 - 406 (24) П+ПК",
        building="Вернадского, 82 - корпус 5",
        lesson_format=LessonFormat.ONSITE,
    )
    defaults.update(overrides)
    return Lesson(**defaults)


def make_schedule(*lessons: Lesson) -> Schedule:
    return Schedule(days=[DaySchedule(day=date(2026, 9, 4), lessons=list(lessons))])


def parse(raw: bytes) -> ParsedCalendar:
    return ParsedCalendar.from_ical(raw)


def events(raw: bytes) -> list:
    return [c for c in parse(raw).walk() if c.name == "VEVENT"]


def test_moscow_time_is_converted_to_utc():
    """09:00 по Москве — это 06:00 UTC; ошибка здесь сдвинет все пары на 3 часа."""
    raw = build_calendar(make_schedule(make_lesson()))
    event = events(raw)[0]

    assert event["DTSTART"].dt.hour == 6
    assert event["DTEND"].dt.hour == 7
    assert event["DTEND"].dt.minute == 20


def test_uid_survives_room_and_teacher_change():
    """Смена аудитории — обновление пары, а не новое событие в сетке."""
    original = make_lesson()
    moved = make_lesson(room="1 - 3406 (26)", teacher="Другой преподаватель")

    assert original.uid == moved.uid


def test_uid_differs_between_lessons():
    morning = make_lesson()
    evening = make_lesson(start=datetime(2026, 9, 4, 15, 40), end=datetime(2026, 9, 4, 17, 0))

    assert morning.uid != evening.uid


def test_cancelled_lesson_stays_in_feed_marked():
    """Убрать пару из ленты недостаточно: клиенты оставляют её висеть в календаре."""
    raw = build_calendar(make_schedule(make_lesson(cancelled=True)))
    event = events(raw)[0]

    assert event["STATUS"] == "CANCELLED"
    assert "Отменено" in str(event["SUMMARY"])


def test_distant_lesson_location():
    raw = build_calendar(make_schedule(make_lesson(lesson_format=LessonFormat.DISTANT)))
    event = events(raw)[0]

    assert "Дистанционно" in str(event["LOCATION"])


def test_location_joins_room_and_building():
    raw = build_calendar(make_schedule(make_lesson()))
    location = str(events(raw)[0]["LOCATION"])

    assert "5 - 406 (24) П+ПК" in location
    assert "Вернадского, 82 - корпус 5" in location


def test_sequence_comes_from_storage():
    """Календарь применит обновление события, только если SEQUENCE вырос."""
    lesson = make_lesson()
    raw = build_calendar(make_schedule(lesson), sequences={lesson.uid: 3})

    assert int(events(raw)[0]["SEQUENCE"]) == 3


def test_subscription_advertises_refresh_interval():
    raw = build_calendar(make_schedule(make_lesson()), for_subscription=True)

    assert b"REFRESH-INTERVAL" in raw
    assert b"X-PUBLISHED-TTL" in raw


def test_reminder_is_added_only_when_requested():
    without = build_calendar(make_schedule(make_lesson()))
    with_alarm = build_calendar(make_schedule(make_lesson()), reminder_minutes=15)

    assert b"VALARM" not in without
    assert b"VALARM" in with_alarm


def test_cancelled_lesson_has_no_reminder():
    raw = build_calendar(make_schedule(make_lesson(cancelled=True)), reminder_minutes=15)

    assert b"VALARM" not in raw


def test_calendar_name_is_visible_to_client():
    raw = build_calendar(make_schedule(make_lesson()), calendar_name="Моё расписание")

    assert "Моё расписание" in raw.decode("utf-8")


def test_diff_describes_changes_in_russian():
    before = make_lesson()
    after = make_lesson(room="1 - 3406 (26)", start=datetime(2026, 9, 4, 10, 40))

    changes = after.differs_from(before)

    assert any("время" in c for c in changes)
    assert any("аудитория" in c for c in changes)


# --- Что обязано попасть в календарь: место, время, преподаватель ---


def test_summary_shows_teacher_next_to_subject():
    """В дневной сетке видно только заголовок — преподаватель должен быть в нём."""
    raw = build_calendar(make_schedule(make_lesson()))
    summary = str(events(raw)[0]["SUMMARY"])

    assert "Математический анализ" in summary
    assert "Козко А. И." in summary


def test_description_keeps_full_name_and_place():
    raw = build_calendar(make_schedule(make_lesson()))
    description = str(events(raw)[0]["DESCRIPTION"])

    assert "Козко Артем Иванович" in description
    assert "5 - 406 (24) П+ПК" in description


def test_lesson_without_teacher_keeps_clean_title():
    raw = build_calendar(make_schedule(make_lesson(teacher=None)))
    summary = str(events(raw)[0]["SUMMARY"])

    assert summary == "Математический анализ"
    assert "·" not in summary


def test_cancelled_lesson_still_names_teacher():
    raw = build_calendar(make_schedule(make_lesson(cancelled=True)))
    summary = str(events(raw)[0]["SUMMARY"])

    assert summary.startswith("Отменено:")
    assert "Козко А. И." in summary


def test_short_name_handles_two_and_three_parts():
    assert short_name("Козко Артем Иванович") == "Козко А. И."
    assert short_name("Иванова Мария") == "Иванова М."


def test_short_name_leaves_unusual_input_alone():
    """Испорченная фамилия в заголовке заметнее, чем несокращённая."""
    assert short_name("Смирнов") == "Смирнов"
    assert short_name("Петров А.И.") == "Петров А."

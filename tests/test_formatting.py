"""Проверки текстового вида расписания в чате."""

from __future__ import annotations

from datetime import date, datetime

from app.bot.formatting import (
    format_date,
    format_day,
    format_lesson,
    format_missing_day,
    format_week,
    short_building,
)
from app.ranepa.models import DaySchedule, Lesson, LessonFormat, Schedule


def lesson(**overrides) -> Lesson:
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


def test_date_in_russian_without_year():
    assert format_date(date(2026, 9, 4)) == "пятница, 4 сентября"


def test_lesson_shows_time_room_building_and_teacher():
    text = format_lesson(lesson())

    assert "<b>09:00–10:20</b>" in text
    assert "Математический анализ" in text
    assert "ауд. 406" in text
    assert "П+ПК" not in text and "(24)" not in text
    assert "корпус 5, Вернадского 82" in text
    assert "Козко Артем Иванович" in text


def test_distant_lesson_says_so_instead_of_room():
    text = format_lesson(lesson(lesson_format=LessonFormat.DISTANT, room=None, building=None))

    assert "дистанционно" in text
    assert "уточняется" not in text


def test_pending_room_is_stated_honestly():
    text = format_lesson(lesson(room=None, building=None))

    assert "аудитория уточняется" in text


def test_cancelled_lesson_is_struck_through():
    text = format_lesson(lesson(cancelled=True))

    assert "<s>Математический анализ</s>" in text
    assert "отменена" in text


def test_html_in_subject_is_escaped():
    """Название вроде «C++ <шаблоны>» не должно ломать разметку сообщения."""
    text = format_lesson(lesson(subject="C++ <шаблоны>"))

    assert "&lt;шаблоны&gt;" in text


def test_empty_day():
    assert "Пар нет" in format_day(DaySchedule(day=date(2026, 9, 4)))


def test_missing_day_differs_from_empty_day():
    """«Данных нет» и «пар нет» — разные сообщения, второе нельзя показать вместо первого."""
    text = format_missing_day(date(2026, 9, 4))

    assert "не загружено" in text
    assert "Пар нет" not in text


def test_day_uses_custom_title():
    text = format_day(DaySchedule(day=date(2026, 9, 4), lessons=[lesson()]), title="Сегодня")

    assert text.startswith("<b>Сегодня</b>")


def test_week_collapses_empty_and_missing_days():
    schedule = Schedule(
        days=[
            DaySchedule(
                day=date(2026, 9, 7),
                lessons=[
                    lesson(start=datetime(2026, 9, 7, 9, 0), end=datetime(2026, 9, 7, 10, 20))
                ],
            ),
            DaySchedule(day=date(2026, 9, 8)),
        ]
    )

    text = format_week(schedule, since=date(2026, 9, 7), days=3)

    assert "Понедельник, 7 сентября" in text
    assert "Вторник, 8 сентября</b> — пар нет" in text
    assert "Среда, 9 сентября</b> — нет данных" in text


def test_short_building_puts_corpus_first():
    assert short_building("Вернадского, 82 - корпус 5") == "корпус 5, Вернадского 82"
    assert short_building("Спортивный комплекс") == "Спортивный комплекс"

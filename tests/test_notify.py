"""Проверки текстов уведомлений об изменениях."""

from __future__ import annotations

from datetime import datetime

from app.ranepa.models import Lesson, LessonFormat
from app.services.diff import Change, ChangeKind
from app.services.notify import TELEGRAM_MESSAGE_LIMIT, format_changes


def lesson(subject="Матанализ", day=7, hour=9, **overrides) -> Lesson:
    defaults = dict(
        subject=subject,
        start=datetime(2026, 9, day, hour, 0),
        end=datetime(2026, 9, day, hour + 1, 20),
        teacher="Козко А. И.",
        room="1 - 3406 (26) П+ПК Блок 3G",
        building="Вернадского, 84 - корпус 1",
        lesson_format=LessonFormat.ONSITE,
    )
    defaults.update(overrides)
    return Lesson(**defaults)


def test_nothing_to_say():
    assert format_changes([]) is None


def test_cancellation_is_struck_through():
    text = format_changes([Change(ChangeKind.CANCELLED, lesson(cancelled=True))])

    assert "<s>09:00 Матанализ</s> — отменена" in text
    assert "Понедельник, 7 сентября" in text


def test_move_names_both_times():
    change = Change(
        ChangeKind.MOVED, lesson(hour=12), previous=lesson(hour=9), details=("время: 09:00 → 12:00",)
    )
    text = format_changes([change])

    assert "перенесена с 09:00 на 12:00" in text


def test_move_with_room_change_mentions_room_too():
    change = Change(
        ChangeKind.MOVED,
        lesson(hour=12),
        previous=lesson(hour=9),
        details=("время: 09:00 → 12:00", "аудитория: 5 - 406 → 1 - 3406"),
    )
    text = format_changes([change])

    assert "перенесена с 09:00 на 12:00 (аудитория: 5 - 406 → 1 - 3406)" in text


def test_added_lesson_shows_human_place():
    text = format_changes([Change(ChangeKind.ADDED, lesson())])

    assert "добавлена, ауд. 3406, блок 3G, корпус 1, Вернадского 84" in text
    assert "П+ПК" not in text


def test_changed_lesson_lists_details():
    change = Change(ChangeKind.CHANGED, lesson(), previous=lesson(), details=("аудитория: 5 - 406 → 1 - 3406",))
    text = format_changes([change])

    assert "09:00 Матанализ — аудитория: 5 - 406 → 1 - 3406" in text


def test_changes_are_grouped_by_day_in_order():
    text = format_changes([
        Change(ChangeKind.ADDED, lesson(day=9)),
        Change(ChangeKind.CANCELLED, lesson(day=7, cancelled=True)),
    ])

    assert text.index("7 сентября") < text.index("9 сентября")
    assert text.count("<b>Расписание изменилось</b>") == 1


def test_html_in_subject_is_escaped():
    text = format_changes([Change(ChangeKind.ADDED, lesson(subject="C++ <шаблоны>"))])

    assert "&lt;шаблоны&gt;" in text


def test_huge_change_list_fits_telegram_limit():
    changes = [Change(ChangeKind.ADDED, lesson(subject=f"Предмет номер {i}", day=(i % 20) + 1)) for i in range(400)]
    text = format_changes(changes)

    assert len(text) <= TELEGRAM_MESSAGE_LIMIT
    assert "/week" in text

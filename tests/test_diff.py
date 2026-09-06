"""Проверки диффера расписания.

Кабинет не отдаёт ни отмен, ни переносов — всё это выводится отсюда, поэтому
каждое правило проверяется отдельно.
"""

from __future__ import annotations

from datetime import date, datetime

from app.ranepa.models import DaySchedule, Lesson, LessonFormat, Schedule
from app.services.diff import ChangeKind, diff_schedules

DAY = date(2026, 9, 7)


def lesson(subject="Матанализ", hour=9, minute=0, **overrides) -> Lesson:
    defaults = dict(
        subject=subject,
        start=datetime(2026, 9, 7, hour, minute),
        end=datetime(2026, 9, 7, hour + 1, minute + 20 if minute < 40 else minute - 40),
        teacher="Козко А. И.",
        room="5 - 406",
        building="Вернадского, 82 - корпус 5",
        lesson_format=LessonFormat.ONSITE,
    )
    defaults.update(overrides)
    return Lesson(**defaults)


def schedule(*lessons: Lesson, day: date = DAY) -> Schedule:
    return Schedule(days=[DaySchedule(day=day, lessons=list(lessons))])


def kinds(result) -> list[ChangeKind]:
    return [c.kind for c in result.changes]


def test_identical_schedules_produce_no_changes():
    result = diff_schedules(schedule(lesson()), schedule(lesson()))

    assert result.is_empty
    assert len(result.merged.lessons) == 1


def test_first_sync_is_silent():
    """Дня в старом снапшоте не было — «добавлено 4 пары» никого не интересует."""
    result = diff_schedules(Schedule(), schedule(lesson(), lesson("История", 11)))

    assert result.is_empty
    assert len(result.merged.lessons) == 2


def test_vanished_lesson_becomes_cancelled_and_stays_in_snapshot():
    """Иначе подписанный календарь оставит событие висеть как ни в чём не бывало."""
    result = diff_schedules(schedule(lesson(), lesson("История", 11)), schedule(lesson()))

    assert kinds(result) == [ChangeKind.CANCELLED]
    cancelled = result.changes[0].lesson
    assert cancelled.subject == "История"
    assert cancelled.cancelled
    assert [l.subject for l in result.merged.lessons] == ["Матанализ", "История"]
    assert result.merged.lessons[1].cancelled


def test_new_lesson_is_added():
    result = diff_schedules(schedule(lesson()), schedule(lesson(), lesson("История", 11)))

    assert kinds(result) == [ChangeKind.ADDED]
    assert result.changes[0].lesson.subject == "История"


def test_same_subject_at_new_time_is_a_move_not_cancel_plus_add():
    result = diff_schedules(schedule(lesson(hour=9)), schedule(lesson(hour=12, minute=20)))

    assert kinds(result) == [ChangeKind.MOVED]
    change = result.changes[0]
    assert change.previous.start.hour == 9
    assert change.lesson.start.hour == 12
    assert any("время" in d for d in change.details)
    # Старого времени в снапшоте нет — событие в календаре переедет, а не раздвоится.
    assert [l.start.hour for l in result.merged.lessons] == [12]


def test_room_change_is_reported_with_details():
    result = diff_schedules(schedule(lesson(room="5 - 406")), schedule(lesson(room="1 - 3406")))

    assert kinds(result) == [ChangeKind.CHANGED]
    assert any("аудитория" in d for d in result.changes[0].details)


def test_teacher_change_is_reported():
    result = diff_schedules(schedule(lesson()), schedule(lesson(teacher="Другой П. П.")))

    assert kinds(result) == [ChangeKind.CHANGED]
    assert any("преподаватель" in d for d in result.changes[0].details)


def test_cancelled_lesson_that_reappears_is_restored():
    old = schedule(lesson(cancelled=True))
    result = diff_schedules(old, schedule(lesson()))

    assert kinds(result) == [ChangeKind.RESTORED]
    assert not result.merged.lessons[0].cancelled


def test_previously_cancelled_and_still_absent_stays_cancelled_silently():
    """Повторно сообщать об отмене каждую ночь нельзя."""
    old = schedule(lesson(), lesson("История", 11, cancelled=True))
    result = diff_schedules(old, schedule(lesson()))

    assert result.is_empty
    assert [(l.subject, l.cancelled) for l in result.merged.lessons] == [
        ("Матанализ", False),
        ("История", True),
    ]


def test_days_absent_from_new_fetch_are_not_compared():
    """Про день, которого нет в выгрузке, мы ничего не знаем — молчим."""
    old = Schedule(days=[
        DaySchedule(day=DAY, lessons=[lesson()]),
        DaySchedule(day=date(2026, 9, 8), lessons=[lesson(subject="История")]),
    ])
    result = diff_schedules(old, schedule(lesson()))

    assert result.is_empty
    assert [d.day for d in result.merged.days] == [DAY]


def test_empty_new_day_cancels_everything_that_was_there():
    result = diff_schedules(schedule(lesson(), lesson("История", 11)), schedule())

    assert kinds(result) == [ChangeKind.CANCELLED, ChangeKind.CANCELLED]
    assert all(l.cancelled for l in result.merged.lessons)


def test_merged_lessons_are_sorted_by_time():
    result = diff_schedules(schedule(lesson(hour=9), lesson("История", 13)), schedule(lesson("История", 13)))

    assert [l.start.hour for l in result.merged.lessons] == [9, 13]

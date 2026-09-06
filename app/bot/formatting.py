"""Расписание в виде текста для чата.

Формат подчинён тому, как его читают: на ходу, с телефона, за секунду до пары.
Поэтому время — первым и жирным, аудитория — сразу под предметом, а корпус
обрезан до того, что реально нужно, чтобы дойти.
"""

from __future__ import annotations

from datetime import date
from html import escape

from app.ranepa.models import DaySchedule, Lesson, LessonFormat, Schedule
from app.ranepa.rooms import pretty_building, pretty_room

WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
MONTHS_GENITIVE = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


def format_date(day: date) -> str:
    """«четверг, 4 сентября» — без года: в чате он только шум."""
    return f"{WEEKDAYS[day.weekday()]}, {day.day} {MONTHS_GENITIVE[day.month - 1]}"


def format_lesson(lesson: Lesson) -> str:
    time = f"<b>{lesson.start:%H:%M}–{lesson.end:%H:%M}</b>"
    subject = escape(lesson.subject)
    if lesson.cancelled:
        subject = f"<s>{subject}</s> — отменена"

    lines = [f"{time}  {subject}"]

    details: list[str] = []
    if lesson.lesson_format is LessonFormat.DISTANT:
        details.append("дистанционно")
    elif lesson.room:
        details.append(escape(pretty_room(lesson.room) or lesson.room))
        if lesson.building:
            details.append(escape(pretty_building(lesson.building) or lesson.building))
    else:
        details.append("аудитория уточняется")
    lines.append("    " + ", ".join(details))

    if lesson.teacher:
        lines.append("    " + escape(lesson.teacher))
    return "\n".join(lines)


def format_day(day: DaySchedule, *, title: str | None = None) -> str:
    header = f"<b>{escape(title or format_date(day.day).capitalize())}</b>"
    if day.is_empty:
        return f"{header}\nПар нет."
    body = "\n\n".join(format_lesson(lesson) for lesson in day.lessons)
    return f"{header}\n\n{body}"


def format_missing_day(day: date, *, title: str | None = None) -> str:
    """Про этот день у нас нет данных — это не то же самое, что «пар нет»."""
    header = f"<b>{escape(title or format_date(day).capitalize())}</b>"
    return f"{header}\nРасписание на этот день ещё не загружено. Попробуйте /status."


def format_week(schedule: Schedule, *, since: date, days: int = 7) -> str:
    """Неделя одним сообщением; пустые дни свёрнуты в одну строку."""
    parts: list[str] = []
    for offset in range(days):
        current = since.__class__.fromordinal(since.toordinal() + offset)
        day = schedule.day_for(current)
        if day is None:
            parts.append(f"<b>{escape(format_date(current).capitalize())}</b> — нет данных")
        elif day.is_empty:
            parts.append(f"<b>{escape(format_date(current).capitalize())}</b> — пар нет")
        else:
            parts.append(format_day(day))
    return "\n\n".join(parts)


def short_building(building: str) -> str:
    """Совместимое имя; логика живёт в `app.ranepa.rooms`."""
    return pretty_building(building) or building

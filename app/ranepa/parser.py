"""Нормализация ответа `GET schedule` в доменные модели.

Схема ответа снята с живого кабинета (см. docs/api-notes.md):

    {
      "days": [
        {"date": "2026-09-23T00:00:00", "discs": [ {пара}, ... ]},
        ...
      ]
    }

Пара — плоский объект: `start_date`, `end_date`, `disc`, `type`, `lecturer`,
`room`, `lms_link`, `groups` и служебные `uid_*`. Три вещи, которые важно знать:

* **Отмен в ответе нет.** Поле `status` всегда «Добавление», фронтенд кабинета
  его не читает вовсе. Отменённая пара просто пропадает из выдачи, поэтому
  признак отмены здесь не вычисляется — это работа диффера, сравнивающего
  выдачу с предыдущим снапшотом.
* **Аудитория и адрес — одна строка.** «5 - 122 (22) П+ПК (Вернадского, 82 -
  корпус 5)»: адрес в последних скобках. «Уточняется» — аудитории пока нет.
  «СДО - СДО / дистанционно (…)» — дистант, обычно с `lms_link`.
* **В ответе есть персональные данные преподавателя** — `sam_login`,
  `sam_email`. Они нам не нужны и в модель не попадают: чего нет в модели,
  того нет ни в базе, ни в календаре.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

from app.ranepa.models import DaySchedule, Lesson, LessonFormat, Schedule

log = logging.getLogger(__name__)

ROOM_PENDING = "Уточняется"
DISTANT_MARKER = "СДО"


class ScheduleParseError(ValueError):
    """Ответ кабинета не похож на расписание.

    Это не «пара без аудитории», а «структура другая»: значит, кабинет
    изменил формат, и нужен человек, а не тихий пропуск.
    """


def parse_schedule(payload: Any, *, fetched_at: datetime | None = None) -> Schedule:
    """Собрать `Schedule` из сырого JSON ответа кабинета.

    Пустой день в ответе — это «пар нет», и он сохраняется как пустой
    `DaySchedule`: для диффера «в среду пусто» и «про среду данных нет» —
    разные вещи.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("days"), list):
        raise ScheduleParseError("В ответе нет списка days")

    days: list[DaySchedule] = []
    for raw_day in payload["days"]:
        day = _parse_day(raw_day)
        if day is not None:
            days.append(day)

    return Schedule(days=days, fetched_at=fetched_at)


def _parse_day(raw_day: Any) -> DaySchedule | None:
    if not isinstance(raw_day, dict):
        log.warning("Пропущен день: не объект (%r)", type(raw_day).__name__)
        return None

    day = _parse_date(raw_day.get("date"))
    if day is None:
        log.warning("Пропущен день без даты: %r", raw_day.get("date"))
        return None

    lessons: list[Lesson] = []
    for raw_lesson in raw_day.get("discs") or []:
        lesson = _parse_lesson(raw_lesson, day)
        if lesson is not None:
            lessons.append(lesson)

    return DaySchedule(day=day, lessons=lessons)


def _parse_lesson(raw: Any, day: date) -> Lesson | None:
    """Одна пара. Битую пару пропускаем с предупреждением, а не роняем весь день."""
    if not isinstance(raw, dict):
        return None

    start = _parse_datetime(raw.get("start_date"))
    end = _parse_datetime(raw.get("end_date"))
    subject = _clean(raw.get("disc"))
    if start is None or end is None or not subject:
        log.warning("Пропущена пара %s без времени или названия", day.isoformat())
        return None

    room, building, lesson_format = split_room(_clean(raw.get("room")), raw.get("lms_link"))

    return Lesson(
        subject=subject,
        start=start,
        end=end,
        teacher=_clean(raw.get("lecturer")) or None,
        room=room,
        building=building,
        lesson_format=lesson_format,
        lesson_type=_clean(raw.get("type")) or None,
    )


def split_room(room: str, lms_link: Any = None) -> tuple[str | None, str | None, LessonFormat]:
    """Разобрать строку аудитории кабинета.

    Returns:
        (аудитория, адрес, формат). Для «Уточняется» аудитория — `None`: в
        календаре пустое место честнее, чем слово «Уточняется» в поле адреса.
    """
    if not room or room == ROOM_PENDING:
        # Дистант без аудитории тоже бывает: тогда единственный признак — ссылка.
        fmt = LessonFormat.DISTANT if lms_link else LessonFormat.UNKNOWN
        return None, None, fmt

    if room.startswith(DISTANT_MARKER) or lms_link:
        return None, None, LessonFormat.DISTANT

    auditorium, building = _split_trailing_parentheses(room)
    return auditorium, building, LessonFormat.ONSITE


def _split_trailing_parentheses(text: str) -> tuple[str, str | None]:
    """«5 - 122 (22) П+ПК (Вернадского, 82 - корпус 5)» → аудитория и адрес.

    Скобок в строке несколько — «(22)» это вместимость, — поэтому берётся
    именно последняя пара, и только если строка ею заканчивается.
    """
    if not text.endswith(")"):
        return text, None
    opening = text.rfind("(")
    if opening <= 0:
        return text, None
    auditorium = text[:opening].strip()
    building = text[opening + 1 : -1].strip()
    return (auditorium or text), (building or None)


def _parse_datetime(value: Any) -> datetime | None:
    """«2026-09-23T15:40:00» — наивное время; часовой пояс кабинета московский."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _parse_date(value: Any) -> date | None:
    moment = _parse_datetime(value)
    return moment.date() if moment else None


def _clean(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""

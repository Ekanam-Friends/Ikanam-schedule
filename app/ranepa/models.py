"""Доменные модели расписания.

Слой намеренно отделён от формата ответа личного кабинета: всё, что придёт из
`/lk/n-api/schedule`, нормализуется сюда, и весь остальной код (календарь, бот,
диффер) знает только эти модели. Когда академия изменит формат ответа, чинить
придётся только `app/ranepa/parser.py`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum


class LessonFormat(str, Enum):
    """Как проходит занятие."""

    ONSITE = "onsite"
    """Очно, в аудитории."""

    DISTANT = "distant"
    """Дистанционно (в расписании — «СДО»)."""

    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Lesson:
    """Одно занятие в расписании студента."""

    subject: str
    start: datetime
    end: datetime
    teacher: str | None = None
    room: str | None = None
    """Аудитория как её показывает ЛК, например «1 - 3406 (26) П+ПК Блок 3G»."""

    building: str | None = None
    """Корпус, например «Вернадского, 84 - корпус 1»."""

    lesson_format: LessonFormat = LessonFormat.UNKNOWN
    lesson_type: str | None = None
    """Лекция, семинар, зачёт — если ЛК его сообщает."""

    cancelled: bool = False
    source_id: str | None = None
    """Идентификатор занятия в ЛК, если он есть в ответе API."""

    @property
    def location(self) -> str:
        """Место занятия одной строкой — для поля LOCATION в календаре."""
        if self.lesson_format is LessonFormat.DISTANT:
            return "Дистанционно (СДО)"
        parts = [p for p in (self.room, self.building) if p]
        return ", ".join(parts)

    @property
    def uid(self) -> str:
        """Стабильный идентификатор занятия для календаря.

        Календарь опознаёт событие по UID: одинаковый UID при повторной выгрузке —
        это обновление существующего события, новый UID — дубликат в сетке.
        Поэтому UID считается от того, что занятие *определяет* — дата, время
        начала и предмет, — но не от того, что у него меняется: аудитории,
        преподавателя, статуса отмены.

        Если ЛК отдаёт собственный идентификатор, используется он: тогда даже
        перенос пары на другое время не создаст дубликат.
        """
        if self.source_id:
            raw = f"ranepa:{self.source_id}"
        else:
            raw = f"ranepa:{self.start.date().isoformat()}:{self.start:%H:%M}:{self.subject}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def differs_from(self, other: Lesson) -> list[str]:
        """Человекочитаемый перечень отличий — основа уведомлений об изменениях."""
        changes: list[str] = []
        if self.start != other.start:
            changes.append(f"время: {other.start:%H:%M} → {self.start:%H:%M}")
        if self.room != other.room:
            changes.append(f"аудитория: {other.room or '—'} → {self.room or '—'}")
        if self.teacher != other.teacher:
            changes.append(f"преподаватель: {other.teacher or '—'} → {self.teacher or '—'}")
        if self.cancelled != other.cancelled:
            changes.append("занятие отменено" if self.cancelled else "отмена снята")
        return changes


@dataclass(frozen=True, slots=True)
class EduGroup:
    """Учебная группа студента с периодом действия."""

    uid: str
    name: str
    starts: date | None = None
    ends: date | None = None


@dataclass(frozen=True, slots=True)
class StudentProfile:
    """То немногое из учебных данных студента, что нужно для расписания.

    Здесь нет ни ФИО, ни контактов — кабинет их отдаёт, но для запроса
    расписания нужны только идентификаторы. Поле `group_name` — единственное
    человекочитаемое, оно для приветствия «Подключено: ЭИ-25».
    """

    student_uid: str
    org_uid: str
    group_name: str
    groups: tuple[EduGroup, ...]
    status: str | None = None
    course: str | None = None

    @property
    def group_uids(self) -> list[str]:
        """Все группы для `filter[]` — ровно так же шлёт их фронтенд кабинета.

        Фильтровать по датам действия соблазнительно, но фронтенд этого не
        делает, а расхождение с ним означало бы расписание, отличное от того,
        что студент видит в кабинете."""
        return [group.uid for group in self.groups]

    @property
    def is_active_student(self) -> bool:
        return self.status is None or self.status.lower() == "студент"


@dataclass(slots=True)
class DaySchedule:
    """Занятия одного дня, отсортированные по времени начала."""

    day: date
    lessons: list[Lesson] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.lessons.sort(key=lambda lesson: lesson.start)

    @property
    def is_empty(self) -> bool:
        return not self.lessons


@dataclass(slots=True)
class Schedule:
    """Расписание за произвольный период — то, что возвращает клиент ЛК."""

    days: list[DaySchedule] = field(default_factory=list)
    fetched_at: datetime | None = None

    def __post_init__(self) -> None:
        self.days.sort(key=lambda day: day.day)

    @property
    def lessons(self) -> list[Lesson]:
        return [lesson for day in self.days for lesson in day.lessons]

    def day_for(self, target: date) -> DaySchedule | None:
        return next((day for day in self.days if day.day == target), None)

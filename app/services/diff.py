"""Сравнение новой выгрузки расписания с предыдущим снапшотом.

Кабинет не сообщает ни отмен, ни переносов: отменённая пара просто исчезает
из выдачи, перенесённая — исчезает и появляется в другое время. Значит, всё,
что бот говорит человеку про изменения, рождается здесь, из разницы двух
выгрузок.

Три правила, которые определяют результат:

* **Исчезнувшая пара — отмена, а не пустота.** В календаре она остаётся с
  пометкой «отменено»: если просто перестать её отдавать, подписанный календарь
  на многих телефонах оставит событие висеть как ни в чём не бывало.
* **Отмена плюс появление того же предмета в тот же день — перенос.** Для
  человека это одно событие («пару передвинули на 12:20»), а не два.
* **Сравниваются только дни, которые есть в новой выгрузке.** Про остальные
  мы ничего нового не знаем, и молчать честнее, чем гадать.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum

from app.ranepa.models import DaySchedule, Lesson, Schedule


class ChangeKind(str, Enum):
    ADDED = "added"
    CANCELLED = "cancelled"
    MOVED = "moved"
    CHANGED = "changed"
    RESTORED = "restored"
    """Пара была помечена отменённой, а теперь снова в выдаче."""


@dataclass(frozen=True, slots=True)
class Change:
    kind: ChangeKind
    lesson: Lesson
    """Пара в её новом состоянии (для отмены — старая пара с `cancelled=True`)."""

    previous: Lesson | None = None
    details: tuple[str, ...] = ()
    """Что именно поменялось — для текста уведомления."""

    @property
    def day(self) -> date:
        return self.lesson.start.date()


@dataclass(slots=True)
class DiffResult:
    changes: list[Change] = field(default_factory=list)
    merged: Schedule = field(default_factory=Schedule)
    """Новая выгрузка, дополненная отменёнными парами из старого снапшота, —
    то, что нужно сохранить и отдать в календарь."""

    @property
    def is_empty(self) -> bool:
        return not self.changes


def diff_schedules(old: Schedule, new: Schedule) -> DiffResult:
    """Сравнить старый снапшот с новой выгрузкой по дням."""
    result = DiffResult()
    merged_days: list[DaySchedule] = []

    for new_day in new.days:
        old_day = old.day_for(new_day.day)
        if old_day is None:
            # Про этот день раньше данных не было — сравнивать не с чем, и
            # уведомлять «добавлено 4 пары» на первой синхронизации не нужно.
            merged_days.append(new_day)
            continue

        day_changes, merged_lessons = _diff_day(old_day, new_day)
        result.changes.extend(day_changes)
        merged_days.append(DaySchedule(day=new_day.day, lessons=merged_lessons))

    result.merged = Schedule(days=merged_days, fetched_at=new.fetched_at)
    return result


def _diff_day(old_day: DaySchedule, new_day: DaySchedule) -> tuple[list[Change], list[Lesson]]:
    old_by_uid = {lesson.uid: lesson for lesson in old_day.lessons}
    new_by_uid = {lesson.uid: lesson for lesson in new_day.lessons}

    changes: list[Change] = []
    merged: list[Lesson] = list(new_day.lessons)

    # Пары, которых больше нет в выдаче, — кандидаты на отмену или перенос.
    gone = [old for uid, old in old_by_uid.items() if uid not in new_by_uid and not old.cancelled]
    # Пары, которых раньше не было, — кандидаты на добавление или перенос.
    appeared = [new for uid, new in new_by_uid.items() if uid not in old_by_uid]

    # Перенос: исчезнувшая и появившаяся пара по одному предмету в один день.
    moved_from: dict[str, Lesson] = {}
    for old in list(gone):
        match = next((n for n in appeared if n.subject == old.subject), None)
        if match is not None:
            appeared.remove(match)
            gone.remove(old)
            moved_from[match.uid] = old
            changes.append(
                Change(
                    kind=ChangeKind.MOVED,
                    lesson=match,
                    previous=old,
                    details=tuple(match.differs_from(old)),
                )
            )

    for new in appeared:
        changes.append(Change(kind=ChangeKind.ADDED, lesson=new))

    for old in gone:
        cancelled = _mark_cancelled(old)
        changes.append(Change(kind=ChangeKind.CANCELLED, lesson=cancelled, previous=old))
        merged.append(cancelled)

    # Пары, которые остались: смотрим, что в них поменялось.
    for uid, new in new_by_uid.items():
        old = old_by_uid.get(uid)
        if old is None or uid in moved_from:
            continue
        if old.cancelled and not new.cancelled:
            changes.append(Change(kind=ChangeKind.RESTORED, lesson=new, previous=old))
            continue
        details = tuple(new.differs_from(old))
        if details:
            changes.append(Change(kind=ChangeKind.CHANGED, lesson=new, previous=old, details=details))

    # Отменённые раньше и всё ещё отсутствующие пары остаются отменёнными в
    # снапшоте, чтобы календарь не «воскресил» их, забыв про отмену.
    for uid, old in old_by_uid.items():
        if old.cancelled and uid not in new_by_uid:
            merged.append(old)

    merged.sort(key=lambda lesson: lesson.start)
    return changes, merged


def _mark_cancelled(lesson: Lesson) -> Lesson:
    return Lesson(
        subject=lesson.subject,
        start=lesson.start,
        end=lesson.end,
        teacher=lesson.teacher,
        room=lesson.room,
        building=lesson.building,
        lesson_format=lesson.lesson_format,
        lesson_type=lesson.lesson_type,
        cancelled=True,
        source_id=lesson.source_id,
    )

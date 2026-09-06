"""Тексты уведомлений об изменениях в расписании.

Одно сообщение на синхронизацию, сгруппированное по дням: студент, у которого
за ночь перенесли две пары и отменили третью, должен получить один пуш, а не
три. Формулировки — как сказал бы одногруппник: «Матанализ в среду перенесли
на 12:20», а не «изменение сущности занятия».
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from html import escape

from app.bot.formatting import format_date
from app.ranepa.models import Lesson
from app.ranepa.rooms import pretty_location
from app.services.diff import Change, ChangeKind

TELEGRAM_MESSAGE_LIMIT = 4096


def format_changes(changes: list[Change]) -> str | None:
    """Собрать сообщение об изменениях; `None`, если сообщать нечего."""
    if not changes:
        return None

    by_day: dict[date, list[Change]] = defaultdict(list)
    for change in changes:
        by_day[change.day].append(change)

    blocks: list[str] = ["<b>Расписание изменилось</b>"]
    for day in sorted(by_day):
        lines = [f"<b>{escape(format_date(day).capitalize())}</b>"]
        for change in sorted(by_day[day], key=lambda c: c.lesson.start):
            lines.append("• " + _describe(change))
        blocks.append("\n".join(lines))

    text = "\n\n".join(blocks)
    if len(text) > TELEGRAM_MESSAGE_LIMIT:
        # Такого не бывает при штатных правках, но смена всего семестра —
        # бывает. Лучше обрезать, чем не отправить ничего.
        head = text[: TELEGRAM_MESSAGE_LIMIT - 40].rsplit("\n", 1)[0]
        text = head + "\n\n…и ещё изменения, см. /week"
    return text


def _describe(change: Change) -> str:
    lesson = change.lesson
    subject = escape(lesson.subject)
    time = f"{lesson.start:%H:%M}"

    if change.kind is ChangeKind.CANCELLED:
        return f"<s>{time} {subject}</s> — отменена"

    if change.kind is ChangeKind.ADDED:
        return f"{time} {subject} — добавлена{_where(lesson)}"

    if change.kind is ChangeKind.RESTORED:
        return f"{time} {subject} — отмена снята{_where(lesson)}"

    if change.kind is ChangeKind.MOVED and change.previous is not None:
        was = f"{change.previous.start:%H:%M}"
        extra = [d for d in change.details if not d.startswith("время")]
        tail = f" ({', '.join(escape(d) for d in extra)})" if extra else ""
        return f"{subject} — перенесена с {was} на {time}{tail}"

    # CHANGED: время то же, поменялось что-то из деталей.
    details = ", ".join(escape(d) for d in change.details) or "изменения"
    return f"{time} {subject} — {details}"


def _where(lesson: Lesson) -> str:
    place = pretty_location(lesson.room, lesson.building)
    return f", {escape(place)}" if place else ""

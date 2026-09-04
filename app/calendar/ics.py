"""Сборка календаря в формате iCalendar (RFC 5545).

Один и тот же генератор обслуживает оба сценария:

* **файл** — разовый импорт, пользователь получает `.ics` в чат;
* **подписка** — тот же документ, отдаваемый по постоянной ссылке, который
  календарь перечитывает сам.

Разница только в заголовках подсказок об обновлении, поэтому код общий.

Про время. Расписание живёт в московском времени, у которого с 2014 года нет
перехода на летнее, — поэтому события пишутся в UTC (`DTSTART` со суффиксом `Z`)
вместо `TZID` с блоком `VTIMEZONE`. Так документ короче и его одинаково понимают
Google Calendar, Apple Calendar и Outlook, у которых исторически бывают разные
представления о содержимом `VTIMEZONE`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from icalendar import Alarm, Calendar, Event

from app.ranepa.models import Lesson, Schedule

MOSCOW = ZoneInfo("Europe/Moscow")

PRODID = "-//Ekanam Friends//Ikanam Schedule//RU"

DEFAULT_CALENDAR_NAME = "Расписание РАНХиГС"


def build_calendar(
    schedule: Schedule,
    *,
    calendar_name: str = DEFAULT_CALENDAR_NAME,
    for_subscription: bool = False,
    reminder_minutes: int | None = None,
    sequences: dict[str, int] | None = None,
) -> bytes:
    """Собрать календарь из расписания.

    Args:
        schedule: расписание, полученное из личного кабинета.
        calendar_name: имя календаря, которое увидит пользователь в приложении.
        for_subscription: добавить подсказки о том, как часто перечитывать ленту.
            Это именно подсказки: Google Calendar опрашивает подписки по своему
            расписанию (обычно раз в несколько часов) и эти поля игнорирует.
        reminder_minutes: за сколько минут напомнить о паре. Напоминания живут
            внутри событий, поэтому в *подписанных* календарях Google их не
            показывает — там пользователь задаёт напоминания сам, в настройках
            календаря. В импортированном файле и в Apple Calendar они работают.
        sequences: номер ревизии для каждого события по его UID. Календарь
            принимает обновление события, только если `SEQUENCE` вырос, поэтому
            значения берутся из базы, где мы считаем изменения каждой пары.

    Returns:
        Готовый документ iCalendar в кодировке UTF-8.
    """
    calendar = Calendar()
    calendar.add("prodid", PRODID)
    calendar.add("version", "2.0")
    calendar.add("calscale", "GREGORIAN")
    calendar.add("method", "PUBLISH")
    calendar.add("x-wr-calname", calendar_name)
    calendar.add("x-wr-timezone", "Europe/Moscow")

    if for_subscription:
        # Обе строки означают одно и то же: первая — современная (RFC 7986),
        # вторая — то, что понимают старые клиенты.
        calendar.add("refresh-interval;value=duration", timedelta(hours=4))
        calendar.add("x-published-ttl", timedelta(hours=4))

    stamp = datetime.now(timezone.utc)
    for lesson in schedule.lessons:
        sequence = (sequences or {}).get(lesson.uid, 0)
        calendar.add_component(
            _build_event(lesson, stamp=stamp, sequence=sequence, reminder_minutes=reminder_minutes)
        )

    return calendar.to_ical()


def _build_event(
    lesson: Lesson,
    *,
    stamp: datetime,
    sequence: int,
    reminder_minutes: int | None,
) -> Event:
    event = Event()
    event.add("uid", f"{lesson.uid}@ikanam.schedule")
    event.add("dtstamp", stamp)
    event.add("dtstart", _as_utc(lesson.start))
    event.add("dtend", _as_utc(lesson.end))
    event.add("summary", _summary(lesson))
    event.add("sequence", sequence)
    event.add("last-modified", stamp)
    event.add("transp", "OPAQUE")

    if lesson.location:
        event.add("location", lesson.location)

    description = _description(lesson)
    if description:
        event.add("description", description)

    # Отменённую пару не убираем из ленты, а помечаем: если просто перестать её
    # отдавать, подписанный календарь во многих клиентах оставит событие висеть.
    event.add("status", "CANCELLED" if lesson.cancelled else "CONFIRMED")

    if reminder_minutes is not None and not lesson.cancelled:
        event.add_component(_build_alarm(reminder_minutes, lesson))

    return event


def _build_alarm(minutes: int, lesson: Lesson) -> Alarm:
    alarm = Alarm()
    alarm.add("action", "DISPLAY")
    alarm.add("description", lesson.subject)
    alarm.add("trigger", timedelta(minutes=-minutes))
    return alarm


def _summary(lesson: Lesson) -> str:
    title = lesson.subject
    if lesson.cancelled:
        return f"Отменено: {title}"
    return title


def _description(lesson: Lesson) -> str:
    lines: list[str] = []
    if lesson.teacher:
        lines.append(lesson.teacher)
    if lesson.lesson_type:
        lines.append(lesson.lesson_type)
    if lesson.cancelled:
        lines.append("Занятие отменено")
    return "\n".join(lines)


def _as_utc(moment: datetime) -> datetime:
    """Привести время пары к UTC, считая наивное время московским."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=MOSCOW)
    return moment.astimezone(timezone.utc)

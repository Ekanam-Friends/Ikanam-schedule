"""Фоновые задачи бота: ночная синхронизация, пуши об изменениях, утренняя сводка.

Две петли, обе — обычные asyncio-задачи внутри процесса бота. Отдельный
планировщик (APScheduler, cron) здесь был бы лишней движущейся частью: задач
две, расписание у них простое, а состояние и так лежит в базе.

Ночная синхронизация обходит всех подключённых с ограничением параллелизма и
случайной задержкой между ними. Это не оптимизация, а вежливость: несколько
сотен логинов в одну секунду с одного адреса — для администраторов кабинета
это атака, и заблокировать наш IP им проще, чем разбираться.
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.formatting import format_date, format_day
from app.core.config import Settings
from app.core.crypto import CredentialsCipher
from app.db.models import User
from app.db.repo import UserRepository
from app.db.session import session_scope
from app.services.notify import format_changes
from app.services.sync import ReauthRequired, ScheduleSyncService, SyncError

log = logging.getLogger(__name__)

MOSCOW = timezone(timedelta(hours=3))

REAUTH_TEXT = (
    "Личный кабинет перестал принимать доступ бота — так бывает после смены "
    "пароля или по сроку. Подключите его заново: /login"
)


# --- Чистые правила времени: их удобно проверять без базы и без Telegram ---


def seconds_until(hour: int, *, now: datetime, jitter_minutes: int = 20) -> float:
    """Сколько ждать до ближайшего запуска в `hour` часов по Москве.

    К моменту добавляется случайный сдвиг: если несколько копий бота (или
    несколько похожих ботов) стартуют ровно в 03:00, кабинет получает пик.
    """
    now_msk = now.astimezone(MOSCOW)
    target = now_msk.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now_msk:
        target += timedelta(days=1)
    target += timedelta(minutes=random.uniform(0, jitter_minutes))
    return (target - now_msk).total_seconds()


def token_is_stale(user: User, *, now: datetime, max_age_days: int) -> bool:
    """Ключ доступа не удавалось обновить дольше допустимого.

    Точкой отсчёта служит последняя удачная синхронизация, а для тех, у кого
    её ещё не было, — момент подключения. Реальный срок токена задаёт кабинет;
    это наша граница, после которой ходить с ключом в чужой кабинет — только
    накапливать отказы.
    """
    anchor = user.last_sync_at or user.connected_at
    if anchor is None:
        return False
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=timezone.utc)
    return now - anchor > timedelta(days=max_age_days)


def digest_is_due(user: User, *, now: datetime, last_sent: date | None) -> bool:
    """Пора ли слать утреннюю сводку этому человеку.

    Сверяем «ЧЧ:ММ» из настроек с текущей минутой по Москве и не шлём дважды
    в один день, даже если петля проверок сработала два раза за минуту.
    """
    if not user.morning_digest_at or not user.is_connected:
        return False
    now_msk = now.astimezone(MOSCOW)
    if last_sent == now_msk.date():
        return False
    try:
        hh, mm = (int(part) for part in user.morning_digest_at.split(":"))
    except ValueError:
        return False
    return (now_msk.hour, now_msk.minute) == (hh, mm)


# --- Сами задачи ---


@dataclass
class SchedulerContext:
    bot: Bot
    settings: Settings
    cipher: CredentialsCipher
    session_factory: async_sessionmaker[AsyncSession]
    digest_sent: dict[int, date] = field(default_factory=dict)
    """Кому сводка уже ушла сегодня — чтобы не повторять в ту же минуту."""


@dataclass(slots=True)
class NightlyReport:
    total: int = 0
    synced: int = 0
    changed: int = 0
    reauth: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


async def run_nightly_loop(ctx: SchedulerContext) -> None:
    """Раз в сутки, в SYNC_HOUR_MSK, обойти всех подключённых."""
    while True:
        delay = seconds_until(ctx.settings.sync_hour_msk, now=datetime.now(timezone.utc))
        log.info("Ночная синхронизация через %.0f мин", delay / 60)
        await asyncio.sleep(delay)
        try:
            report = await sync_everyone(ctx)
            await report_to_owner(ctx, report)
        except Exception:  # noqa: BLE001 — петля не должна умереть из-за одной ночи
            log.exception("Ночная синхронизация упала целиком")


async def sync_everyone(ctx: SchedulerContext) -> NightlyReport:
    """Синхронизировать всех активных с ограничением параллелизма."""
    async with session_scope(ctx.session_factory) as session:
        repo = UserRepository(session, ctx.cipher)
        ids = [user.telegram_id for user in await repo.list_active()]

    report = NightlyReport(total=len(ids))
    semaphore = asyncio.Semaphore(ctx.settings.sync_concurrency)

    async def one(telegram_id: int) -> None:
        async with semaphore:
            # Разброс в пределах минуты внутри окна параллелизма — чтобы даже
            # четыре одновременных запроса не уходили одним залпом.
            await asyncio.sleep(random.uniform(0, 60))
            await sync_one(ctx, telegram_id, report)

    await asyncio.gather(*(one(i) for i in ids))
    log.info(
        "Ночь: всего %d, успешно %d, с изменениями %d, нужен вход %d, ошибок %d",
        report.total, report.synced, report.changed, report.reauth, report.failed,
    )
    return report


async def sync_one(ctx: SchedulerContext, telegram_id: int, report: NightlyReport) -> None:
    async with session_scope(ctx.session_factory) as session:
        repo = UserRepository(session, ctx.cipher)
        user = await repo.get(telegram_id)
        if user is None or not user.is_connected:
            return
        now = datetime.now(timezone.utc)
        if token_is_stale(user, now=now, max_age_days=ctx.settings.token_max_age_days):
            # Месяц без удачной синхронизации: ключ считаем мёртвым, снапшоты
            # оставляем — в календаре пусть будет старое расписание, а не пустота.
            await repo.mark_failed(
                user,
                error=f"Ключ не обновлялся {ctx.settings.token_max_age_days} дней",
                deactivate=True,
            )
            report.reauth += 1
            await _send(ctx.bot, telegram_id, REAUTH_TEXT)
            return
        service = ScheduleSyncService(repo)
        try:
            result = await service.sync_user(user)
        except ReauthRequired:
            report.reauth += 1
            await _send(ctx.bot, telegram_id, REAUTH_TEXT)
            return
        except SyncError as exc:
            report.failed += 1
            report.errors.append(f"{telegram_id}: {exc}"[:200])
            return

        report.synced += 1
        if result.changes and user.notify_on_change:
            report.changed += 1
            text = format_changes(result.changes)
            if text:
                await _send(ctx.bot, telegram_id, text)


async def run_digest_loop(ctx: SchedulerContext) -> None:
    """Каждую минуту проверять, кому пора прислать пары на сегодня."""
    while True:
        try:
            await send_due_digests(ctx)
        except Exception:  # noqa: BLE001
            log.exception("Рассылка утренней сводки упала")
        # До начала следующей минуты, чтобы проверка попадала в каждую.
        now = datetime.now(timezone.utc)
        await asyncio.sleep(60 - now.second + 0.5)


async def send_due_digests(ctx: SchedulerContext, *, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(MOSCOW).date()
    sent = 0
    async with session_scope(ctx.session_factory) as session:
        repo = UserRepository(session, ctx.cipher)
        for user in await repo.list_active():
            if not digest_is_due(user, now=now, last_sent=ctx.digest_sent.get(user.telegram_id)):
                continue
            schedule = await repo.load_schedule(user, since=today, until=today)
            day = schedule.day_for(today)
            if day is None or day.is_empty:
                # Без пар сводка — это будильник без причины. Молчим.
                ctx.digest_sent[user.telegram_id] = today
                continue
            text = format_day(day, title=f"Сегодня, {format_date(today)}")
            if await _send(ctx.bot, user.telegram_id, text):
                sent += 1
            ctx.digest_sent[user.telegram_id] = today
    return sent


async def report_to_owner(ctx: SchedulerContext, report: NightlyReport) -> None:
    """Алерт владельцу: только когда есть о чём.

    Каждую ночь писать «всё хорошо» — способ приучить владельца не читать
    сообщения бота. Пишем, когда есть ошибки, или когда сломалось у всех.
    """
    owner = ctx.settings.owner_chat_id
    if owner is None:
        return
    if report.failed == 0 and report.reauth == 0:
        return
    lines = [
        "<b>Ночная синхронизация</b>",
        f"Всего: {report.total}, успешно: {report.synced}, с изменениями: {report.changed}",
        f"Нужен повторный вход: {report.reauth}, ошибок: {report.failed}",
    ]
    if report.total and report.failed == report.total:
        lines.append("⚠️ Упали все — похоже, кабинет недоступен или сменил формат.")
    lines.extend(report.errors[:5])
    await _send(ctx.bot, owner, "\n".join(lines))


async def _send(bot: Bot, chat_id: int, text: str) -> bool:
    try:
        await bot.send_message(chat_id, text)
        return True
    except TelegramForbiddenError:
        # Человек заблокировал бота — это не ошибка синхронизации.
        log.info("Пользователь %s заблокировал бота", chat_id)
        return False
    except TelegramAPIError as exc:
        log.warning("Не удалось отправить сообщение %s: %s", chat_id, exc)
        return False


def start_background_tasks(ctx: SchedulerContext) -> list[asyncio.Task]:
    return [
        asyncio.create_task(run_nightly_loop(ctx), name="nightly-sync"),
        asyncio.create_task(run_digest_loop(ctx), name="morning-digest"),
    ]

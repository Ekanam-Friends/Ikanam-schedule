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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.types import BufferedInputFile
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.formatting import format_date, format_day
from app.core.config import Settings
from app.core.crypto import CredentialsCipher
from app.db.activity import ActivityLog
from app.db.models import User
from app.db.repo import UserRepository
from app.db.session import session_scope
from app.ranepa.client import RanepaClient, RanepaError
from app.services.notify import format_changes
from app.services.stats import collect, format_summary, render_chart
from app.services.sync import CabinetClosed, ReauthRequired, ScheduleSyncService, SyncError

log = logging.getLogger(__name__)

MOSCOW = timezone(timedelta(hours=3))

REAUTH_TEXT = (
    "Личный кабинет перестал принимать доступ бота — так бывает после смены "
    "пароля или по сроку. Подключите его заново: /login"
)

RECOVERY_INTERVAL = 30 * 60.0
"""Как часто проверять, вернулся ли кабинет после техработ.

Ночь 30.09.2026: кабинет закрыли заглушкой в 23:52, синхронизация в 03:00
упала у всех, а следующая попытка была бы только через сутки — весь день
люди ходили бы со вчерашним расписанием. Полчаса — это один запрос `version`
за полчаса, кабинету незаметно, а людям расписание обновится в тот же день."""


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


def last_night_started(hour: int, *, now: datetime) -> datetime:
    """Момент последнего штатного запуска ночной синхронизации (уже наступивший)."""
    now_msk = now.astimezone(MOSCOW)
    start = now_msk.replace(hour=hour, minute=0, second=0, microsecond=0)
    if start > now_msk:
        start -= timedelta(days=1)
    return start


def night_was_lost(users: list[User], *, now: datetime, sync_hour: int) -> bool:
    """После последнего штатного часа синхронизации не преуспел никто.

    Проверяется на старте бота: перезапуск после ночи, которая упала целиком
    (техработы кабинета, лежал сам сервер, бот перезапустили в самый час
    обхода), не должен оставлять людей со вчерашним расписанием до следующей
    ночи. Смотрим на самую свежую удачную синхронизацию среди всех: пока хоть
    у кого-то она после ночи, кабинет работал — у остальных свои причины.
    Без подключённых терять нечего.
    """
    if not users:
        return False
    stamps = [user.last_sync_at for user in users if user.last_sync_at is not None]
    if not stamps:
        return True
    newest = max(stamps)
    if newest.tzinfo is None:
        newest = newest.replace(tzinfo=timezone.utc)
    return newest < last_night_started(sync_hour, now=now)


def seconds_until_local(clock: str, tz: str, *, now: datetime) -> float:
    """Сколько ждать до ближайших «ЧЧ:ММ» в указанном часовом поясе.

    Считается в самом поясе, поэтому переход на летнее время не сдвигает
    момент: 06:00 по Киеву остаётся 06:00 и в июле, и в декабре.
    """
    hours, minutes = (int(part) for part in clock.split(":"))
    local_now = now.astimezone(ZoneInfo(tz))
    target = local_now.replace(hour=hours, minute=minutes, second=0, microsecond=0)
    if target <= local_now:
        target += timedelta(days=1)
    return (target - local_now).total_seconds()


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
    client_factory: Callable[..., RanepaClient] = RanepaClient
    """Как создавать клиент кабинета; см. `AppContext.client_factory`."""
    digest_sent: dict[int, date] = field(default_factory=dict)
    """Кому сводка уже ушла сегодня — чтобы не повторять в ту же минуту."""


@dataclass(slots=True)
class NightlyReport:
    total: int = 0
    synced: int = 0
    changed: int = 0
    reauth: int = 0
    failed: int = 0
    closed: int = 0
    """Сколько из `failed` — из-за заглушки кабинета (техработы)."""
    errors: list[str] = field(default_factory=list)

    @property
    def cabinet_was_closed(self) -> bool:
        """Не удалось никому, и хотя бы у одного — заглушка вместо API.

        Признак «кабинет закрыт», а не «у кого-то не вышло»: единичные
        ошибки бывают каждую ночь, а заглушка ломает всех разом."""
        return self.total > 0 and self.synced == 0 and self.closed > 0


async def run_nightly_loop(ctx: SchedulerContext) -> None:
    """Раз в сутки, в SYNC_HOUR_MSK, обойти всех подключённых."""
    try:
        if await nobody_synced_lately(ctx):
            log.warning("После последней ночи не синхронизировался никто — догоняем сейчас")
            await recover_after_maintenance(ctx)
    except Exception:  # noqa: BLE001 — старт бота важнее догоняющего обхода
        log.exception("Догоняющая синхронизация на старте упала")
    while True:
        delay = seconds_until(ctx.settings.sync_hour_msk, now=datetime.now(timezone.utc))
        log.info("Ночная синхронизация через %.0f мин", delay / 60)
        await asyncio.sleep(delay)
        try:
            report = await sync_everyone(ctx)
            await report_to_owner(ctx, report)
            if report.cabinet_was_closed:
                await recover_after_maintenance(ctx)
        except Exception:  # noqa: BLE001 — петля не должна умереть из-за одной ночи
            log.exception("Ночная синхронизация упала целиком")


async def nobody_synced_lately(ctx: SchedulerContext) -> bool:
    async with session_scope(ctx.session_factory) as session:
        users = await UserRepository(session, ctx.cipher).list_active()
    return night_was_lost(
        users, now=datetime.now(timezone.utc), sync_hour=ctx.settings.sync_hour_msk
    )


async def recover_after_maintenance(
    ctx: SchedulerContext,
    *,
    interval: float = RECOVERY_INTERVAL,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> NightlyReport | None:
    """Кабинет закрыт: каждые полчаса пробовать, не вернулся ли, и досинхронизировать.

    Сначала одна дешёвая проба `version` — гонять по заглушке все две сотни
    человек незачем. Ответил JSON — полный обход, как ночью, с отчётом
    владельцу. Если до штатной ночи осталось меньше интервала, уступаем ей.

    Returns:
        Отчёт обхода, которым всё закончилось, или None, если кабинет так и
        не вернулся до следующей ночи.
    """
    while True:
        until_night = seconds_until(ctx.settings.sync_hour_msk, now=now(), jitter_minutes=0)
        if until_night <= interval:
            log.info("Кабинет так и не вернулся — ждём штатной ночной синхронизации")
            return None
        if await cabinet_is_open(ctx):
            log.info("Кабинет отвечает — запускаем синхронизацию после техработ")
            report = await sync_everyone(ctx)
            await report_to_owner(ctx, report, title="Синхронизация после техработ", always=True)
            if not report.cabinet_was_closed:
                return report
        await sleep(interval)


async def cabinet_is_open(ctx: SchedulerContext) -> bool:
    """Одна проба `version`: JSON — открыт, заглушка или любая ошибка — нет."""
    try:
        async with ctx.client_factory() as client:
            await client.ping()
    except RanepaError as exc:
        log.info("Кабинет ещё закрыт: %s", exc)
        return False
    return True


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
        report.total,
        report.synced,
        report.changed,
        report.reauth,
        report.failed,
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
        service = ScheduleSyncService(repo, client_factory=ctx.client_factory)
        activity = ActivityLog(session)
        try:
            result = await service.sync_user(user)
        except ReauthRequired:
            report.reauth += 1
            await activity.record("sync:reauth")
            await _send(ctx.bot, telegram_id, REAUTH_TEXT)
            return
        except SyncError as exc:
            report.failed += 1
            if isinstance(exc, CabinetClosed):
                report.closed += 1
            report.errors.append(f"{telegram_id}: {exc}"[:200])
            await activity.record("sync:error")
            return

        report.synced += 1
        await activity.record("sync:ok")
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


async def run_owner_stats_loop(ctx: SchedulerContext) -> None:
    """Раз в сутки, в OWNER_STATS_AT по OWNER_STATS_TZ, прислать владельцу сводку."""
    clock = ctx.settings.owner_stats_at
    if clock is None or ctx.settings.owner_chat_id is None:
        return
    while True:
        delay = seconds_until_local(
            clock, ctx.settings.owner_stats_tz, now=datetime.now(timezone.utc)
        )
        log.info("Утренняя сводка владельцу через %.0f мин", delay / 60)
        await asyncio.sleep(delay)
        try:
            await send_owner_stats(ctx)
        except Exception:  # noqa: BLE001 — петля переживает любое утро
            log.exception("Утренняя сводка владельцу не отправилась")
        # Секунда форы, чтобы не отправить дважды в ту же минуту.
        await asyncio.sleep(1)


async def send_owner_stats(ctx: SchedulerContext) -> bool:
    """Собрать сводку и отправить владельцу текстом и картинкой."""
    owner = ctx.settings.owner_chat_id
    if owner is None:
        return False
    async with session_scope(ctx.session_factory) as session:
        repo = UserRepository(session, ctx.cipher)
        stats = await collect(repo, ActivityLog(session))
    if not await _send(ctx.bot, owner, format_summary(stats)):
        return False
    try:
        png = await asyncio.to_thread(render_chart, stats)
    except Exception:  # noqa: BLE001 — цифры ушли, картинка не обязана ломать утро
        log.exception("Не удалось построить график для утренней сводки")
        return True
    try:
        await ctx.bot.send_photo(owner, BufferedInputFile(png, filename="stats.png"))
    except TelegramAPIError as exc:
        log.warning("Не удалось отправить график владельцу: %s", exc)
    return True


async def report_to_owner(
    ctx: SchedulerContext,
    report: NightlyReport,
    *,
    title: str = "Ночная синхронизация",
    always: bool = False,
) -> None:
    """Алерт владельцу: только когда есть о чём.

    Каждую ночь писать «всё хорошо» — способ приучить владельца не читать
    сообщения бота. Пишем, когда есть ошибки, или когда сломалось у всех.
    `always` — для обхода после техработ: там и «всё хорошо» — новость.
    """
    owner = ctx.settings.owner_chat_id
    if owner is None:
        return
    if report.failed == 0 and report.reauth == 0 and not always:
        return
    lines = [
        f"<b>{title}</b>",
        f"Всего: {report.total}, успешно: {report.synced}, с изменениями: {report.changed}",
        f"Нужен повторный вход: {report.reauth}, ошибок: {report.failed}",
    ]
    if report.cabinet_was_closed:
        lines.append(
            "⚠️ Кабинет на техработах — отдаёт заглушку вместо API. "
            f"Пробую снова каждые {RECOVERY_INTERVAL / 60:.0f} мин."
        )
    elif report.total and report.failed == report.total:
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
        asyncio.create_task(run_owner_stats_loop(ctx), name="owner-stats"),
    ]

"""Синхронизация расписания одного пользователя с личным кабинетом.

Одна операция: взять refresh-токен → обновить доступ → запросить дни →
нормализовать → сохранить снапшоты. Ночной планировщик и `/login` вызывают
именно её, поэтому все решения о том, что считать ошибкой и что с ней делать,
живут здесь, а не в обработчиках.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from app.db.models import User
from app.db.repo import UserRepository
from app.ranepa.client import AuthError, RanepaClient, RanepaError, Tokens
from app.ranepa.models import Schedule
from app.ranepa.parser import ScheduleParseError, parse_schedule
from app.services.diff import Change, diff_schedules

log = logging.getLogger(__name__)

DEFAULT_HORIZON_DAYS = 21
"""Сколько дней вперёд запрашивать.

Три недели — компромисс: календарь видит достаточно, чтобы планировать, а
запрос к кабинету остаётся одним. Кабинет технически отдаст и семестр, но
каждая синхронизация — это его серверное время, потраченное на нас."""


@dataclass(frozen=True, slots=True)
class SyncResult:
    schedule: Schedule
    lessons_count: int
    changes: list[Change] = field(default_factory=list)
    """Что изменилось по сравнению с прошлым снапшотом — материал для пуша."""


class SyncError(Exception):
    """Синхронизация не удалась; повторить позже."""


class ReauthRequired(SyncError):
    """Кабинет не принял refresh-токен — нужен новый вход пользователя."""


class ScheduleSyncService:
    def __init__(self, repo: UserRepository, *, client_factory=RanepaClient) -> None:
        self._repo = repo
        self._client_factory = client_factory

    async def sync_user(
        self,
        user: User,
        *,
        today: date | None = None,
        horizon_days: int = DEFAULT_HORIZON_DAYS,
    ) -> SyncResult:
        refresh_token = self._repo.refresh_token_of(user)
        if not refresh_token or not user.group_uids or not user.org_uid:
            raise ReauthRequired("Кабинет не подключён")

        today = today or datetime.now(timezone.utc).astimezone(MOSCOW_OFFSET).date()
        days = [today + timedelta(days=offset) for offset in range(horizon_days)]

        tokens = Tokens(
            access_token="",
            refresh_token=refresh_token,
            expires_at=None,
            fszet=self._repo.fszet_of(user),
        )
        async with self._client_factory(tokens=tokens) as client:
            try:
                # Access-токен не хранится, поэтому каждая синхронизация
                # начинается с обновления. Заодно получаем свежий refresh.
                tokens = await client.refresh()
                await self._repo.update_tokens(
                    user,
                    refresh_token=tokens.refresh_token,
                    access_valid_until=tokens.expires_at,
                    fszet=tokens.fszet,
                )
                raw = await client.get_schedule(days, user.group_uids, org_uid=user.org_uid)
            except AuthError as exc:
                await self._repo.mark_failed(user, error=str(exc), deactivate=True)
                raise ReauthRequired(str(exc)) from exc
            except RanepaError as exc:
                await self._repo.mark_failed(user, error=str(exc))
                raise SyncError(str(exc)) from exc

        try:
            schedule = parse_schedule(raw, fetched_at=datetime.now(timezone.utc))
        except ScheduleParseError as exc:
            # Формат ответа изменился: это ломает всех разом, и владелец
            # должен узнать об этом из алерта, а не из жалоб.
            await self._repo.mark_failed(user, error=f"Формат ответа: {exc}")
            raise SyncError(str(exc)) from exc

        # Сравниваем с тем, что лежало в базе: отсюда берутся отмены и
        # переносы (кабинет их не отдаёт), а исчезнувшие пары остаются в
        # снапшоте с пометкой, чтобы календарь не «воскресил» их.
        previous = await self._repo.load_schedule(user, since=days[0], until=days[-1])
        diff = diff_schedules(previous, schedule)
        merged = diff.merged

        await self._repo.save_schedule(user, merged)
        bumped = await self._repo.bump_revisions(user, merged)
        await self._repo.mark_synced(user, when=merged.fetched_at or datetime.now(timezone.utc))
        log.info(
            "Синхронизировано: пользователь %s, дней %d, пар %d, изменений %d, ревизий %d",
            user.telegram_id,
            len(merged.days),
            len(merged.lessons),
            len(diff.changes),
            bumped,
        )
        return SyncResult(
            schedule=merged,
            lessons_count=sum(1 for lesson in merged.lessons if not lesson.cancelled),
            changes=diff.changes,
        )


MOSCOW_OFFSET = timezone(timedelta(hours=3))

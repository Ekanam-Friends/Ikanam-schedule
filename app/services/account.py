"""Подключение личного кабинета к аккаунту в Telegram.

Единственное место в проекте, через которое проходит пароль. Он приходит
аргументом, уходит в `login()` и больше нигде не появляется: не логируется,
не сохраняется, не попадает в исключения. То, что остаётся после этой функции,
— refresh-токен в зашифрованном виде и учебные идентификаторы.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.db.models import User
from app.db.repo import UserRepository
from app.ranepa.client import AuthError, BlockedError, RanepaClient, RanepaError
from app.ranepa.models import StudentProfile
from app.ranepa.parser import ScheduleParseError, parse_student_profile
from app.services.sync import ScheduleSyncService, SyncError

log = logging.getLogger(__name__)


class ConnectError(Exception):
    """Подключить кабинет не удалось; текст пригоден для показа пользователю."""


class WrongCredentials(ConnectError):
    pass


class CabinetUnavailable(ConnectError):
    pass


@dataclass(frozen=True, slots=True)
class ConnectResult:
    user: User
    profile: StudentProfile
    lessons_count: int
    """Сколько пар удалось забрать сразу; 0 не ошибка — бывают каникулы."""


class AccountService:
    def __init__(
        self,
        repo: UserRepository,
        sync: ScheduleSyncService,
        *,
        client_factory=RanepaClient,
    ) -> None:
        self._repo = repo
        self._sync = sync
        self._client_factory = client_factory

    async def connect(self, telegram_id: int, login: str, password: str) -> ConnectResult:
        async with self._client_factory() as client:
            try:
                tokens = await client.login(login, password)
                raw_groups = await client.get_student_groups()
            except BlockedError as exc:
                raise CabinetUnavailable(
                    "Кабинет отклонил запрос. Обычно это временно — попробуйте через несколько минут."
                ) from exc
            except AuthError as exc:
                raise WrongCredentials("Кабинет не принял логин или пароль.") from exc
            except RanepaError as exc:
                raise CabinetUnavailable(
                    "Личный кабинет сейчас недоступен. Попробуйте позже."
                ) from exc

        try:
            profile = parse_student_profile(raw_groups)
        except ScheduleParseError as exc:
            # Вход прошёл, а учебных данных нет: сотрудник, абитуриент, или
            # кабинет сменил формат. Токен не сохраняем — синхронизировать нечего.
            log.warning("Нет учебных данных после входа: %s", exc)
            raise ConnectError(
                "Вход выполнен, но в кабинете не нашлось учебной группы. "
                "Бот работает только с расписанием студентов."
            ) from exc

        user = await self._repo.connect(
            telegram_id,
            refresh_token=tokens.refresh_token,
            access_valid_until=tokens.expires_at,
            profile=profile,
        )

        # Первая синхронизация — сразу, чтобы /today заработал в ту же минуту.
        # Её неудача не отменяет подключение: токен уже сохранён, ночью
        # планировщик попробует снова.
        lessons_count = 0
        try:
            result = await self._sync.sync_user(user)
            lessons_count = result.lessons_count
        except SyncError as exc:
            log.warning("Первичная синхронизация не удалась для %s: %s", telegram_id, exc)

        return ConnectResult(user=user, profile=profile, lessons_count=lessons_count)

    async def disconnect(self, telegram_id: int) -> bool:
        return await self._repo.delete(telegram_id)

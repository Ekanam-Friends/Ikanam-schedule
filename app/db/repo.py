"""Доступ к данным пользователей и снапшотам расписания.

Репозиторий — единственное место, где код знает про SQLAlchemy. Обработчики
бота и сервисы работают с доменными моделями и не пишут запросов: когда база
поменяется (а SQLite на разработке и PostgreSQL на проде — уже две базы),
править придётся один модуль.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import CredentialsCipher, new_feed_token
from app.db.models import LessonRevision, ScheduleSnapshot, User
from app.ranepa.models import DaySchedule, Lesson, LessonFormat, Schedule, StudentProfile


class UserRepository:
    def __init__(self, session: AsyncSession, cipher: CredentialsCipher) -> None:
        self._session = session
        self._cipher = cipher

    # --- Пользователи ---

    async def get(self, telegram_id: int) -> User | None:
        return await self._session.get(User, telegram_id)

    async def get_or_create(self, telegram_id: int) -> User:
        user = await self.get(telegram_id)
        if user is None:
            user = User(telegram_id=telegram_id, feed_token=new_feed_token())
            self._session.add(user)
            await self._session.flush()
        return user

    async def get_by_feed_token(self, token: str) -> User | None:
        result = await self._session.execute(select(User).where(User.feed_token == token))
        return result.scalar_one_or_none()

    async def list_active(self) -> list[User]:
        result = await self._session.execute(
            select(User).where(User.is_active.is_(True), User.refresh_token_encrypted.is_not(None))
        )
        return list(result.scalars())

    async def connect(
        self,
        telegram_id: int,
        *,
        refresh_token: str,
        access_valid_until: datetime | None,
        profile: StudentProfile,
        fszet: str | None = None,
    ) -> User:
        """Сохранить результат успешного входа. Пароля здесь нет — и не будет."""
        user = await self.get_or_create(telegram_id)
        user.refresh_token_encrypted = self._cipher.encrypt(refresh_token)
        user.fszet_encrypted = self._cipher.encrypt(fszet) if fszet else None
        user.access_valid_until = access_valid_until
        user.connected_at = datetime.now(timezone.utc)
        user.org_uid = profile.org_uid
        user.student_uid = profile.student_uid
        user.group_name = profile.group_name
        user.group_uids = profile.group_uids
        user.is_active = True
        user.consecutive_failures = 0
        user.last_sync_error = None
        await self._session.flush()
        return user

    def refresh_token_of(self, user: User) -> str | None:
        if not user.refresh_token_encrypted:
            return None
        return self._cipher.decrypt(user.refresh_token_encrypted)

    def fszet_of(self, user: User) -> str | None:
        if not user.fszet_encrypted:
            return None
        return self._cipher.decrypt(user.fszet_encrypted)

    async def update_tokens(
        self,
        user: User,
        *,
        refresh_token: str,
        access_valid_until: datetime | None,
        fszet: str | None = None,
    ) -> None:
        # Кабинет выдаёт новую пару на каждое обновление; старый refresh-токен
        # после этого мёртв, так что не сохранить новый — значит потерять доступ.
        user.refresh_token_encrypted = self._cipher.encrypt(refresh_token)
        user.access_valid_until = access_valid_until
        if fszet:
            user.fszet_encrypted = self._cipher.encrypt(fszet)
        await self._session.flush()

    async def mark_synced(self, user: User, *, when: datetime) -> None:
        user.last_sync_at = when
        user.last_sync_error = None
        user.consecutive_failures = 0
        await self._session.flush()

    async def mark_failed(self, user: User, *, error: str, deactivate: bool = False) -> None:
        user.last_sync_error = error[:500]
        user.consecutive_failures += 1
        if deactivate:
            # Токен больше не принимают: дальнейшие попытки только раздражают
            # кабинет. Данные не трогаем — снапшоты продолжают отдаваться в фид.
            user.is_active = False
            user.refresh_token_encrypted = None
            user.fszet_encrypted = None
        await self._session.flush()

    async def rotate_feed_token(self, user: User) -> str:
        user.feed_token = new_feed_token()
        await self._session.flush()
        return user.feed_token

    async def delete(self, telegram_id: int) -> bool:
        """`/logout`: удалить пользователя и всё производное — без остатка."""
        user = await self.get(telegram_id)
        if user is None:
            return False
        await self._session.execute(
            delete(ScheduleSnapshot).where(ScheduleSnapshot.user_id == telegram_id)
        )
        await self._session.execute(
            delete(LessonRevision).where(LessonRevision.user_id == telegram_id)
        )
        await self._session.delete(user)
        await self._session.flush()
        return True

    # --- Снапшоты расписания ---

    async def save_schedule(self, user: User, schedule: Schedule) -> None:
        """Заменить снапшоты дней, пришедших в выгрузке.

        Дни, которых в выгрузке нет, не трогаются: если запросили две недели,
        прошлый месяц не должен исчезнуть из истории и из фида.
        """
        fetched_at = schedule.fetched_at or datetime.now(timezone.utc)
        for day in schedule.days:
            result = await self._session.execute(
                select(ScheduleSnapshot).where(
                    ScheduleSnapshot.user_id == user.telegram_id,
                    ScheduleSnapshot.day == day.day,
                )
            )
            snapshot = result.scalar_one_or_none()
            payload = [_lesson_to_dict(lesson) for lesson in day.lessons]
            if snapshot is None:
                self._session.add(
                    ScheduleSnapshot(
                        user_id=user.telegram_id,
                        day=day.day,
                        lessons=payload,
                        fetched_at=fetched_at,
                    )
                )
            else:
                snapshot.lessons = payload
                snapshot.fetched_at = fetched_at
        await self._session.flush()

    async def load_schedule(
        self, user: User, *, since: date | None = None, until: date | None = None
    ) -> Schedule:
        """Собрать расписание из снапшотов за период (включительно)."""
        query = select(ScheduleSnapshot).where(ScheduleSnapshot.user_id == user.telegram_id)
        if since is not None:
            query = query.where(ScheduleSnapshot.day >= since)
        if until is not None:
            query = query.where(ScheduleSnapshot.day <= until)
        result = await self._session.execute(query.order_by(ScheduleSnapshot.day))
        snapshots = list(result.scalars())

        days = [
            DaySchedule(day=s.day, lessons=[_lesson_from_dict(raw) for raw in s.lessons])
            for s in snapshots
        ]
        fetched_at = max((s.fetched_at for s in snapshots), default=None)
        return Schedule(days=days, fetched_at=fetched_at)


def _lesson_to_dict(lesson: Lesson) -> dict:
    data = asdict(lesson)
    data["start"] = lesson.start.isoformat()
    data["end"] = lesson.end.isoformat()
    data["lesson_format"] = lesson.lesson_format.value
    return data


def _lesson_from_dict(data: dict) -> Lesson:
    return Lesson(
        subject=data["subject"],
        start=datetime.fromisoformat(data["start"]),
        end=datetime.fromisoformat(data["end"]),
        teacher=data.get("teacher"),
        room=data.get("room"),
        building=data.get("building"),
        lesson_format=LessonFormat(data.get("lesson_format", "unknown")),
        lesson_type=data.get("lesson_type"),
        cancelled=bool(data.get("cancelled", False)),
        source_id=data.get("source_id"),
    )

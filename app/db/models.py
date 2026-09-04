"""Схема базы данных.

Три сущности, и каждая существует ради конкретного решения, принятого на старте:

* :class:`User` — кого синхронизируем и по какой ссылке отдаём календарь;
* :class:`ScheduleSnapshot` — последняя *удачная* выгрузка. Она нужна дважды:
  чтобы не обнулять календарь, когда личный кабинет недоступен, и чтобы было с
  чем сравнить новую выгрузку и сказать «пару перенесли»;
* :class:`LessonRevision` — счётчик правок каждой пары. Календари принимают
  обновление события, только если `SEQUENCE` вырос, поэтому его приходится
  помнить между выгрузками.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    """Студент, подключивший аккаунт."""

    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    # --- Учётные данные личного кабинета ---
    # Оба поля зашифрованы: логин — это персональные данные, и в дампе базы ему
    # рядом с расписанием делать нечего.
    login_encrypted: Mapped[str | None] = mapped_column(Text, default=None)
    password_encrypted: Mapped[str | None] = mapped_column(Text, default=None)

    # --- Подписка на календарь ---
    feed_token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    """Секрет в ссылке на ICS-фид. Он же и аутентификация: календарь ходит по
    ссылке без входа. Поэтому токен можно перевыпустить, не трогая аккаунт, —
    старая ссылка сразу перестанет работать."""

    # --- Настройки уведомлений ---
    morning_digest_at: Mapped[str | None] = mapped_column(String(5), default="08:00")
    """Время утренней сводки в московском времени, «ЧЧ:ММ». `None` — не слать."""

    reminder_minutes: Mapped[int | None] = mapped_column(Integer, default=None)
    """За сколько минут напоминать о паре внутри календаря. `None` — без напоминаний."""

    notify_on_change: Mapped[bool] = mapped_column(Boolean, default=True)

    # --- Состояние синхронизации ---
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    """Снимается, когда пароль перестал подходить: продолжать долбиться в чужой
    личный кабинет неверными данными — верный способ получить блокировку."""

    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_sync_error: Mapped[str | None] = mapped_column(Text, default=None)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    snapshots: Mapped[list[ScheduleSnapshot]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    revisions: Mapped[list[LessonRevision]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    @property
    def has_credentials(self) -> bool:
        return bool(self.login_encrypted and self.password_encrypted)


class ScheduleSnapshot(Base):
    """Расписание одного дня, каким его в последний раз отдал личный кабинет."""

    __tablename__ = "schedule_snapshots"
    __table_args__ = (UniqueConstraint("user_id", "day", name="uq_snapshot_user_day"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), index=True
    )
    day: Mapped[date] = mapped_column(Date, index=True)

    lessons: Mapped[list[dict]] = mapped_column(JSONB)
    """Пары дня в том виде, в каком их нормализовал парсер.

    Хранится именно нормализованный вид, а не сырой ответ ЛК: сырой ответ — это
    персональные данные в неизвестном объёме, и лежать он должен ровно столько,
    сколько нужно для отладки, а не постоянно."""

    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped[User] = relationship(back_populates="snapshots")


class LessonRevision(Base):
    """Сколько раз менялась конкретная пара — номер ревизии для календаря."""

    __tablename__ = "lesson_revisions"
    __table_args__ = (UniqueConstraint("user_id", "lesson_uid", name="uq_revision_user_lesson"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), index=True
    )
    lesson_uid: Mapped[str] = mapped_column(String(64), index=True)

    sequence: Mapped[int] = mapped_column(Integer, default=0)
    """Значение `SEQUENCE` в событии календаря. Растёт при каждом содержательном
    изменении пары; если его не увеличить, часть клиентов проигнорирует правку."""

    content_hash: Mapped[str] = mapped_column(String(64))
    """Отпечаток пары. По нему видно, изменилось ли что-то на самом деле, —
    иначе `SEQUENCE` рос бы каждую ночь и календари перекачивали бы всё подряд."""

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    user: Mapped[User] = relationship(back_populates="revisions")

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
    JSON,
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

JsonColumn = JSON().with_variant(JSONB, "postgresql")
"""JSON, который работает и в PostgreSQL (как JSONB), и в SQLite.

SQLite нужен для локальной разработки и тестов: поднимать Postgres ради
проверки форматирования сообщения — лишнее трение, из-за которого тесты
перестают запускать."""


class Base(DeclarativeBase):
    pass


class User(Base):
    """Студент, подключивший аккаунт."""

    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    # --- Доступ к личному кабинету ---
    # Пароля здесь нет и быть не должно. Кабинет умеет продлевать доступ по
    # refresh-токену (`auth/refresh`), поэтому пароль нужен ровно один раз — в
    # момент подключения, — живёт в памяти секунды и никуда не записывается.
    #
    # Разница не косметическая: refresh-токен даёт доступ к одному кабинету и
    # обнуляется одной командой, а пароль почти наверняка используется человеком
    # ещё в нескольких местах.
    refresh_token_encrypted: Mapped[str | None] = mapped_column(Text, default=None)

    fszet_encrypted: Mapped[str | None] = mapped_column(Text, default=None)
    """Второй секрет входа: без заголовка `fszet` кабинет не продлевает токен.
    Хранится так же, как refresh-токен, и удаляется вместе с ним."""

    access_valid_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    """До какого момента действует последний полученный access-токен.

    Сам access-токен не хранится: он живёт минуты и запрашивается заново на
    каждую синхронизацию. В базе от него нужен только срок — чтобы не ходить за
    обновлением чаще, чем необходимо."""

    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    # --- Учебные данные, нужные для запроса расписания ---
    # Кабинет требует `uid_org` и все группы в `filter[]`; запрашивать их у
    # `student-groups` перед каждой синхронизацией — лишний запрос к чужому
    # серверу ради данных, которые меняются раз в семестр.
    org_uid: Mapped[str | None] = mapped_column(String(64), default=None)
    student_uid: Mapped[str | None] = mapped_column(String(64), default=None)
    group_name: Mapped[str | None] = mapped_column(String(64), default=None)
    """Название основной группы — для «Подключено: ЭИ-25» и ничего больше."""

    group_uids: Mapped[list[str] | None] = mapped_column(JsonColumn, default=None)

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
    """Снимается, когда refresh-токен перестал приниматься: продолжать долбиться
    в чужой личный кабинет мёртвым токеном — верный способ получить блокировку.
    Такого пользователя нужно попросить подключить кабинет заново."""

    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_sync_error: Mapped[str | None] = mapped_column(Text, default=None)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    snapshots: Mapped[list[ScheduleSnapshot]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    revisions: Mapped[list[LessonRevision]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    @property
    def is_connected(self) -> bool:
        """Есть ли у бота действующий доступ к кабинету этого человека."""
        return bool(self.refresh_token_encrypted)


class DialogState(Base):
    """Состояние диалога с пользователем (FSM aiogram).

    В памяти процесса его держать нельзя: бот перезапускается при деплое, и
    человек, набравший пароль через секунду после рестарта, получает молчание
    вместо ответа — а пароль остаётся висеть в чате. Проверено на себе.
    """

    __tablename__ = "dialog_states"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    """`bot_id:chat_id:user_id` — так aiogram различает диалоги."""

    state: Mapped[str | None] = mapped_column(String(128), default=None)
    data: Mapped[dict] = mapped_column(JsonColumn, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ScheduleSnapshot(Base):
    """Расписание одного дня, каким его в последний раз отдал личный кабинет."""

    __tablename__ = "schedule_snapshots"
    __table_args__ = (UniqueConstraint("user_id", "day", name="uq_snapshot_user_day"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), index=True
    )
    day: Mapped[date] = mapped_column(Date, index=True)

    lessons: Mapped[list[dict]] = mapped_column(JsonColumn)
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

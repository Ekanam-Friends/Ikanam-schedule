"""Рассылки владельца и ответы на них."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Broadcast, BroadcastAnswer, User

KIND_TEXT = "text"
KIND_ANSWER = "answer"


@dataclass(frozen=True, slots=True)
class AnswerRow:
    id: int
    user_id: int
    question: str
    answer: str


class BroadcastStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, kind: str, text: str) -> Broadcast:
        broadcast = Broadcast(kind=kind, text=text)
        self._session.add(broadcast)
        await self._session.flush()
        return broadcast

    async def get(self, broadcast_id: int) -> Broadcast | None:
        return await self._session.get(Broadcast, broadcast_id)

    async def claim_for_sending(self, broadcast_id: int) -> bool:
        """Отметить черновик отправленным, если его ещё никто не отправил.

        Одним UPDATE с условием: два быстрых нажатия «Отправить» не должны
        разослать сообщение дважды, а проверка и запись по отдельности это
        допускают."""
        result = await self._session.execute(
            update(Broadcast)
            .where(Broadcast.id == broadcast_id, Broadcast.sent_at.is_(None))
            .values(sent_at=datetime.now(timezone.utc))
        )
        return result.rowcount == 1

    async def drop(self, broadcast: Broadcast) -> None:
        await self._session.delete(broadcast)
        await self._session.flush()

    async def recipient_ids(self) -> list[int]:
        """Все, кто есть в базе, — подключённые и те, у кого доступ отвалился."""
        result = await self._session.execute(select(User.telegram_id).order_by(User.telegram_id))
        return list(result.scalars())

    async def add_answer(self, broadcast_id: int, user_id: int, text: str) -> None:
        self._session.add(BroadcastAnswer(broadcast_id=broadcast_id, user_id=user_id, text=text))
        await self._session.flush()

    async def pending_answers(self) -> list[AnswerRow]:
        result = await self._session.execute(
            select(BroadcastAnswer, Broadcast.text)
            .join(Broadcast, Broadcast.id == BroadcastAnswer.broadcast_id)
            .order_by(BroadcastAnswer.id)
        )
        return [
            AnswerRow(id=answer.id, user_id=answer.user_id, question=question, answer=answer.text)
            for answer, question in result.all()
        ]

    async def delete_answers(self, ids: list[int]) -> None:
        if ids:
            await self._session.execute(delete(BroadcastAnswer).where(BroadcastAnswer.id.in_(ids)))

"""관심 목록(회원 단위)."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


class InterestKind(StrEnum):
    COMPLEX = "complex"
    AUCTION = "auction"


@dataclass(frozen=True)
class Interest:
    target_id: str
    added_at: datetime


@dataclass(frozen=True)
class AddResult:
    added_at: datetime | None  # 넣었거나 이미 있던 등록 시각. 넣지 못했으면 None
    member_alive: bool  # 탈퇴하지 않은 회원인지(탈퇴했으면 넣지 않는다)


# 이미 있으면 그대로 두고(멱등), 없으면 한도 안이고 탈퇴하지 않은 회원일 때만 넣는다.
# 사용자 행을 FOR SHARE로 잠가 탈퇴(accounts.withdraw가 사용자 행을 먼저 FOR NO KEY UPDATE)와 차례를 맞춘다:
# 탈퇴가 먼저면 커밋을 기다렸다가 deleted_at을 보고 넣지 않고, 등록이 먼저면 탈퇴가 커밋을 기다렸다가 그 행까지 지운다.
# 회원이 살아 있는지(alive)도 돌려줘서, 넣지 못한 까닭이 한도 초과인지 탈퇴인지 가른다
_ADD = text(
    """
    WITH alive AS (
        SELECT 1 FROM users WHERE id = :user AND deleted_at IS NULL FOR SHARE
    ), existing AS (
        SELECT created_at FROM user_interests WHERE user_id = :user AND kind = :kind AND target_id = :target
    ), inserted AS (
        INSERT INTO user_interests (user_id, kind, target_id)
        SELECT :user, :kind, :target
        WHERE NOT EXISTS (SELECT 1 FROM existing)
          AND EXISTS (SELECT 1 FROM alive)
          AND (SELECT count(*) FROM user_interests WHERE user_id = :user AND kind = :kind) < :max
        ON CONFLICT DO NOTHING
        RETURNING created_at
    )
    SELECT (SELECT created_at FROM existing UNION ALL SELECT created_at FROM inserted LIMIT 1) AS added_at,
           EXISTS (SELECT 1 FROM alive) AS alive
    """
)
_EXISTING = text("SELECT created_at FROM user_interests WHERE user_id = :user AND kind = :kind AND target_id = :target")
_REMOVE = text("DELETE FROM user_interests WHERE user_id = :user AND kind = :kind AND target_id = :target")
_LIST = text(
    """
    SELECT target_id, created_at FROM user_interests WHERE user_id = :user AND kind = :kind
    ORDER BY created_at DESC, target_id
    """
)


class PostgresInterestRepository:
    def __init__(self, engine: AsyncEngine):
        self._engine = engine

    async def add(self, user_id: UUID, kind: InterestKind, target: str, *, max_items: int) -> AddResult:
        """등록 시각과 회원이 살아 있는지. 한도를 넘었거나 탈퇴한 회원이면 등록 시각이 None."""
        params = {"user": user_id, "kind": kind.value, "target": target, "max": max_items}
        async with self._engine.begin() as conn:
            row = (await conn.execute(_ADD, params)).one()
        if not row.alive:
            return AddResult(None, member_alive=False)
        added = row.added_at
        if added is None:  # 같은 대상을 동시에 넣어 충돌했으면 이미 들어간 시각을 돌려준다
            async with self._engine.connect() as conn:
                added = (await conn.execute(_EXISTING, params)).scalar()
        return AddResult(added, member_alive=True)

    async def remove(self, user_id: UUID, kind: InterestKind, target: str) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(_REMOVE, {"user": user_id, "kind": kind.value, "target": target})

    async def list(self, user_id: UUID, kind: InterestKind) -> list[Interest]:
        """최근에 등록한 것부터."""
        async with self._engine.connect() as conn:
            rows = await conn.execute(_LIST, {"user": user_id, "kind": kind.value})
            return [Interest(r.target_id, r.created_at) for r in rows]

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


# 이미 있으면 그대로 두고(멱등), 없으면 한도 안이고 탈퇴하지 않은 회원일 때만 넣는다.
# 사용자 행을 FOR SHARE로 잠가 탈퇴(accounts.withdraw가 사용자 행을 먼저 FOR UPDATE)와 차례를 맞춘다:
# 탈퇴가 먼저면 커밋을 기다렸다가 deleted_at을 보고 넣지 않고, 등록이 먼저면 탈퇴가 커밋을 기다렸다가 그 행까지 지운다
_ADD = text(
    """
    WITH existing AS (
        SELECT created_at FROM user_interests WHERE user_id = :user AND kind = :kind AND target_id = :target
    ), inserted AS (
        INSERT INTO user_interests (user_id, kind, target_id)
        SELECT :user, :kind, :target
        WHERE NOT EXISTS (SELECT 1 FROM existing)
          AND EXISTS (SELECT 1 FROM users WHERE id = :user AND deleted_at IS NULL FOR SHARE)
          AND (SELECT count(*) FROM user_interests WHERE user_id = :user AND kind = :kind) < :max
        ON CONFLICT DO NOTHING
        RETURNING created_at
    )
    SELECT created_at FROM existing UNION ALL SELECT created_at FROM inserted
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

    async def add(self, user_id: UUID, kind: InterestKind, target: str, *, max_items: int) -> datetime | None:
        """등록 시각. 한도를 넘어 넣지 못했으면 None."""
        params = {"user": user_id, "kind": kind.value, "target": target, "max": max_items}
        async with self._engine.begin() as conn:
            added = (await conn.execute(_ADD, params)).scalar()
        if added is None:  # 같은 대상을 동시에 넣어 충돌했으면 이미 들어간 시각을 돌려준다
            async with self._engine.connect() as conn:
                added = (await conn.execute(_EXISTING, params)).scalar()
        return added

    async def remove(self, user_id: UUID, kind: InterestKind, target: str) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(_REMOVE, {"user": user_id, "kind": kind.value, "target": target})

    async def list(self, user_id: UUID, kind: InterestKind) -> list[Interest]:
        """최근에 등록한 것부터."""
        async with self._engine.connect() as conn:
            rows = await conn.execute(_LIST, {"user": user_id, "kind": kind.value})
            return [Interest(r.target_id, r.created_at) for r in rows]

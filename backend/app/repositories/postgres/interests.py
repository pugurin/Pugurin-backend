"""관심 목록(기기 단위). 로그인 전이라 `X-Device-Id`에 묶는다."""

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


# 이미 있으면 그대로 두고(멱등), 없으면 한도 안일 때만 넣는다
_ADD = text(
    """
    WITH existing AS (
        SELECT created_at FROM device_interests WHERE device_id = :device AND kind = :kind AND target_id = :target
    ), inserted AS (
        INSERT INTO device_interests (device_id, kind, target_id)
        SELECT :device, :kind, :target
        WHERE NOT EXISTS (SELECT 1 FROM existing)
          AND (SELECT count(*) FROM device_interests WHERE device_id = :device AND kind = :kind) < :max
        ON CONFLICT DO NOTHING
        RETURNING created_at
    )
    SELECT created_at FROM existing UNION ALL SELECT created_at FROM inserted
    """
)
_EXISTING = text(
    "SELECT created_at FROM device_interests WHERE device_id = :device AND kind = :kind AND target_id = :target"
)
_REMOVE = text("DELETE FROM device_interests WHERE device_id = :device AND kind = :kind AND target_id = :target")
_LIST = text(
    """
    SELECT target_id, created_at FROM device_interests WHERE device_id = :device AND kind = :kind
    ORDER BY created_at DESC, target_id
    """
)


class PostgresInterestRepository:
    def __init__(self, engine: AsyncEngine):
        self._engine = engine

    async def add(self, device: UUID, kind: InterestKind, target: str, *, max_items: int) -> datetime | None:
        """등록 시각. 한도를 넘어 넣지 못했으면 None."""
        params = {"device": device, "kind": kind.value, "target": target, "max": max_items}
        async with self._engine.begin() as conn:
            added = (await conn.execute(_ADD, params)).scalar()
        if added is None:  # 같은 대상을 동시에 넣어 충돌했으면 이미 들어간 시각을 돌려준다
            async with self._engine.connect() as conn:
                added = (await conn.execute(_EXISTING, params)).scalar()
        return added

    async def remove(self, device: UUID, kind: InterestKind, target: str) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(_REMOVE, {"device": device, "kind": kind.value, "target": target})

    async def list(self, device: UUID, kind: InterestKind) -> list[Interest]:
        """최근에 등록한 것부터."""
        async with self._engine.connect() as conn:
            rows = await conn.execute(_LIST, {"device": device, "kind": kind.value})
            return [Interest(r.target_id, r.created_at) for r in rows]

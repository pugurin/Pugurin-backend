"""관심 목록(회원 단위)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


class InterestKind(StrEnum):
    COMPLEX = "complex"
    AUCTION = "auction"
    REGION = "region"  # target_id는 법정동 코드(10자리)


@dataclass(frozen=True)
class Interest:
    target_id: str
    added_at: datetime


@dataclass(frozen=True)
class RegionInterest:
    code: str
    name: str
    sigungu_name: str
    lat: float | None
    lng: float | None
    added_at: datetime


# 이미 있으면 그대로 두고(멱등), 없으면 한도 안일 때만 넣는다
_ADD = text(
    """
    WITH existing AS (
        SELECT created_at FROM user_interests WHERE user_id = :user AND kind = :kind AND target_id = :target
    ), inserted AS (
        INSERT INTO user_interests (user_id, kind, target_id)
        SELECT :user, :kind, :target
        WHERE NOT EXISTS (SELECT 1 FROM existing)
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
_IS_DONG = text("SELECT EXISTS (SELECT 1 FROM regions WHERE code = :code AND level = 'dong')")
_LIST_REGIONS = text(
    """
    SELECT d.code, d.name, g.name AS sigungu_name, ST_Y(d.centroid) AS lat, ST_X(d.centroid) AS lng, ui.created_at
    FROM user_interests ui
    JOIN regions d ON d.code = ui.target_id AND d.level = 'dong'
    JOIN regions g ON g.code = d.parent_code
    WHERE ui.user_id = :user AND ui.kind = 'region'
    ORDER BY ui.created_at DESC, d.code
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

    async def is_dong(self, code: str) -> bool:
        async with self._engine.connect() as conn:
            return bool((await conn.execute(_IS_DONG, {"code": code})).scalar())

    async def list_regions(self, user_id: UUID) -> list[RegionInterest]:
        """관심 지역을 동·시군구 이름, 중심점과 함께. 최근에 등록한 것부터."""
        async with self._engine.connect() as conn:
            rows = await conn.execute(_LIST_REGIONS, {"user": user_id})
            return [RegionInterest(r.code, r.name, r.sigungu_name, r.lat, r.lng, r.created_at) for r in rows]

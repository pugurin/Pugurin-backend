"""알림함(회원 단위). 리포트 배치가 넣고, 알림함 API가 읽는다."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

KIND_INTEREST_REPORT = "interest_report"


@dataclass(frozen=True)
class Notification:
    id: UUID
    kind: str
    period_start: date
    title: str
    summary: str
    payload: dict
    read_at: datetime | None
    created_at: datetime


# 같은 주를 다시 돌려도 한 통만 남긴다(이미 만든 리포트는 덮지 않는다).
# 탈퇴한 회원이나 리포트 받기를 끈 회원에겐 넣지 않는다(대상 조회 뒤에 끈 경우도 여기서 거른다).
# FOR SHARE로 행을 잡아 탈퇴·설정 변경 트랜잭션과 겹쳐도 어긋나지 않게 한다
_INSERT = text(
    """
    INSERT INTO notifications (id, user_id, kind, period_start, title, summary, payload, created_at)
    SELECT :id, :user, :kind, :period, :title, :summary, CAST(:payload AS jsonb), :now
    WHERE EXISTS (
        SELECT 1 FROM users
        WHERE id = :user AND deleted_at IS NULL AND coalesce((settings->>'weekly_report')::boolean, true)
        FOR SHARE
    )
    ON CONFLICT (user_id, kind, period_start) DO NOTHING
    """
)
_COLUMNS = "id, kind, period_start, title, summary, payload, read_at, created_at"
_LIST = text(
    f"""
    SELECT {_COLUMNS} FROM notifications WHERE user_id = :user
    ORDER BY created_at DESC, id OFFSET :offset LIMIT :limit
    """
)
_COUNTS = text(
    """
    SELECT count(*) AS total, count(*) FILTER (WHERE read_at IS NULL) AS unread
    FROM notifications WHERE user_id = :user
    """
)
_GET = text(f"SELECT {_COLUMNS} FROM notifications WHERE user_id = :user AND id = :id")
# 이미 읽은 것은 읽은 시각을 그대로 둔다
_MARK_ALL = text("UPDATE notifications SET read_at = :now WHERE user_id = :user AND read_at IS NULL")
_MARK_IDS = text("UPDATE notifications SET read_at = :now WHERE user_id = :user AND read_at IS NULL AND id = ANY(:ids)")
_DELETE_OLDER = text("DELETE FROM notifications WHERE created_at < :cutoff")


def _row(r) -> Notification:
    return Notification(r.id, r.kind, r.period_start, r.title, r.summary, r.payload, r.read_at, r.created_at)


class PostgresNotificationRepository:
    def __init__(self, engine: AsyncEngine):
        self._engine = engine

    async def insert_report(
        self, user_id: UUID, period_start: date, title: str, summary: str, payload: dict, now: datetime
    ) -> bool:
        """새로 넣었으면 True, 같은 주 리포트가 이미 있거나 탈퇴했거나 리포트를 끈 회원이면 False."""
        params = {
            "id": uuid4(),
            "user": user_id,
            "kind": KIND_INTEREST_REPORT,
            "period": period_start,
            "title": title,
            "summary": summary,
            "payload": json.dumps(payload, ensure_ascii=False),
            "now": now,
        }
        async with self._engine.begin() as conn:
            return (await conn.execute(_INSERT, params)).rowcount == 1

    async def list(self, user_id: UUID, offset: int, limit: int) -> list[Notification]:
        """최신순."""
        async with self._engine.connect() as conn:
            rows = await conn.execute(_LIST, {"user": user_id, "offset": offset, "limit": limit})
            return [_row(r) for r in rows]

    async def counts(self, user_id: UUID) -> tuple[int, int]:
        """(전체, 안 읽은) 개수."""
        async with self._engine.connect() as conn:
            r = (await conn.execute(_COUNTS, {"user": user_id})).one()
            return r.total, r.unread

    async def unread_count(self, user_id: UUID) -> int:
        return (await self.counts(user_id))[1]

    async def get(self, user_id: UUID, notification_id: UUID) -> Notification | None:
        async with self._engine.connect() as conn:
            r = (await conn.execute(_GET, {"user": user_id, "id": notification_id})).first()
            return _row(r) if r else None

    async def mark_read(self, user_id: UUID, ids: list[UUID] | None, now: datetime) -> None:
        """ids가 None이면 전부 읽음. 남의 알림 id는 무시한다."""
        async with self._engine.begin() as conn:
            if ids is None:
                await conn.execute(_MARK_ALL, {"user": user_id, "now": now})
            else:
                await conn.execute(_MARK_IDS, {"user": user_id, "now": now, "ids": ids})

    async def delete_older_than(self, cutoff: datetime) -> int:
        """보관 기간이 지난 알림을 지운다. 지운 개수."""
        async with self._engine.begin() as conn:
            return (await conn.execute(_DELETE_OLDER, {"cutoff": cutoff})).rowcount

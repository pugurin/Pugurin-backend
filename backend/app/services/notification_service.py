"""알림함. 로그인한 회원의 알림만 읽는다."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from app.core.dates import Clock
from app.core.errors import data_not_ready, not_found, validation_error
from app.repositories.postgres.notifications import Notification, PostgresNotificationRepository

NOT_READY = "알림함은 서버 DB가 있어야 쓸 수 있어요"


@dataclass(frozen=True)
class NotificationPage:
    items: list[Notification]
    total: int
    unread_count: int


class NotificationService:
    def __init__(self, repo: PostgresNotificationRepository | None, clock: Clock):
        self._repo = repo
        self._clock = clock

    def _ready(self) -> PostgresNotificationRepository:
        if self._repo is None:
            raise data_not_ready(NOT_READY)
        return self._repo

    async def list(self, user_id: UUID, page: int, page_size: int) -> NotificationPage:
        repo = self._ready()
        items = await repo.list(user_id, (page - 1) * page_size, page_size)
        total, unread = await repo.counts(user_id)
        return NotificationPage(items, total, unread)

    async def get(self, user_id: UUID, notification_id: UUID) -> Notification:
        found = await self._ready().get(user_id, notification_id)
        if found is None:  # 없거나 남의 알림
            raise not_found("알림을 찾을 수 없어요")
        return found

    async def mark_read(self, user_id: UUID, ids: list[UUID] | None, all_: bool) -> None:
        if all_ == bool(ids):  # 둘 다 있거나 둘 다 없으면 무엇을 읽을지 알 수 없다
            raise validation_error("ids", "ids와 all 중 하나만 보내 주세요")
        await self._ready().mark_read(user_id, None if all_ else ids, self._clock())

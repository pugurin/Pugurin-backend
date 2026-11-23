from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.repositories.postgres.notifications import Notification


class NotificationOut(BaseModel):
    id: UUID
    kind: str
    period_start: date
    title: str
    summary: str
    read_at: datetime | None
    created_at: datetime

    @classmethod
    def from_domain(cls, n: Notification) -> "NotificationOut":
        return cls(
            id=n.id,
            kind=n.kind,
            period_start=n.period_start,
            title=n.title,
            summary=n.summary,
            read_at=n.read_at,
            created_at=n.created_at,
        )


class NotificationDetailOut(NotificationOut):
    payload: dict

    @classmethod
    def from_domain(cls, n: Notification) -> "NotificationDetailOut":
        return cls(**NotificationOut.from_domain(n).model_dump(), payload=n.payload)


class NotificationListMeta(BaseModel):
    page: int
    page_size: int
    total: int
    total_pages: int
    unread_count: int


class NotificationsResponse(BaseModel):
    data: list[NotificationOut]
    meta: NotificationListMeta


class NotificationResponse(BaseModel):
    data: NotificationDetailOut


class NotificationReadIn(BaseModel):
    ids: list[UUID] | None = Field(default=None, max_length=100)  # 이 알림들만 읽음
    all: bool = False  # 전부 읽음

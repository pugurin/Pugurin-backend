"""알림함. 로그인한 회원만 쓴다(access token, 없거나 틀리면 401).

회원마다 응답이 달라서 공용 캐시에 남지 않게 `Cache-Control: private, no-cache`를 붙인다.
"""

import math
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Response, status

from app.routers.deps import ContainerDep, CurrentUser
from app.schemas.notification import (
    NotificationDetailOut,
    NotificationListMeta,
    NotificationOut,
    NotificationReadIn,
    NotificationResponse,
    NotificationsResponse,
)

router = APIRouter(tags=["notifications"])

PRIVATE = "private, no-cache"


@router.get("/notifications", response_model=NotificationsResponse, summary="알림함 (최신순)")
async def list_notifications(
    user: CurrentUser,
    container: ContainerDep,
    response: Response,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=50)] = 20,
) -> NotificationsResponse:
    response.headers["Cache-Control"] = PRIVATE
    result = await container.notification_service.list(user.id, page, page_size)
    return NotificationsResponse(
        data=[NotificationOut.from_domain(n) for n in result.items],
        meta=NotificationListMeta(
            page=page,
            page_size=page_size,
            total=result.total,
            total_pages=math.ceil(result.total / page_size),
            unread_count=result.unread_count,
        ),
    )


# 고정 경로라 `/notifications/{notification_id}`보다 먼저 둔다
@router.post("/notifications/read", status_code=status.HTTP_204_NO_CONTENT, summary="알림 읽음 처리 (ids 또는 all)")
async def read_notifications(body: NotificationReadIn, user: CurrentUser, container: ContainerDep) -> None:
    await container.notification_service.mark_read(user.id, body.ids, body.all)


@router.get(
    "/notifications/{notification_id}", response_model=NotificationResponse, summary="알림 한 통 (payload 포함)"
)
async def get_notification(
    notification_id: UUID, user: CurrentUser, container: ContainerDep, response: Response
) -> NotificationResponse:
    response.headers["Cache-Control"] = PRIVATE
    found = await container.notification_service.get(user.id, notification_id)
    return NotificationResponse(data=NotificationDetailOut.from_domain(found))

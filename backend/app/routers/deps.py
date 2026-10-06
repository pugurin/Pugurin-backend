from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, Request

from app.wiring import Container


def get_container(request: Request) -> Container:
    return request.app.state.container


def require_device_id(
    x_device_id: Annotated[UUID, Header(alias="X-Device-Id", description="앱 설치 단위 UUID")],
) -> UUID:
    return x_device_id


ContainerDep = Annotated[Container, Depends(get_container)]

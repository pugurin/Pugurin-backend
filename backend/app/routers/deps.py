from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, Request

from app.core.errors import data_not_ready
from app.wiring import Container


def get_container(request: Request) -> Container:
    return request.app.state.container


def require_device_id(
    x_device_id: Annotated[UUID, Header(alias="X-Device-Id", description="앱 설치 단위 UUID")],
) -> UUID:
    return x_device_id


ContainerDep = Annotated[Container, Depends(get_container)]


async def require_market_data(container: ContainerDep) -> None:
    """실거래 데이터가 없으면 빈 화면 대신 503 DATA_NOT_READY로 알린다(가짜 데이터로 채우지 않는다)."""
    if not await container.has_market_data():
        raise data_not_ready()

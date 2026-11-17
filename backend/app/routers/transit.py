from typing import Annotated

from fastapi import APIRouter, Query

from app.routers.deps import ContainerDep
from app.schemas.transit import TransitRoutesResponse
from app.services.transit_service import MAX_LIMIT

router = APIRouter(tags=["transit"])

Lat = Annotated[float, Query(ge=-90, le=90, description="위도(WGS84)")]
Lng = Annotated[float, Query(ge=-180, le=180, description="경도(WGS84)")]


@router.get("/transit/routes", response_model=TransitRoutesResponse, summary="대중교통 길찾기 (집→직장, ODsay)")
async def transit_routes(
    container: ContainerDep,
    from_lat: Lat,
    from_lng: Lng,
    to_lat: Lat,
    to_lng: Lng,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT, description="돌려줄 경로 수(소요시간 짧은 순)")] = 3,
) -> TransitRoutesResponse:
    """경로를 줄 수 없으면 200에 `routes: []`와 `unavailable_reason`(too_close·no_route·source_unavailable)."""
    result = await container.transit_service.routes(from_lat, from_lng, to_lat, to_lng, limit)
    return TransitRoutesResponse.from_result(result)

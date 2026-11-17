from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.core.errors import validation_error
from app.repositories.types import AddressHit, ComplexSearchHit, RegionSearchHit, School, StationHit
from app.routers.deps import ContainerDep, require_market_data
from app.schemas.search import (
    AddressResult,
    ComplexResult,
    RegionResult,
    SchoolResult,
    SearchItem,
    SearchResponse,
    StationResult,
)

router = APIRouter(tags=["search"], dependencies=[Depends(require_market_data)])


@router.get("/search", response_model=SearchResponse, summary="동네·지하철역·학교·단지·주소 통합 검색")
async def search(
    container: ContainerDep,
    q: Annotated[str, Query(description="검색어, 2자 이상")],
    limit: Annotated[int, Query(ge=1, le=30)] = 10,
) -> SearchResponse:
    if len(q.strip()) < 2:
        raise validation_error("q", "검색어는 2자 이상이어야 합니다")
    hits = await container.search_service.search(q, limit)
    return SearchResponse(data=[_item(h) for h in hits])


def _item(hit) -> SearchItem:
    match hit:
        case RegionSearchHit():
            return RegionResult.from_domain(hit)
        case StationHit():
            return StationResult.from_domain(hit)
        case School():
            return SchoolResult.from_domain(hit)
        case ComplexSearchHit():
            return ComplexResult.from_domain(hit)
        case AddressHit():
            return AddressResult.from_domain(hit)
    raise TypeError(f"모르는 검색 결과: {type(hit).__name__}")

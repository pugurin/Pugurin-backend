from typing import Annotated

from fastapi import APIRouter, Query

from app.core.errors import validation_error
from app.repositories.types import AddressHit, ComplexSearchHit
from app.routers.deps import ContainerDep
from app.schemas.search import AddressResult, ComplexResult, RegionResult, SearchResponse

router = APIRouter(tags=["search"])


@router.get("/search", response_model=SearchResponse, summary="단지·법정동·주소 통합 검색")
async def search(
    container: ContainerDep,
    q: Annotated[str, Query(description="검색어, 2자 이상")],
    limit: Annotated[int, Query(ge=1, le=30)] = 10,
) -> SearchResponse:
    if len(q.strip()) < 2:
        raise validation_error("q", "검색어는 2자 이상이어야 합니다")
    hits = await container.search_service.search(q, limit)
    items = [
        ComplexResult.from_domain(h)
        if isinstance(h, ComplexSearchHit)
        else AddressResult.from_domain(h)
        if isinstance(h, AddressHit)
        else RegionResult.from_domain(h)
        for h in hits
    ]
    return SearchResponse(data=items)

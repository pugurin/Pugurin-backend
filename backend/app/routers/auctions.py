import math
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query

from app.core.errors import validation_error
from app.core.geo import BBox
from app.repositories.types import AuctionFilter, AuctionGroup, AuctionSource
from app.routers.deps import ContainerDep
from app.schemas.auction import (
    AuctionDetailOut,
    AuctionMeta,
    AuctionOut,
    AuctionPageMeta,
    AuctionResponse,
    AuctionsResponse,
)

router = APIRouter(tags=["auctions"])


@router.get("/auctions", response_model=AuctionsResponse, summary="경매·공매 물건 목록 (지도 마커·단지별)")
async def list_auctions(
    container: ContainerDep,
    bbox: Annotated[str | None, Query(description="min_lng,min_lat,max_lng,max_lat. 지도 화면 안의 물건")] = None,
    complex_id: Annotated[UUID | None, Query(description="이 단지와 같은 필지(PNU)의 물건")] = None,
    source: Annotated[AuctionSource | None, Query(description="court=법원 경매, public=공매. 생략하면 둘 다")] = None,
    property_group: Annotated[
        str | None, Query(description="쉼표 구분: apartment,officetel,villa,land,other. 생략하면 전부")
    ] = None,
    include_closed: Annotated[bool, Query(description="진행 목록에서 빠진(끝난) 물건도 포함")] = False,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=500)] = 100,
) -> AuctionsResponse:
    groups: tuple[AuctionGroup, ...] = ()
    if property_group:
        try:
            groups = tuple(AuctionGroup(g.strip()) for g in property_group.split(",") if g.strip())
        except ValueError:
            raise validation_error(
                "property_group", "property_group은 apartment,officetel,villa,land,other 중에서 고르세요"
            ) from None
    flt = AuctionFilter(
        bbox=BBox.parse(bbox) if bbox else None,
        complex_id=complex_id,
        source=source,
        groups=groups,
        include_closed=include_closed,
    )
    result, as_of = await container.auction_service.search(flt, page=page, page_size=page_size)
    return AuctionsResponse(
        data=[AuctionOut.from_domain(a) for a in result.items],
        meta=AuctionPageMeta(
            data_as_of=as_of,
            page=page,
            page_size=page_size,
            total=result.total,
            total_pages=math.ceil(result.total / page_size),
        ),
    )


@router.get("/auctions/{auction_id}", response_model=AuctionResponse, summary="경매·공매 물건 상세 (기일 이력 포함)")
async def get_auction(auction_id: str, container: ContainerDep) -> AuctionResponse:
    found, as_of = await container.auction_service.get(auction_id)
    return AuctionResponse(data=AuctionDetailOut.from_domain(found), meta=AuctionMeta(data_as_of=as_of))

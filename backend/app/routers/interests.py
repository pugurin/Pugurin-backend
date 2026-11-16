"""관심 단지·관심 경매 물건. 로그인 전이라 `X-Device-Id`(앱 설치 단위)로 저장한다.

기기마다 응답이 달라서 공용 캐시에 남지 않게 `Cache-Control: private, no-cache`를 붙인다.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response, status

from app.repositories.postgres.interests import InterestKind
from app.routers.deps import ContainerDep, require_device_id
from app.schemas.interest import (
    InterestAddedOut,
    InterestAddedResponse,
    InterestAuctionOut,
    InterestAuctionsMeta,
    InterestAuctionsResponse,
    InterestComplexesResponse,
    InterestComplexOut,
    InterestIdsOut,
    InterestIdsResponse,
    InterestListMeta,
)
from app.services.interest_service import MAX_PER_KIND

router = APIRouter(tags=["interests"])

DeviceDep = Annotated[UUID, Depends(require_device_id)]
PRIVATE = "private, no-cache"


@router.get("/interests", response_model=InterestIdsResponse, summary="내 관심 id 목록 (별 표시용)")
async def list_interest_ids(device: DeviceDep, container: ContainerDep, response: Response) -> InterestIdsResponse:
    response.headers["Cache-Control"] = PRIVATE
    ids = await container.interest_service.ids(device)
    return InterestIdsResponse(
        data=InterestIdsOut(complexes=ids[InterestKind.COMPLEX], auctions=ids[InterestKind.AUCTION])
    )


@router.get("/interests/complexes", response_model=InterestComplexesResponse, summary="관심 단지 목록")
async def list_interest_complexes(
    device: DeviceDep, container: ContainerDep, response: Response
) -> InterestComplexesResponse:
    response.headers["Cache-Control"] = PRIVATE
    items = await container.interest_service.complexes(device)
    return InterestComplexesResponse(
        data=[InterestComplexOut.from_domain(i) for i in items],
        meta=InterestListMeta(total=len(items), max_items=MAX_PER_KIND),
    )


@router.put("/interests/complexes/{complex_id}", response_model=InterestAddedResponse, summary="관심 단지 등록 (멱등)")
async def add_interest_complex(complex_id: UUID, device: DeviceDep, container: ContainerDep) -> InterestAddedResponse:
    added = await container.interest_service.add_complex(device, complex_id)
    return InterestAddedResponse(data=InterestAddedOut(target_id=str(complex_id), added_at=added))


@router.delete(
    "/interests/complexes/{complex_id}", status_code=status.HTTP_204_NO_CONTENT, summary="관심 단지 해제 (멱등)"
)
async def remove_interest_complex(complex_id: UUID, device: DeviceDep, container: ContainerDep) -> None:
    await container.interest_service.remove(device, InterestKind.COMPLEX, str(complex_id))


@router.get("/interests/auctions", response_model=InterestAuctionsResponse, summary="관심 경매·공매 물건 목록")
async def list_interest_auctions(
    device: DeviceDep, container: ContainerDep, response: Response
) -> InterestAuctionsResponse:
    response.headers["Cache-Control"] = PRIVATE
    items = await container.interest_service.auctions(device)
    return InterestAuctionsResponse(
        data=[InterestAuctionOut.from_domain(i) for i in items],
        meta=InterestAuctionsMeta(total=len(items), max_items=MAX_PER_KIND),
    )


@router.put(
    "/interests/auctions/{auction_id}", response_model=InterestAddedResponse, summary="관심 경매·공매 물건 등록 (멱등)"
)
async def add_interest_auction(auction_id: str, device: DeviceDep, container: ContainerDep) -> InterestAddedResponse:
    added = await container.interest_service.add_auction(device, auction_id)
    return InterestAddedResponse(data=InterestAddedOut(target_id=auction_id, added_at=added))


@router.delete(
    "/interests/auctions/{auction_id}", status_code=status.HTTP_204_NO_CONTENT, summary="관심 경매 물건 해제 (멱등)"
)
async def remove_interest_auction(auction_id: str, device: DeviceDep, container: ContainerDep) -> None:
    await container.interest_service.remove(device, InterestKind.AUCTION, auction_id)

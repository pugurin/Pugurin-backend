"""관심 단지·관심 경매 물건. 로그인한 회원만 쓴다(access token, 없거나 틀리면 401).

회원마다 응답이 달라서 공용 캐시에 남지 않게 `Cache-Control: private, no-cache`를 붙인다.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Path, Response, status

from app.repositories.postgres.interests import InterestKind
from app.routers.deps import ContainerDep, CurrentUser
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
    InterestRegionOut,
    InterestRegionsResponse,
)
from app.services.interest_service import MAX_PER_KIND, MAX_REGIONS

router = APIRouter(tags=["interests"])

PRIVATE = "private, no-cache"
# 법정동 10자리. 시군구 5자리도 받아서 서비스가 404로 돌려준다
REGION_CODE = Path(pattern=r"^(\d{5}|\d{10})$")


@router.get("/interests", response_model=InterestIdsResponse, summary="내 관심 id 목록 (별 표시용)")
async def list_interest_ids(user: CurrentUser, container: ContainerDep, response: Response) -> InterestIdsResponse:
    response.headers["Cache-Control"] = PRIVATE
    ids = await container.interest_service.ids(user.id)
    return InterestIdsResponse(
        data=InterestIdsOut(
            complexes=ids[InterestKind.COMPLEX],
            auctions=ids[InterestKind.AUCTION],
            regions=ids[InterestKind.REGION],
        )
    )


@router.get("/interests/complexes", response_model=InterestComplexesResponse, summary="관심 단지 목록")
async def list_interest_complexes(
    user: CurrentUser, container: ContainerDep, response: Response
) -> InterestComplexesResponse:
    response.headers["Cache-Control"] = PRIVATE
    items = await container.interest_service.complexes(user.id)
    return InterestComplexesResponse(
        data=[InterestComplexOut.from_domain(i) for i in items],
        meta=InterestListMeta(total=len(items), max_items=MAX_PER_KIND),
    )


@router.put("/interests/complexes/{complex_id}", response_model=InterestAddedResponse, summary="관심 단지 등록 (멱등)")
async def add_interest_complex(complex_id: UUID, user: CurrentUser, container: ContainerDep) -> InterestAddedResponse:
    added = await container.interest_service.add_complex(user.id, complex_id)
    return InterestAddedResponse(data=InterestAddedOut(target_id=str(complex_id), added_at=added))


@router.delete(
    "/interests/complexes/{complex_id}", status_code=status.HTTP_204_NO_CONTENT, summary="관심 단지 해제 (멱등)"
)
async def remove_interest_complex(complex_id: UUID, user: CurrentUser, container: ContainerDep) -> None:
    await container.interest_service.remove(user.id, InterestKind.COMPLEX, str(complex_id))


@router.get("/interests/auctions", response_model=InterestAuctionsResponse, summary="관심 경매·공매 물건 목록")
async def list_interest_auctions(
    user: CurrentUser, container: ContainerDep, response: Response
) -> InterestAuctionsResponse:
    response.headers["Cache-Control"] = PRIVATE
    items = await container.interest_service.auctions(user.id)
    return InterestAuctionsResponse(
        data=[InterestAuctionOut.from_domain(i) for i in items],
        meta=InterestAuctionsMeta(total=len(items), max_items=MAX_PER_KIND),
    )


@router.put(
    "/interests/auctions/{auction_id}", response_model=InterestAddedResponse, summary="관심 경매·공매 물건 등록 (멱등)"
)
async def add_interest_auction(auction_id: str, user: CurrentUser, container: ContainerDep) -> InterestAddedResponse:
    added = await container.interest_service.add_auction(user.id, auction_id)
    return InterestAddedResponse(data=InterestAddedOut(target_id=auction_id, added_at=added))


@router.delete(
    "/interests/auctions/{auction_id}", status_code=status.HTTP_204_NO_CONTENT, summary="관심 경매 물건 해제 (멱등)"
)
async def remove_interest_auction(auction_id: str, user: CurrentUser, container: ContainerDep) -> None:
    await container.interest_service.remove(user.id, InterestKind.AUCTION, auction_id)


@router.get("/interests/regions", response_model=InterestRegionsResponse, summary="관심 지역(법정동) 목록")
async def list_interest_regions(
    user: CurrentUser, container: ContainerDep, response: Response
) -> InterestRegionsResponse:
    response.headers["Cache-Control"] = PRIVATE
    items = await container.interest_service.regions(user.id)
    return InterestRegionsResponse(
        data=[InterestRegionOut.from_domain(i) for i in items],
        meta=InterestListMeta(total=len(items), max_items=MAX_REGIONS),
    )


@router.put("/interests/regions/{region_code}", response_model=InterestAddedResponse, summary="관심 지역 등록 (멱등)")
async def add_interest_region(
    region_code: Annotated[str, REGION_CODE], user: CurrentUser, container: ContainerDep
) -> InterestAddedResponse:
    added = await container.interest_service.add_region(user.id, region_code)
    return InterestAddedResponse(data=InterestAddedOut(target_id=region_code, added_at=added))


@router.delete(
    "/interests/regions/{region_code}", status_code=status.HTTP_204_NO_CONTENT, summary="관심 지역 해제 (멱등)"
)
async def remove_interest_region(
    region_code: Annotated[str, REGION_CODE], user: CurrentUser, container: ContainerDep
) -> None:
    await container.interest_service.remove(user.id, InterestKind.REGION, region_code)

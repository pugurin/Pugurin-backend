"""관심 단지·관심 경매 물건. 로그인한 회원 계정에 저장한다."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.core.dates import Clock
from app.core.errors import AppError, ErrorCode, data_not_ready, not_found
from app.repositories.ports import MarketRepository
from app.repositories.postgres.auctions import PostgresAuctionRepository
from app.repositories.postgres.interests import InterestKind, PostgresInterestRepository
from app.repositories.types import Auction, Complex, DealType, Transaction
from app.services.auction_service import AuctionService
from app.services.complex_service import ComplexService

MAX_PER_KIND = 200
MAX_REGIONS = 10  # 관심 지역은 리포트가 지역마다 만들어지므로 적게 둔다
NOT_READY = "관심 목록은 서버 DB가 있어야 쓸 수 있어요"


@dataclass(frozen=True)
class InterestedComplex:
    complex: Complex
    latest_sale: Transaction | None
    added_at: datetime


@dataclass(frozen=True)
class InterestedRegion:
    code: str
    name: str
    sigungu_name: str
    lat: float | None
    lng: float | None
    added_at: datetime


@dataclass(frozen=True)
class InterestedAuction:
    auction: Auction
    added_at: datetime


class InterestService:
    def __init__(
        self,
        repo: PostgresInterestRepository | None,
        market: MarketRepository,
        complexes: ComplexService,
        auctions: AuctionService,
        auction_repo: PostgresAuctionRepository | None,
        clock: Clock,
    ):
        self._repo = repo
        self._market = market
        self._complexes = complexes
        self._auctions = auctions
        self._auction_repo = auction_repo
        self._clock = clock

    def _ready(self) -> PostgresInterestRepository:
        if self._repo is None:
            raise data_not_ready(NOT_READY)
        return self._repo

    async def _add(self, user_id: UUID, kind: InterestKind, target: str) -> datetime:
        limit = MAX_REGIONS if kind is InterestKind.REGION else MAX_PER_KIND
        added = await self._ready().add(user_id, kind, target, max_items=limit)
        if added is None:
            what = {InterestKind.COMPLEX: "단지는", InterestKind.AUCTION: "경매 물건은", InterestKind.REGION: "지역은"}
            raise AppError(ErrorCode.CONFLICT, f"관심 {what[kind]} {limit}개까지 등록할 수 있어요")
        return added

    async def add_complex(self, user_id: UUID, complex_id: UUID) -> datetime:
        self._ready()
        await self._complexes.get(complex_id)  # 없는 단지면 404
        return await self._add(user_id, InterestKind.COMPLEX, str(complex_id))

    async def add_auction(self, user_id: UUID, auction_id: str) -> datetime:
        self._ready()
        await self._auctions.get(auction_id)  # 없는 물건이면 404, 경매 데이터가 없으면 503
        return await self._add(user_id, InterestKind.AUCTION, auction_id)

    async def add_region(self, user_id: UUID, region_code: str) -> datetime:
        repo = self._ready()
        if not await repo.is_dong(region_code):  # 없는 코드나 시군구면 404
            raise not_found("해당 법정동을 찾을 수 없어요")
        return await self._add(user_id, InterestKind.REGION, region_code)

    async def remove(self, user_id: UUID, kind: InterestKind, target: str) -> None:
        await self._ready().remove(user_id, kind, target)

    async def ids(self, user_id: UUID) -> dict[InterestKind, list[str]]:
        repo = self._ready()
        return {kind: [i.target_id for i in await repo.list(user_id, kind)] for kind in InterestKind}

    async def complexes(self, user_id: UUID) -> list[InterestedComplex]:
        """최근 등록한 것부터. 실거래 데이터에서 사라진 단지는 뺀다."""
        found: list[InterestedComplex] = []
        for item in await self._ready().list(user_id, InterestKind.COMPLEX):
            c = await self._market.get_complex(UUID(item.target_id))
            if c is None:
                continue
            page = await self._market.complex_transactions(
                c.id, deal_type=DealType.sale, exclude_direct=False, include_cancelled=False,
                sort="contract_date_desc", offset=0, limit=1,
            )  # fmt: skip
            found.append(InterestedComplex(c, page.items[0] if page.items else None, item.added_at))
        return found

    async def regions(self, user_id: UUID) -> list[InterestedRegion]:
        """최근 등록한 것부터."""
        return [InterestedRegion(**vars(r)) for r in await self._ready().list_regions(user_id)]

    async def auctions(self, user_id: UUID) -> list[InterestedAuction]:
        """최근 등록한 것부터. 끝난 물건도 `is_closed=true`로 남긴다(원천에서 지워진 물건만 빠진다)."""
        items = await self._ready().list(user_id, InterestKind.AUCTION)
        if not items or self._auction_repo is None:
            return []
        by_id = await self._auction_repo.get_many([i.target_id for i in items], today=self._clock().date())
        return [InterestedAuction(by_id[i.target_id], i.added_at) for i in items if i.target_id in by_id]

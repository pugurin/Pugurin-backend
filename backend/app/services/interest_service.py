"""관심 단지·관심 경매 물건. 로그인한 회원 계정에 저장한다."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.core.dates import Clock
from app.core.errors import AppError, ErrorCode, data_not_ready
from app.repositories.ports import MarketRepository
from app.repositories.postgres.auctions import PostgresAuctionRepository
from app.repositories.postgres.interests import InterestKind, PostgresInterestRepository
from app.repositories.types import Auction, Complex, DealType, Transaction
from app.services.auction_service import AuctionService
from app.services.complex_service import ComplexService

MAX_PER_KIND = 200
NOT_READY = "관심 목록은 서버 DB가 있어야 쓸 수 있어요"


def limit_message(kind: InterestKind) -> str:
    """종류별 한도 안내. 받침에 맞춰 조사를 고른다(단지는 / 경매 물건은)."""
    what = "단지는" if kind is InterestKind.COMPLEX else "경매 물건은"
    return f"관심 {what} {MAX_PER_KIND}개까지 등록할 수 있어요"


@dataclass(frozen=True)
class InterestedComplex:
    complex: Complex
    latest_sale: Transaction | None
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
        added = await self._ready().add(user_id, kind, target, max_items=MAX_PER_KIND)
        if added is None:
            raise AppError(ErrorCode.CONFLICT, limit_message(kind))
        return added

    async def add_complex(self, user_id: UUID, complex_id: UUID) -> datetime:
        self._ready()
        await self._complexes.get(complex_id)  # 없는 단지면 404
        return await self._add(user_id, InterestKind.COMPLEX, str(complex_id))

    async def add_auction(self, user_id: UUID, auction_id: str) -> datetime:
        self._ready()
        await self._auctions.get(auction_id)  # 없는 물건이면 404, 경매 데이터가 없으면 503
        return await self._add(user_id, InterestKind.AUCTION, auction_id)

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

    async def auctions(self, user_id: UUID) -> list[InterestedAuction]:
        """최근 등록한 것부터. 끝난 물건도 `is_closed=true`로 남긴다(원천에서 지워진 물건만 빠진다)."""
        items = await self._ready().list(user_id, InterestKind.AUCTION)
        if not items or self._auction_repo is None:
            return []
        by_id = await self._auction_repo.get_many([i.target_id for i in items], today=self._clock().date())
        return [InterestedAuction(by_id[i.target_id], i.added_at) for i in items if i.target_id in by_id]

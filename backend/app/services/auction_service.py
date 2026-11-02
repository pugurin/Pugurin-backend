from datetime import datetime

from app.core.dates import Clock
from app.core.errors import data_not_ready, not_found
from app.repositories.postgres.auctions import PostgresAuctionRepository
from app.repositories.types import Auction, AuctionFilter, AuctionPage, AuctionSort

NOT_READY = "경매·공매 데이터가 아직 없어요. 동기화가 끝나면 다시 시도해 주세요"


class AuctionService:
    """경매·공매는 DB에만 있다. DB가 없거나 동기화가 한 번도 끝나지 않았으면 503 DATA_NOT_READY."""

    def __init__(self, repo: PostgresAuctionRepository | None, clock: Clock):
        self._repo = repo
        self._clock = clock

    async def _ready(self) -> tuple[PostgresAuctionRepository, datetime]:
        if self._repo is None:
            raise data_not_ready(NOT_READY)
        as_of = await self._repo.data_as_of()
        if as_of is None:
            raise data_not_ready(NOT_READY)
        return self._repo, as_of

    async def search(
        self, flt: AuctionFilter, *, sort: AuctionSort, page: int, page_size: int
    ) -> tuple[AuctionPage, datetime]:
        repo, as_of = await self._ready()
        found = await repo.search(
            flt, today=self._clock().date(), sort=sort, offset=(page - 1) * page_size, limit=page_size
        )
        return found, as_of

    async def get(self, auction_id: str) -> tuple[Auction, datetime]:
        repo, as_of = await self._ready()
        found = await repo.get(auction_id, today=self._clock().date())
        if found is None:
            raise not_found("경매 물건을 찾을 수 없습니다")
        return found, as_of

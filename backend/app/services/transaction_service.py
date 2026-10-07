from datetime import datetime
from uuid import UUID

from app.core.errors import not_found
from app.core.geo import BBox
from app.repositories.ports import MarketRepository
from app.repositories.types import Transaction, TransactionFilter, TransactionPage


class TransactionService:
    def __init__(self, repo: MarketRepository):
        self._repo = repo

    @property
    def data_as_of(self) -> datetime:
        return self._repo.data_as_of

    async def list(
        self,
        flt: TransactionFilter,
        *,
        bbox: BBox | None,
        region_code: str | None,
        include_cancelled: bool,
        sort: str,
        page: int,
        page_size: int,
    ) -> TransactionPage:
        return await self._repo.list_transactions(
            flt,
            bbox=bbox,
            region_code=region_code,
            include_cancelled=include_cancelled,
            sort=sort,
            offset=(page - 1) * page_size,
            limit=page_size,
        )

    async def get(self, transaction_id: UUID) -> Transaction:
        found = await self._repo.get_transaction(transaction_id)
        if found is None:
            raise not_found("거래를 찾을 수 없습니다")
        return found

from datetime import datetime
from uuid import UUID

from app.core.errors import not_found
from app.repositories.ports import MarketRepository
from app.repositories.types import Complex, DealType, TransactionPage


class ComplexService:
    def __init__(self, repo: MarketRepository):
        self._repo = repo

    @property
    def data_as_of(self) -> datetime:
        return self._repo.data_as_of

    async def get(self, complex_id: UUID) -> Complex:
        found = await self._repo.get_complex(complex_id)
        if found is None:
            raise not_found("단지를 찾을 수 없습니다")
        return found

    async def transactions(
        self,
        complex_id: UUID,
        *,
        deal_type: DealType | None,
        exclude_direct: bool,
        include_cancelled: bool,
        sort: str,
        page: int,
        page_size: int,
    ) -> TransactionPage:
        await self.get(complex_id)
        return await self._repo.complex_transactions(
            complex_id,
            deal_type=deal_type,
            exclude_direct=exclude_direct,
            include_cancelled=include_cancelled,
            sort=sort,
            offset=(page - 1) * page_size,
            limit=page_size,
        )

from uuid import UUID

from app.core.errors import not_found, validation_error
from app.repositories.ports import MarketRepository
from app.repositories.types import DealType, PropertyType, StatsResult


def _check_land_is_sale(property_type: PropertyType, deal_type: DealType) -> None:
    if property_type == PropertyType.land and deal_type != DealType.sale:
        raise validation_error("deal_type", "토지는 매매(sale)만 조회할 수 있습니다")


class StatsService:
    def __init__(self, repo: MarketRepository):
        self._repo = repo

    async def region(
        self,
        region_code: str,
        property_type: PropertyType,
        deal_type: DealType,
        period_months: int,
        exclude_direct: bool,
    ) -> StatsResult:
        _check_land_is_sale(property_type, deal_type)
        result = await self._repo.region_stats(region_code, property_type, deal_type, period_months, exclude_direct)
        if result is None:
            raise not_found("지역을 찾을 수 없습니다")
        return result

    async def complex(
        self,
        complex_id: UUID,
        property_type: PropertyType,
        deal_type: DealType,
        period_months: int,
        exclude_direct: bool,
    ) -> StatsResult:
        _check_land_is_sale(property_type, deal_type)
        found = await self._repo.get_complex(complex_id)
        if found is None:
            raise not_found("단지를 찾을 수 없습니다")
        if found.property_type != property_type:
            raise validation_error("property_type", "단지의 매물 유형과 다릅니다")
        result = await self._repo.complex_stats(complex_id, deal_type, period_months, exclude_direct)
        assert result is not None
        return result

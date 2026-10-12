from dataclasses import dataclass
from uuid import UUID

from app.core.errors import not_found
from app.repositories.ports import MarketRepository
from app.repositories.types import Complex, DealType, PropertyType, StatsResult
from app.services.parcel_service import ParcelService, ZoningView

DISCLAIMER = "참고용 통계이며 실제 시세와 거래 조건은 공인중개사에게 확인이 필요합니다."
SIMILAR_WITHIN_PERCENT = 1.0  # 이보다 차이가 작으면 '비슷해요'로 말한다

# 한 숫자로 비교할 수 있는 지표. 월세는 보증금 조건이 제각각이라 비교하지 않는다
COMPARABLE_METRICS = {
    DealType.sale: ("median_price_per_pyeong", "평당 매매가"),
    DealType.jeonse: ("median_deposit_per_pyeong", "평당 전세 보증금"),
}
PROPERTY_LABELS = {
    PropertyType.apartment: "아파트",
    PropertyType.officetel: "오피스텔",
    PropertyType.villa: "연립·다세대",
    PropertyType.land: "토지",
}


@dataclass(frozen=True)
class Comparison:
    metric: str
    complex_value: int
    region_value: int
    difference_percent: float
    summary: str


@dataclass(frozen=True)
class ComplexAnalysis:
    complex: Complex
    region_code: str
    region_name: str
    complex_stats: StatsResult
    region_stats: StatsResult
    comparison: Comparison | None
    zonings: list[ZoningView] | None
    zoning_unavailable_reason: str | None
    disclaimer: str = DISCLAIMER


class AnalysisService:
    """단지 시세 추이를 같은 법정동 전체 추이와 나란히 놓고, 땅의 용도지역을 덧붙인다. 예측·권유는 하지 않는다."""

    def __init__(self, repo: MarketRepository, parcels: ParcelService):
        self._repo = repo
        self._parcels = parcels

    async def complex(
        self, complex_id: UUID, deal_type: DealType, period_months: int, exclude_direct: bool
    ) -> ComplexAnalysis:
        found = await self._repo.get_complex(complex_id)
        if found is None:
            raise not_found("단지를 찾을 수 없습니다")

        complex_stats = await self._repo.complex_stats(complex_id, deal_type, period_months, exclude_direct)
        region_stats = await self._repo.region_stats(
            found.region_code, found.property_type, deal_type, period_months, exclude_direct
        )
        assert complex_stats is not None and region_stats is not None
        full_name, short_name = await self._region_names(found.region_code)
        zonings, reason = await self._zonings(found)
        return ComplexAnalysis(
            complex=found,
            region_code=found.region_code,
            region_name=full_name,
            complex_stats=complex_stats,
            region_stats=region_stats,
            comparison=_compare(deal_type, found.property_type, short_name, complex_stats, region_stats),
            zonings=zonings,
            zoning_unavailable_reason=reason,
        )

    async def _region_names(self, region_code: str) -> tuple[str, str]:
        """('해운대구 우동', '우동')"""
        region = await self._repo.get_region(region_code)
        short = region.name if region else region_code
        sigungu = await self._repo.get_region(region_code[:5]) if len(region_code) == 10 else None
        return (f"{sigungu.name} {short}" if sigungu else short), short

    async def _zonings(self, found: Complex) -> tuple[list[ZoningView] | None, str | None]:
        if found.pnu is None:
            return None, "no_pnu"
        result = await self._parcels.by_pnu(found.pnu)
        if result.parcel is None:
            return None, result.unavailable_reason
        return result.parcel.zonings, None


def _compare(
    deal_type: DealType,
    property_type: PropertyType,
    region_short_name: str,
    mine: StatsResult,
    region: StatsResult,
) -> Comparison | None:
    if deal_type not in COMPARABLE_METRICS:
        return None
    metric, label = COMPARABLE_METRICS[deal_type]
    complex_value = getattr(mine.overall.metrics, metric)
    region_value = getattr(region.overall.metrics, metric)
    if not complex_value or not region_value:
        return None

    diff = round((complex_value / region_value - 1) * 100, 1)
    subject = f"이 단지의 {label} 중위값은 {region_short_name} {PROPERTY_LABELS[property_type]} 전체"
    if abs(diff) < SIMILAR_WITHIN_PERCENT:
        summary = f"{subject}와 비슷해요."
    else:
        summary = f"{subject}보다 {abs(round(diff))}% {'높아요' if diff > 0 else '낮아요'}."
    return Comparison(metric, complex_value, region_value, diff, summary)

from pydantic import BaseModel, ConfigDict

from app.core.geo import m2_to_pyeong
from app.repositories.types import DealType, StatsMetrics, StatsResult, StatsSeries
from app.schemas.common import DataMeta


class TrendPoint(BaseModel):
    """거래유형별 지표(median_*)는 추가 필드로 붙는다. 표본이 모자란 달은 값이 null이다."""

    model_config = ConfigDict(extra="allow")
    month: str
    count: int


class AreaStatsOut(BaseModel):
    model_config = ConfigDict(extra="allow")
    exclusive_area_m2: float
    exclusive_area_pyeong: float
    supply_area_pyeong: float | None
    transaction_count: int
    trend: list[TrendPoint]


class RegionStatsOut(BaseModel):
    model_config = ConfigDict(extra="allow")
    transaction_count: int
    trend: list[TrendPoint]


class ComplexStatsOut(RegionStatsOut):
    area_types: list[AreaStatsOut]


class StatsMeta(DataMeta):
    method: str


class RegionStatsResponse(BaseModel):
    data: RegionStatsOut
    meta: StatsMeta


class ComplexStatsResponse(BaseModel):
    data: ComplexStatsOut
    meta: StatsMeta


def metric_fields(deal_type: DealType, m: StatsMetrics) -> dict[str, int | None]:
    if deal_type == DealType.sale:
        return {"median_price_per_pyeong": m.median_price_per_pyeong}
    if deal_type == DealType.jeonse:
        return {"median_deposit_per_pyeong": m.median_deposit_per_pyeong}
    return {"median_deposit": m.median_deposit, "median_monthly_rent": m.median_monthly_rent}


def _series_fields(deal_type: DealType, series: StatsSeries) -> dict:
    return {
        "transaction_count": series.count,
        "trend": [
            TrendPoint(month=p.month, count=p.count, **metric_fields(deal_type, p.metrics)) for p in series.trend
        ],
        **metric_fields(deal_type, series.metrics),
    }


def region_stats_out(deal_type: DealType, result: StatsResult) -> RegionStatsOut:
    return RegionStatsOut(**_series_fields(deal_type, result.overall))


def complex_stats_out(deal_type: DealType, result: StatsResult) -> ComplexStatsOut:
    return ComplexStatsOut(
        **_series_fields(deal_type, result.overall),
        area_types=[
            AreaStatsOut(
                exclusive_area_m2=a.area_type.exclusive_area_m2,
                exclusive_area_pyeong=m2_to_pyeong(a.area_type.exclusive_area_m2),
                supply_area_pyeong=a.area_type.supply_area_pyeong,
                **_series_fields(deal_type, a.series),
            )
            for a in result.by_area
        ],
    )

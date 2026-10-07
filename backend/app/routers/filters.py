from typing import Annotated

from fastapi import Depends, Query

from app.core.errors import validation_error
from app.repositories.types import DealType, PropertyType, Range, TransactionFilter


def _range(name: str, low: float | None, high: float | None) -> Range:
    if low is not None and high is not None and low > high:
        raise validation_error(f"{name}_min", f"{name}_min은 {name}_max보다 클 수 없습니다")
    return Range(low, high)


def _only_when(name: str, rng: Range, allowed: bool, hint: str) -> Range:
    if rng.active and not allowed:
        field = f"{name}_min" if rng.min is not None else f"{name}_max"
        raise validation_error(field, f"{name} 필터는 {hint}에서만 쓸 수 있습니다")
    return rng


def market_filter(
    property_type: PropertyType,
    deal_type: DealType = DealType.sale,
    period_months: Annotated[int, Query(ge=1, le=60)] = 12,
    price_min: Annotated[int | None, Query(ge=0)] = None,
    price_max: Annotated[int | None, Query(ge=0)] = None,
    deposit_min: Annotated[int | None, Query(ge=0)] = None,
    deposit_max: Annotated[int | None, Query(ge=0)] = None,
    rent_min: Annotated[int | None, Query(ge=0)] = None,
    rent_max: Annotated[int | None, Query(ge=0)] = None,
    exclusive_area_pyeong_min: Annotated[float | None, Query(ge=0)] = None,
    exclusive_area_pyeong_max: Annotated[float | None, Query(ge=0)] = None,
    land_area_pyeong_min: Annotated[float | None, Query(ge=0)] = None,
    land_area_pyeong_max: Annotated[float | None, Query(ge=0)] = None,
    exclude_direct: bool = False,
) -> TransactionFilter:
    """지도 마커와 거래 목록이 같이 쓰는 필터. 유형에 맞지 않는 조합은 400으로 거절한다."""
    is_land = property_type == PropertyType.land
    if is_land and deal_type != DealType.sale:
        raise validation_error("deal_type", "토지는 매매(sale)만 조회할 수 있습니다")
    return TransactionFilter(
        property_type=property_type,
        deal_type=deal_type,
        period_months=period_months,
        price=_only_when("price", _range("price", price_min, price_max), deal_type == DealType.sale, "매매"),
        deposit=_only_when(
            "deposit", _range("deposit", deposit_min, deposit_max), deal_type != DealType.sale, "전세·월세"
        ),
        rent=_only_when("rent", _range("rent", rent_min, rent_max), deal_type == DealType.monthly, "월세"),
        exclusive_area_pyeong=_only_when(
            "exclusive_area_pyeong",
            _range("exclusive_area_pyeong", exclusive_area_pyeong_min, exclusive_area_pyeong_max),
            not is_land,
            "토지가 아닌 유형",
        ),
        land_area_pyeong=_only_when(
            "land_area_pyeong", _range("land_area_pyeong", land_area_pyeong_min, land_area_pyeong_max), is_land, "토지"
        ),
        exclude_direct=exclude_direct,
    )


MarketFilter = Annotated[TransactionFilter, Depends(market_filter)]

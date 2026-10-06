from typing import Annotated

from fastapi import APIRouter, Query

from app.core.errors import validation_error
from app.core.geo import BBox
from app.repositories.types import DealType, PropertyType, Range, TransactionFilter
from app.routers.deps import ContainerDep
from app.schemas.common import DataMeta
from app.schemas.map import ComplexMarker, MarkersData, MarkersResponse, ParcelMarker, RegionMarker

router = APIRouter(tags=["map"])


def _range(name: str, low: float | None, high: float | None) -> Range:
    if low is not None and high is not None and low > high:
        raise validation_error(f"{name}_min", f"{name}_min은 {name}_max보다 클 수 없습니다")
    return Range(low, high)


def _only_when(name: str, rng: Range, allowed: bool, hint: str) -> Range:
    if rng.active and not allowed:
        field = f"{name}_min" if rng.min is not None else f"{name}_max"
        raise validation_error(field, f"{name} 필터는 {hint}에서만 쓸 수 있습니다")
    return rng


@router.get("/map/markers", response_model=MarkersResponse, summary="지도 마커·집계 조회")
async def get_markers(
    container: ContainerDep,
    bbox: Annotated[str, Query(description="min_lng,min_lat,max_lng,max_lat")],
    zoom: Annotated[int, Query(ge=0, le=22, description="표준 웹 메르카토르 줌 (256px 타일 기준 정수)")],
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
) -> MarkersResponse:
    area = BBox.parse(bbox)
    is_land = property_type == PropertyType.land
    if is_land and deal_type != DealType.sale:
        raise validation_error("deal_type", "토지는 매매(sale)만 조회할 수 있습니다")

    flt = TransactionFilter(
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

    result = await container.map_service.markers(area, zoom, flt)
    markers = [
        *(RegionMarker.from_domain(r, deal_type) for r in result.regions),
        *(ComplexMarker.from_domain(c) for c in result.complexes),
        *(ParcelMarker.from_domain(p) for p in result.parcels),
    ]
    return MarkersResponse(
        data=MarkersData(level=result.level, markers=markers),
        meta=DataMeta(data_as_of=result.data_as_of),
    )

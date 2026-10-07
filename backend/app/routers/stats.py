from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Path, Query

from app.repositories.types import DealType, PropertyType
from app.routers.deps import ContainerDep
from app.schemas.stats import (
    ComplexStatsResponse,
    RegionStatsResponse,
    StatsMeta,
    complex_stats_out,
    region_stats_out,
)

router = APIRouter(tags=["stats"])


def _meta(container, exclude_direct: bool) -> StatsMeta:
    method = "중위값, 해제거래 제외" + (", 직거래 제외" if exclude_direct else "")
    return StatsMeta(data_as_of=container.complex_service.data_as_of, method=method)


@router.get("/stats/regions/{region_code}", response_model=RegionStatsResponse, summary="구·동 단위 통계")
async def region_stats(
    container: ContainerDep,
    region_code: Annotated[str, Path(pattern=r"^(\d{5}|\d{10})$", description="시군구 5자리 또는 법정동 10자리")],
    property_type: PropertyType,
    deal_type: DealType = DealType.sale,
    period_months: Annotated[int, Query(ge=1, le=60)] = 36,
    exclude_direct: bool = False,
) -> RegionStatsResponse:
    result = await container.stats_service.region(region_code, property_type, deal_type, period_months, exclude_direct)
    return RegionStatsResponse(data=region_stats_out(deal_type, result), meta=_meta(container, exclude_direct))


@router.get("/complexes/{complex_id}/stats", response_model=ComplexStatsResponse, summary="단지 평형별 시세 추이")
async def complex_stats(
    container: ContainerDep,
    complex_id: UUID,
    property_type: PropertyType,
    deal_type: DealType = DealType.sale,
    period_months: Annotated[int, Query(ge=1, le=60)] = 36,
    exclude_direct: bool = False,
) -> ComplexStatsResponse:
    result = await container.stats_service.complex(complex_id, property_type, deal_type, period_months, exclude_direct)
    return ComplexStatsResponse(data=complex_stats_out(deal_type, result), meta=_meta(container, exclude_direct))

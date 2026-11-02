from enum import StrEnum
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query

from app.core.errors import validation_error
from app.core.geo import BBox
from app.repositories.types import HospitalTier, SchoolLevel
from app.routers.deps import ContainerDep, require_market_data
from app.schemas.facility import (
    FacilityMeta,
    HospitalOut,
    HospitalsResponse,
    NearbyData,
    NearbyResponse,
    SchoolOut,
    SchoolsResponse,
)

router = APIRouter(tags=["facilities"])


def _parse[E: StrEnum](raw: str | None, enum: type[E], field: str) -> tuple[E, ...]:
    if not raw:
        return ()
    try:
        return tuple(enum(v.strip()) for v in raw.split(",") if v.strip())
    except ValueError:
        allowed = ",".join(e.value for e in enum)
        raise validation_error(field, f"{field}은 {allowed} 중에서 고르세요") from None


def _check_point(lat: float, lng: float, container) -> None:
    min_lng, min_lat, max_lng, max_lat = container.settings.busan_bbox
    if not (min_lat <= lat <= max_lat and min_lng <= lng <= max_lng):
        raise validation_error("lat", "부산 안의 좌표만 조회할 수 있습니다")


@router.get("/nearby", response_model=NearbyResponse, summary="좌표 주변 학교·병원")
async def get_nearby(
    container: ContainerDep,
    lat: Annotated[float, Query(ge=-90, le=90)],
    lng: Annotated[float, Query(ge=-180, le=180)],
) -> NearbyResponse:
    _check_point(lat, lng, container)
    found, as_of = await container.facility_service.nearby(lat, lng)
    return NearbyResponse(data=NearbyData.from_domain(found), meta=FacilityMeta(data_as_of=as_of))


@router.get("/complexes/{complex_id}/nearby", response_model=NearbyResponse, summary="단지 주변 학교·병원")
async def get_complex_nearby(complex_id: UUID, container: ContainerDep) -> NearbyResponse:
    await require_market_data(container)
    complex_ = await container.complex_service.get(complex_id)
    found, as_of = await container.facility_service.nearby(complex_.lat, complex_.lng)
    return NearbyResponse(data=NearbyData.from_domain(found), meta=FacilityMeta(data_as_of=as_of))


@router.get("/schools", response_model=SchoolsResponse, summary="지도 화면 안의 학교 (최대 500)")
async def list_schools(
    container: ContainerDep,
    bbox: Annotated[str, Query(description="min_lng,min_lat,max_lng,max_lat")],
    level: Annotated[str | None, Query(description="쉼표 구분: elementary,middle,high,special,other")] = None,
) -> SchoolsResponse:
    levels = _parse(level, SchoolLevel, "level")
    found, as_of = await container.facility_service.schools(BBox.parse(bbox), levels)
    return SchoolsResponse(data=[SchoolOut.from_domain(s) for s in found], meta=FacilityMeta(data_as_of=as_of))


@router.get("/hospitals", response_model=HospitalsResponse, summary="지도 화면 안의 병원 (의사 많은 순 최대 500)")
async def list_hospitals(
    container: ContainerDep,
    bbox: Annotated[str, Query(description="min_lng,min_lat,max_lng,max_lat")],
    tier: Annotated[
        str | None, Query(description="쉼표 구분: general,hospital,clinic,dental,oriental,public,other")
    ] = None,
) -> HospitalsResponse:
    tiers = _parse(tier, HospitalTier, "tier")
    found, as_of = await container.facility_service.hospitals(BBox.parse(bbox), tiers)
    return HospitalsResponse(data=[HospitalOut.from_domain(h) for h in found], meta=FacilityMeta(data_as_of=as_of))

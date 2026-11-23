from datetime import date
from typing import Literal

from pydantic import BaseModel

from app.core.geo import m2_to_pyeong
from app.repositories.types import (
    REDEVELOPMENT_NO_CODE_PREFIX,
    LocationKind,
    Redevelopment,
    RedevelopmentKind,
    RedevelopmentStage,
    RedevelopmentZone,
)
from app.schemas.common import DataMeta

ATTRIBUTION = "부산광역시 정비사업 정보(공공데이터포털)"
ZONE_ATTRIBUTION = "브이월드 용도지역지구정보"
# 부산시 정비사업 통합홈페이지의 구역 페이지. aCode가 곧 페이지 주소다
CITY_PAGE = "https://dynamice.busan.go.kr/home/{code}/main.do"


class RedevelopmentMeta(DataMeta):
    reporting_lag_notice: bool = False
    source_attribution: str = ATTRIBUTION  # 화면에 출처로 표시해야 한다


class RedevelopmentPageMeta(RedevelopmentMeta):
    page: int
    page_size: int
    total: int
    total_pages: int


class RedevelopmentImages(BaseModel):
    view: str | None = None  # 조감도
    location: str | None = None  # 위치도
    panorama: str | None = None  # 전경
    layout: str | None = None  # 배치도


class RedevelopmentOut(BaseModel):
    id: str
    area_name: str
    kind: RedevelopmentKind
    step_raw: str | None  # 사업추진단계 원문
    stage: RedevelopmentStage
    stage_order: int | None  # 0 해제 … 11 조합해산. 모르는 단계는 null
    location: str | None  # 위치 원문
    location_kind: LocationKind  # lot(지번) / office(조합 사무실 주소) / none. 좌표는 lot일 때만
    lat: float | None
    lng: float | None
    location_precise: bool
    # 위치 출처: lot(지번 좌표) / zone(이어진 정비구역 경계 안의 대표점, location_precise는 false) / none
    position_source: Literal["lot", "zone", "none"]
    zone_id: str | None  # 이어진 정비구역 경계(GET /redevelopment-zones의 zone_id)
    area_m2: float | None
    area_pyeong: float | None
    households: int | None
    buildings: int | None
    union_members: int | None
    floor_area_ratio: float | None  # 용적률(%)
    building_coverage_ratio: float | None  # 건폐율(%)
    scale: str | None
    business_entity: str | None
    contractor: str | None
    engineer: str | None
    architect: str | None
    specialists: str | None
    tel: str | None
    images: RedevelopmentImages
    source_url: str | None  # 부산시 정비사업 통합홈페이지의 구역 페이지
    is_closed: bool  # 원천 목록에서 빠짐

    @classmethod
    def from_domain(cls, z: Redevelopment) -> "RedevelopmentOut":
        return cls(
            id=z.id,
            area_name=z.area_name,
            kind=z.kind,
            step_raw=z.step_raw,
            stage=z.stage,
            stage_order=z.stage_order,
            location=z.location,
            location_kind=z.location_kind,
            lat=z.lat,
            lng=z.lng,
            location_precise=z.location_precise,
            position_source=z.position_source,
            zone_id=z.zone_id,
            area_m2=z.area_m2,
            area_pyeong=m2_to_pyeong(z.area_m2) if z.area_m2 is not None else None,
            households=z.households,
            buildings=z.buildings,
            union_members=z.union_members,
            floor_area_ratio=z.floor_area_ratio,
            building_coverage_ratio=z.building_coverage_ratio,
            scale=z.scale,
            business_entity=z.business_entity,
            contractor=z.contractor,
            engineer=z.engineer,
            architect=z.architect,
            specialists=z.specialists,
            tel=z.tel,
            images=RedevelopmentImages.model_validate(z.images),
            source_url=None if z.id.startswith(REDEVELOPMENT_NO_CODE_PREFIX) else CITY_PAGE.format(code=z.id),
            is_closed=z.closed_at is not None,
        )


class RedevelopmentsResponse(BaseModel):
    data: list[RedevelopmentOut]
    meta: RedevelopmentPageMeta


class RedevelopmentResponse(BaseModel):
    data: RedevelopmentOut
    meta: RedevelopmentMeta


class RedevelopmentZoneMeta(DataMeta):
    reporting_lag_notice: bool = False
    source_attribution: str = ZONE_ATTRIBUTION  # 화면에 출처로 표시해야 한다


class RedevelopmentZoneOut(BaseModel):
    zone_id: str
    layer: Literal["zone", "promotion", "planned"]  # 정비구역 / 재정비촉진구역 / 정비예정구역
    name: str | None  # 원천 이름. 비었거나 안내 문구면 null
    notice: str | None  # 이름 대신 고시 번호만 있을 때
    designated_on: date | None  # 고시일
    geometry: dict  # GeoJSON MultiPolygon, 좌표는 [경도, 위도]
    # 이어진 우리 정비사업 구역(없으면 모두 null). 누르면 GET /redevelopments/{redevelopment_id}로 상세를 연다
    redevelopment_id: str | None
    area_name: str | None
    kind: RedevelopmentKind | None
    stage: RedevelopmentStage | None

    @classmethod
    def from_domain(cls, z: RedevelopmentZone) -> "RedevelopmentZoneOut":
        return cls(**{f: getattr(z, f) for f in cls.model_fields})


class RedevelopmentZonesResponse(BaseModel):
    data: list[RedevelopmentZoneOut]
    meta: RedevelopmentZoneMeta

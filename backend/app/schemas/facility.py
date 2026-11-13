from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, SerializerFunctionWrapHandler, model_serializer

from app.repositories.types import (
    Academy,
    AcademyCategory,
    Hospital,
    HospitalTier,
    Nearby,
    School,
    SchoolLevel,
    SchoolZone,
    SchoolZoneInfo,
)
from app.schemas.common import DataMeta

SCHOOL_SOURCE = "나이스 교육정보 개방 포털"
HOSPITAL_SOURCE = "건강보험심사평가원"
SCHOOL_NOTICE = "배정 학교(학군)가 아니라 직선거리로 가까운 학교입니다"
ZONE_SOURCE = "한국교육시설안전원 학구도"
ZONE_NOTICE = "교육청 고시 통학구역 기준이며 실제 배정과 다를 수 있습니다"


def zone_source(info: SchoolZoneInfo) -> str:
    return f"{ZONE_SOURCE}(기준일 {info.base_date.isoformat()})" if info.base_date else ZONE_SOURCE


class FacilityMeta(DataMeta):
    reporting_lag_notice: bool = False
    source_attribution: list[str] = [SCHOOL_SOURCE, HOSPITAL_SOURCE]
    school_notice: str = SCHOOL_NOTICE
    zone_notice: str | None = None  # 학구도를 실어 배정 초등학교를 알려 줄 때만

    @classmethod
    def for_nearby(cls, n: Nearby, data_as_of: datetime) -> "FacilityMeta":
        if n.zone_info is None:
            return cls(data_as_of=data_as_of)
        return cls(
            data_as_of=data_as_of,
            source_attribution=[SCHOOL_SOURCE, HOSPITAL_SOURCE, zone_source(n.zone_info)],
            zone_notice=ZONE_NOTICE,
        )


class SchoolZoneMeta(DataMeta):
    reporting_lag_notice: bool = False
    source_attribution: list[str]
    zone_notice: str = ZONE_NOTICE

    @classmethod
    def from_domain(cls, info: SchoolZoneInfo) -> "SchoolZoneMeta":
        return cls(data_as_of=info.loaded_at, source_attribution=[zone_source(info)])


class SchoolZoneOut(BaseModel):
    school_id: str
    kind: str  # 통학구역 / 공동통학구역
    geometry: dict[str, Any]  # WGS84 GeoJSON Polygon 또는 MultiPolygon

    @classmethod
    def from_domain(cls, z: SchoolZone) -> "SchoolZoneOut":
        return cls(school_id=z.school_id, kind=z.kind, geometry=z.geometry)


class SchoolZoneResponse(BaseModel):
    data: SchoolZoneOut
    meta: SchoolZoneMeta


class SchoolOut(BaseModel):
    id: str
    name: str
    kind: str | None
    level: SchoolLevel
    foundation: str | None
    coed: str | None
    address: str
    lat: float | None
    lng: float | None
    tel: str | None
    homepage: str | None
    opened_on: date | None
    distance_m: int | None

    @classmethod
    def from_domain(cls, s: School) -> "SchoolOut":
        return cls(**{f: getattr(s, f) for f in cls.model_fields})


class HospitalOut(BaseModel):
    id: str
    name: str
    class_name: str | None
    tier: HospitalTier
    address: str
    lat: float | None
    lng: float | None
    tel: str | None
    homepage: str | None
    doctors: int | None
    distance_m: int | None

    @classmethod
    def from_domain(cls, h: Hospital) -> "HospitalOut":
        return cls(**{f: getattr(h, f) for f in cls.model_fields})


class AcademyOut(BaseModel):
    id: str
    name: str
    kind: str | None
    field: str | None
    category: AcademyCategory
    course: str | None
    address: str
    lat: float | None
    lng: float | None
    tel: str | None

    @classmethod
    def from_domain(cls, a: Academy) -> "AcademyOut":
        return cls(**{f: getattr(a, f) for f in cls.model_fields})


class NearbySchools(BaseModel):
    radius_m: int
    # 학구도 기준 배정 초등학교. 통학구역 밖이면 null, 학구도를 아직 안 실었으면 키를 뺀다(앱은 "준비 안 됨"으로 본다)
    assigned_elementary: SchoolOut | None = None
    elementary: list[SchoolOut]
    middle: list[SchoolOut]
    high: list[SchoolOut]

    # 반환 타입을 적지 않아야 OpenAPI 응답 스키마가 필드 목록 그대로 남는다
    @model_serializer(mode="wrap")
    def _omit_unloaded_zone(self, handler: SerializerFunctionWrapHandler):
        data = handler(self)
        if "assigned_elementary" not in self.model_fields_set:
            data.pop("assigned_elementary", None)
        return data


class NearbyHospitals(BaseModel):
    radius_m: int
    total: int
    counts: dict[HospitalTier, int]
    nearest_general: HospitalOut | None
    nearest: list[HospitalOut]


class NearbyAcademies(BaseModel):
    radius_m: int
    total: int
    counts: dict[AcademyCategory, int]


class NearbyData(BaseModel):
    schools: NearbySchools
    hospitals: NearbyHospitals
    academies: NearbyAcademies

    @classmethod
    def from_domain(cls, n: Nearby) -> "NearbyData":
        def schools(level: SchoolLevel) -> list[SchoolOut]:
            return [SchoolOut.from_domain(s) for s in n.schools.get(level, [])]

        zone: dict[str, SchoolOut | None] = {}
        if n.zone_info is not None:
            assigned = n.assigned_elementary
            zone["assigned_elementary"] = SchoolOut.from_domain(assigned) if assigned else None
        return cls(
            schools=NearbySchools(
                radius_m=n.school_radius_m,
                **zone,
                elementary=schools(SchoolLevel.elementary),
                middle=schools(SchoolLevel.middle),
                high=schools(SchoolLevel.high),
            ),
            hospitals=NearbyHospitals(
                radius_m=n.hospital_radius_m,
                total=sum(n.hospital_counts.values()),
                counts=n.hospital_counts,
                nearest_general=HospitalOut.from_domain(n.nearest_general) if n.nearest_general else None,
                nearest=[HospitalOut.from_domain(h) for h in n.nearest_hospitals],
            ),
            academies=NearbyAcademies(
                radius_m=n.academy_radius_m, total=sum(n.academy_counts.values()), counts=n.academy_counts
            ),
        )


class NearbyResponse(BaseModel):
    data: NearbyData
    meta: FacilityMeta


class SchoolsResponse(BaseModel):
    data: list[SchoolOut]
    meta: FacilityMeta


class HospitalsResponse(BaseModel):
    data: list[HospitalOut]
    meta: FacilityMeta


class AcademiesResponse(BaseModel):
    data: list[AcademyOut]
    meta: FacilityMeta

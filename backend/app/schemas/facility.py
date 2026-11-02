from datetime import date

from pydantic import BaseModel

from app.repositories.types import Hospital, HospitalTier, Nearby, School, SchoolLevel
from app.schemas.common import DataMeta

SCHOOL_SOURCE = "나이스 교육정보 개방 포털"
HOSPITAL_SOURCE = "건강보험심사평가원"
SCHOOL_NOTICE = "배정 학교(학군)가 아니라 직선거리로 가까운 학교입니다"


class FacilityMeta(DataMeta):
    reporting_lag_notice: bool = False
    source_attribution: list[str] = [SCHOOL_SOURCE, HOSPITAL_SOURCE]
    school_notice: str = SCHOOL_NOTICE


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


class NearbySchools(BaseModel):
    radius_m: int
    elementary: list[SchoolOut]
    middle: list[SchoolOut]
    high: list[SchoolOut]


class NearbyHospitals(BaseModel):
    radius_m: int
    total: int
    counts: dict[HospitalTier, int]
    nearest_general: HospitalOut | None
    nearest: list[HospitalOut]


class NearbyData(BaseModel):
    schools: NearbySchools
    hospitals: NearbyHospitals

    @classmethod
    def from_domain(cls, n: Nearby) -> "NearbyData":
        def schools(level: SchoolLevel) -> list[SchoolOut]:
            return [SchoolOut.from_domain(s) for s in n.schools.get(level, [])]

        return cls(
            schools=NearbySchools(
                radius_m=n.school_radius_m,
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

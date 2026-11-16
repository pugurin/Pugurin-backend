from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.repositories.types import AddressHit, ComplexSearchHit, RegionSearchHit, School, StationHit
from app.schemas.common import PlainEnvelope
from app.schemas.facility import SchoolOut


class ComplexResult(BaseModel):
    type: Literal["complex"] = "complex"
    complex_id: UUID
    name: str
    address: str
    lat: float
    lng: float

    @classmethod
    def from_domain(cls, hit: ComplexSearchHit) -> "ComplexResult":
        c = hit.complex
        return cls(complex_id=c.id, name=c.name, address=c.address, lat=c.lat, lng=c.lng)


class RegionResult(BaseModel):
    type: Literal["region"] = "region"
    region_level: Literal["sigungu", "dong"]
    region_code: str
    name: str
    lat: float
    lng: float
    bbox: list[float]

    @classmethod
    def from_domain(cls, hit: RegionSearchHit) -> "RegionResult":
        return cls(
            region_level=hit.level, region_code=hit.code, name=hit.name, lat=hit.lat, lng=hit.lng, bbox=list(hit.bbox)
        )


class AddressResult(BaseModel):
    type: Literal["address"] = "address"
    pnu: str | None
    address: str
    lat: float
    lng: float

    @classmethod
    def from_domain(cls, hit: AddressHit) -> "AddressResult":
        return cls(pnu=hit.pnu, address=hit.address, lat=hit.lat, lng=hit.lng)


class StationResult(BaseModel):
    type: Literal["station"] = "station"
    station_id: str
    name: str  # 예: "서면역 1호선". 노선마다 따로 온다
    line: str | None  # 예: "부산도시철도 1호선"
    address: str
    lat: float
    lng: float

    @classmethod
    def from_domain(cls, hit: StationHit) -> "StationResult":
        return cls(
            station_id=hit.station_id, name=hit.name, line=hit.line, address=hit.address, lat=hit.lat, lng=hit.lng
        )


class SchoolResult(BaseModel):
    type: Literal["school"] = "school"
    name: str
    address: str
    lat: float
    lng: float
    school: SchoolOut  # 학교 정보 전체(지도에서 학교 시트를 바로 열 수 있게)

    @classmethod
    def from_domain(cls, s: School) -> "SchoolResult":
        assert s.lat is not None and s.lng is not None  # 위치 없는 학교는 서비스에서 뺀다
        return cls(name=s.name, address=s.address, lat=s.lat, lng=s.lng, school=SchoolOut.from_domain(s))


SearchItem = Annotated[
    RegionResult | StationResult | SchoolResult | ComplexResult | AddressResult, Field(discriminator="type")
]
SearchResponse = PlainEnvelope[list[SearchItem]]

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.repositories.types import AddressHit, ComplexSearchHit, RegionSearchHit
from app.schemas.common import PlainEnvelope


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


SearchItem = Annotated[ComplexResult | RegionResult | AddressResult, Field(discriminator="type")]
SearchResponse = PlainEnvelope[list[SearchItem]]

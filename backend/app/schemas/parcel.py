from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel


class ZoningOut(BaseModel):
    zone_type: str
    area_ratio: float | None
    inclusion: str
    max_building_coverage_ratio: int | None
    max_floor_area_ratio: int | None


class RestrictionOut(BaseModel):
    name: str
    plain_explanation: str
    glossary_term_id: UUID | None


class ParcelOut(BaseModel):
    pnu: str
    jibun_address: str
    land_category: str
    land_area_m2: float
    land_area_pyeong: float
    official_land_price_per_m2: int
    official_land_price_year: int
    road_side: str
    shape: str
    geometry: dict
    zonings: list[ZoningOut]
    ratio_source: str
    restrictions: list[RestrictionOut]
    glossary: dict[str, UUID]


UnavailableReason = Literal["no_parcel", "out_of_service_area", "source_unavailable"]


class ParcelMeta(BaseModel):
    cached_at: datetime | None
    unavailable_reason: UnavailableReason | None


class ParcelResponse(BaseModel):
    data: ParcelOut | None
    meta: ParcelMeta

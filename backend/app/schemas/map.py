from datetime import date
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.core.geo import m2_to_pyeong
from app.repositories.types import (
    ComplexMarkerRow,
    DealType,
    Level,
    ParcelMarkerRow,
    RegionAggregate,
)
from app.schemas.common import DataMeta


class RegionMarker(BaseModel):
    kind: Literal["region"] = "region"
    region_code: str
    name: str
    lat: float
    lng: float
    transaction_count: int
    summary: dict[str, int]

    @classmethod
    def from_domain(cls, agg: RegionAggregate, deal_type: DealType) -> "RegionMarker":
        if deal_type == DealType.sale:
            summary = {"median_price_per_pyeong": agg.median_price_per_pyeong}
        elif deal_type == DealType.jeonse:
            summary = {"median_deposit_per_pyeong": agg.median_deposit_per_pyeong}
        else:
            summary = {"median_deposit": agg.median_deposit, "median_monthly_rent": agg.median_monthly_rent}
        return cls(
            region_code=agg.region.code,
            name=agg.region.name,
            lat=agg.region.lat,
            lng=agg.region.lng,
            transaction_count=agg.transaction_count,
            summary={k: v for k, v in summary.items() if v is not None},
        )


class ComplexLatest(BaseModel):
    deal_type: DealType
    price: int | None
    deposit: int | None
    monthly_rent: int | None
    exclusive_area_pyeong: float
    supply_area_pyeong: float | None
    floor: int | None
    contract_date: date


class ComplexMarker(BaseModel):
    kind: Literal["complex"] = "complex"
    complex_id: UUID
    name: str
    lat: float
    lng: float
    latest: ComplexLatest
    transaction_count: int

    @classmethod
    def from_domain(cls, row: ComplexMarkerRow) -> "ComplexMarker":
        tx = row.latest
        assert tx.exclusive_area_m2 is not None
        return cls(
            complex_id=row.complex.id,
            name=row.complex.name,
            lat=row.complex.lat,
            lng=row.complex.lng,
            transaction_count=row.transaction_count,
            latest=ComplexLatest(
                deal_type=tx.deal_type,
                price=tx.price,
                deposit=tx.deposit,
                monthly_rent=tx.monthly_rent,
                exclusive_area_pyeong=m2_to_pyeong(tx.exclusive_area_m2),
                supply_area_pyeong=tx.supply_area_pyeong,
                floor=tx.floor,
                contract_date=tx.contract_date,
            ),
        )


class ParcelLatest(BaseModel):
    deal_type: DealType
    price: int
    land_area_pyeong: float
    contract_date: date


class ParcelMarker(BaseModel):
    kind: Literal["parcel"] = "parcel"
    transaction_id: UUID
    pnu: str
    lat: float
    lng: float
    latest: ParcelLatest

    @classmethod
    def from_domain(cls, row: ParcelMarkerRow) -> "ParcelMarker":
        tx = row.latest
        assert tx.pnu is not None and tx.price is not None and tx.land_area_m2 is not None
        return cls(
            transaction_id=tx.id,
            pnu=tx.pnu,
            lat=tx.lat,
            lng=tx.lng,
            latest=ParcelLatest(
                deal_type=tx.deal_type,
                price=tx.price,
                land_area_pyeong=m2_to_pyeong(tx.land_area_m2),
                contract_date=tx.contract_date,
            ),
        )


Marker = Annotated[RegionMarker | ComplexMarker | ParcelMarker, Field(discriminator="kind")]


class MarkersData(BaseModel):
    level: Level
    markers: list[Marker]


class MarkersResponse(BaseModel):
    data: MarkersData
    meta: DataMeta

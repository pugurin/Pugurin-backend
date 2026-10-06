from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel

from app.core.geo import m2_to_pyeong, pyeong_exact
from app.repositories.types import (
    Complex,
    DealType,
    LocationPrecision,
    PropertyType,
    TradeMethod,
    Transaction,
)
from app.schemas.common import PageMeta, PlainEnvelope


class AreaTypeOut(BaseModel):
    exclusive_area_m2: float
    exclusive_area_pyeong: float
    supply_area_pyeong: float | None


class ComplexOut(BaseModel):
    id: UUID
    property_type: PropertyType
    name: str
    address: str
    region_code: str
    pnu: str
    lat: float
    lng: float
    build_year: int
    household_count: int
    area_types: list[AreaTypeOut]

    @classmethod
    def from_domain(cls, c: Complex) -> "ComplexOut":
        return cls(
            id=c.id,
            property_type=c.property_type,
            name=c.name,
            address=c.address,
            region_code=c.region_code,
            pnu=c.pnu,
            lat=c.lat,
            lng=c.lng,
            build_year=c.build_year,
            household_count=c.household_count,
            area_types=[
                AreaTypeOut(
                    exclusive_area_m2=a.exclusive_area_m2,
                    exclusive_area_pyeong=m2_to_pyeong(a.exclusive_area_m2),
                    supply_area_pyeong=a.supply_area_pyeong,
                )
                for a in c.area_types
            ],
        )


class TransactionOut(BaseModel):
    id: UUID
    property_type: PropertyType
    deal_type: DealType
    complex_id: UUID | None
    address: str
    region_code: str
    jibun: str | None
    pnu: str | None
    location_precision: LocationPrecision
    lat: float
    lng: float
    price: int | None
    deposit: int | None
    monthly_rent: int | None
    exclusive_area_m2: float | None
    exclusive_area_pyeong: float | None
    price_per_pyeong: int | None
    floor: int | None
    contract_date: date
    build_year: int | None
    trade_method: TradeMethod
    is_cancelled: bool
    cancelled_at: datetime | None

    @classmethod
    def from_domain(cls, tx: Transaction) -> "TransactionOut":
        exclusive = tx.exclusive_area_m2
        per_pyeong = (
            int(round(tx.price / pyeong_exact(tx.basis_area_m2)))
            if tx.deal_type == DealType.sale and tx.price is not None
            else None
        )
        return cls(
            id=tx.id,
            property_type=tx.property_type,
            deal_type=tx.deal_type,
            complex_id=tx.complex_id,
            address=tx.address,
            region_code=tx.region_code,
            jibun=tx.jibun,
            pnu=tx.pnu,
            location_precision=tx.location_precision,
            lat=tx.lat,
            lng=tx.lng,
            price=tx.price,
            deposit=tx.deposit,
            monthly_rent=tx.monthly_rent,
            exclusive_area_m2=exclusive,
            exclusive_area_pyeong=m2_to_pyeong(exclusive) if exclusive is not None else None,
            price_per_pyeong=per_pyeong,
            floor=tx.floor,
            contract_date=tx.contract_date,
            build_year=tx.build_year,
            trade_method=tx.trade_method,
            is_cancelled=tx.is_cancelled,
            cancelled_at=tx.cancelled_at,
        )


ComplexResponse = PlainEnvelope[ComplexOut]


class TransactionsResponse(BaseModel):
    data: list[TransactionOut]
    meta: PageMeta

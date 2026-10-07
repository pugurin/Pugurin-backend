from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from uuid import UUID


class PropertyType(StrEnum):
    apartment = "apartment"
    officetel = "officetel"
    villa = "villa"
    land = "land"


class DealType(StrEnum):
    sale = "sale"
    jeonse = "jeonse"
    monthly = "monthly"


class Level(StrEnum):
    sigungu = "sigungu"
    dong = "dong"
    complex = "complex"


class TradeMethod(StrEnum):
    broker = "broker"
    direct = "direct"


class LocationPrecision(StrEnum):
    parcel = "parcel"
    dong = "dong"


class GlossaryCategory(StrEnum):
    trade = "trade"
    land = "land"
    building = "building"
    tax = "tax"


@dataclass(frozen=True)
class Range:
    min: float | None = None
    max: float | None = None

    @property
    def active(self) -> bool:
        return self.min is not None or self.max is not None

    def accepts(self, value: float | None) -> bool:
        if not self.active:
            return True
        if value is None:
            return False
        return (self.min is None or value >= self.min) and (self.max is None or value <= self.max)


@dataclass(frozen=True)
class TransactionFilter:
    property_type: PropertyType
    deal_type: DealType
    period_months: int = 12
    price: Range = Range()
    deposit: Range = Range()
    rent: Range = Range()
    exclusive_area_pyeong: Range = Range()
    land_area_pyeong: Range = Range()
    exclude_direct: bool = False


@dataclass(frozen=True)
class AreaType:
    exclusive_area_m2: float
    supply_area_pyeong: float | None


@dataclass(frozen=True)
class Complex:
    id: UUID
    property_type: PropertyType
    name: str
    address: str
    region_code: str
    pnu: str | None
    lat: float
    lng: float
    build_year: int | None
    household_count: int | None
    area_types: tuple[AreaType, ...]


@dataclass(frozen=True)
class Transaction:
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
    land_area_m2: float | None
    supply_area_pyeong: float | None
    floor: int | None
    contract_date: date
    build_year: int | None
    trade_method: TradeMethod | None
    is_cancelled: bool
    cancelled_at: datetime | None

    @property
    def basis_area_m2(self) -> float:
        """평당가 계산 기준 면적: 토지는 토지면적, 그 외는 전용면적."""
        area = self.land_area_m2 if self.property_type == PropertyType.land else self.exclusive_area_m2
        assert area is not None
        return area


@dataclass(frozen=True)
class RegionRef:
    code: str
    name: str
    lat: float
    lng: float


@dataclass(frozen=True)
class RegionAggregate:
    region: RegionRef
    transaction_count: int
    median_price_per_pyeong: int | None = None
    median_deposit_per_pyeong: int | None = None
    median_deposit: int | None = None
    median_monthly_rent: int | None = None


@dataclass(frozen=True)
class ComplexMarkerRow:
    complex: Complex
    transaction_count: int
    latest: Transaction


@dataclass(frozen=True)
class ParcelMarkerRow:
    latest: Transaction


@dataclass(frozen=True)
class TransactionPage:
    items: list[Transaction]
    total: int


@dataclass(frozen=True)
class GlossaryTerm:
    id: UUID
    term: str
    category: GlossaryCategory
    is_popular: bool
    display_order: int
    short_definition: str
    long_definition: str
    example: str


@dataclass(frozen=True)
class ZoningPart:
    zone_type: str
    area_ratio: float | None
    inclusion: str


@dataclass(frozen=True)
class RestrictionInfo:
    name: str
    plain_explanation: str


@dataclass(frozen=True)
class ParcelData:
    pnu: str
    jibun_address: str
    land_category: str
    land_area_m2: float
    official_land_price_per_m2: int
    official_land_price_year: int
    road_side: str
    shape: str
    geometry: dict
    zonings: tuple[ZoningPart, ...]
    restrictions: tuple[RestrictionInfo, ...]


@dataclass(frozen=True)
class ZoningLimits:
    max_building_coverage_ratio: int
    max_floor_area_ratio: int

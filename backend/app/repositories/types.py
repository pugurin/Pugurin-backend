from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from uuid import UUID

from app.core.geo import BBox


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
    include_renewals: bool = False  # 전세·월세 갱신 계약. 기본은 빼고 신규 계약만 본다


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
    is_renewal: bool = False  # 전세·월세 갱신 계약(원천 contractType='갱신')
    complex_name: str | None = None  # 단지 거래면 단지명. 목록에서 주소 대신 보여준다

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
class StatsMetrics:
    median_price_per_pyeong: int | None = None
    median_deposit_per_pyeong: int | None = None
    median_deposit: int | None = None
    median_monthly_rent: int | None = None


@dataclass(frozen=True)
class MonthStats:
    month: str  # YYYY-MM
    count: int
    metrics: StatsMetrics  # 표본이 모자란 달은 모두 None


@dataclass(frozen=True)
class StatsSeries:
    count: int
    metrics: StatsMetrics
    trend: tuple[MonthStats, ...]


@dataclass(frozen=True)
class AreaStats:
    area_type: AreaType
    series: StatsSeries


@dataclass(frozen=True)
class StatsResult:
    overall: StatsSeries
    by_area: tuple[AreaStats, ...] = ()


@dataclass(frozen=True)
class AddressHit:
    address: str
    lat: float
    lng: float
    pnu: str | None


@dataclass(frozen=True)
class ComplexSearchHit:
    complex: Complex
    score: int
    popularity: int


@dataclass(frozen=True)
class RegionSearchHit:
    code: str
    level: str  # sigungu | dong
    name: str  # 예: "해운대구 우동"
    lat: float
    lng: float
    bbox: tuple[float, float, float, float]  # min_lng, min_lat, max_lng, max_lat
    score: int


@dataclass(frozen=True)
class ZoningLimits:
    max_building_coverage_ratio: int
    max_floor_area_ratio: int


# ── 경매·공매 (경매마당, 원천: 대법원 법원경매정보·온비드) ─────────────


class AuctionSource(StrEnum):
    court = "court"  # 법원 경매
    public = "public"  # 공매(온비드)


class AuctionGroup(StrEnum):
    """물건용도 원문을 우리 매물 유형으로 묶은 것. 단독·다가구·상가·공장 등은 other."""

    apartment = "apartment"
    officetel = "officetel"
    villa = "villa"
    land = "land"
    other = "other"


@dataclass(frozen=True)
class AuctionFilter:
    bbox: BBox | None = None
    complex_id: UUID | None = None
    source: AuctionSource | None = None
    groups: tuple[AuctionGroup, ...] = ()
    include_closed: bool = False


@dataclass(frozen=True)
class Auction:
    id: str
    source: AuctionSource
    case_no: str
    court_name: str | None
    title: str | None
    usage: str | None
    group: AuctionGroup
    address: str
    lat: float | None
    lng: float | None
    location_precise: bool
    complex_id: UUID | None
    appraisal_price: int | None
    min_price: int | None
    deposit: int | None
    fail_count: int | None
    sale_date: date | None
    bid_start_at: datetime | None
    bid_end_at: datetime | None
    land_area_m2: float | None
    building_area_m2: float | None
    is_share: bool
    status: str | None
    closed_at: datetime | None
    schedule: tuple[dict, ...] = ()
    results: tuple[dict, ...] = ()


@dataclass(frozen=True)
class AuctionPage:
    items: list[Auction]
    total: int


# ── 주변 생활 시설 (학교: 나이스, 병원: 건강보험심사평가원) ─────────────


class SchoolLevel(StrEnum):
    elementary = "elementary"  # 초등학교
    middle = "middle"  # 중학교
    high = "high"  # 고등학교
    special = "special"  # 특수학교
    other = "other"  # 각종·평생·외국인학교 등


class HospitalTier(StrEnum):
    general = "general"  # 상급종합·종합병원
    hospital = "hospital"  # 병원·요양병원·정신병원·한방병원·치과병원
    clinic = "clinic"  # 의원
    dental = "dental"  # 치과의원
    oriental = "oriental"  # 한의원
    public = "public"  # 보건소·보건지소·보건진료소
    other = "other"


@dataclass(frozen=True)
class School:
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
    distance_m: int | None = None


@dataclass(frozen=True)
class Hospital:
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
    distance_m: int | None = None


@dataclass(frozen=True)
class Nearby:
    schools: dict[SchoolLevel, list[School]]
    school_radius_m: int
    hospital_radius_m: int
    hospital_counts: dict[HospitalTier, int]
    nearest_general: Hospital | None
    nearest_hospitals: list[Hospital]

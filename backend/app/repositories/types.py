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


class AuctionSort(StrEnum):
    sale_date = "sale_date"  # 매각기일 빠른 순(기본)
    min_price_ratio = "min_price_ratio"  # 최저가 ÷ 감정가 낮은 순
    min_price = "min_price"  # 최저가 낮은 순
    recent_trade_discount = "recent_trade_discount"  # 최근 실거래 대비 많이 싼 순


@dataclass(frozen=True)
class RecentTrade:
    """같은 단지·같은 평형의 최근 12개월 매매(해제·직거래 제외) 중 가장 최근 1건, 그 기간 거래 수와 평균가."""

    price: int
    contract_date: date
    exclusive_area_m2: float
    count: int
    avg_price: int  # 같은 거래들의 평균가(원 단위 반올림)
    supply_area_pyeong: float | None = None  # 그 평형의 공급 평형. 건축물대장으로 못 채웠으면 None


@dataclass(frozen=True)
class AuctionFilter:
    bbox: BBox | None = None
    complex_id: UUID | None = None
    source: AuctionSource | None = None
    groups: tuple[AuctionGroup, ...] = ()
    include_closed: bool = False
    exclude_share: bool = False


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
    recent_trade: RecentTrade | None = None


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


class AcademyCategory(StrEnum):
    tutoring = "tutoring"  # 입시.검정 및 보습
    arts = "arts"  # 예능(음악·미술·무용)
    language = "language"  # 국제화(외국어)
    reading_room = "reading_room"  # 독서실
    other = "other"  # 종합·직업기술·기예·인문사회 등


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
class Academy:
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
    distance_m: int | None = None


@dataclass(frozen=True)
class Nearby:
    schools: dict[SchoolLevel, list[School]]
    school_radius_m: int
    hospital_radius_m: int
    hospital_counts: dict[HospitalTier, int]
    nearest_general: Hospital | None
    nearest_hospitals: list[Hospital]
    academy_radius_m: int
    academy_counts: dict[AcademyCategory, int]


# ── 정비사업 구역 (부산광역시 정비사업 정보) ─────────────

# aCode가 빈 구역의 ID 접두어(뒤는 구역명 해시). 이런 구역은 시 홈페이지 구역 페이지 주소를 만들 수 없다
REDEVELOPMENT_NO_CODE_PREFIX = "NOCODE_"


class RedevelopmentKind(StrEnum):
    """구역명(areaName)에 들어 있는 사업 이름으로 나눈다."""

    redevelopment = "redevelopment"  # 재개발
    reconstruction = "reconstruction"  # 재건축
    small_reconstruction = "small_reconstruction"  # 소규모재건축
    small_redevelopment = "small_redevelopment"  # 소규모재개발
    street_housing = "street_housing"  # 가로주택정비
    residential_improvement = "residential_improvement"  # 주거환경개선
    urban_environment = "urban_environment"  # 도시환경정비
    other = "other"


class RedevelopmentStage(StrEnum):
    """사업추진단계(step) 원문을 순서 있는 단계로 맞춘 것. 순서는 adapters/redevelopment.py의 표에 있다."""

    cancelled = "cancelled"  # 0 해제·취소
    planned = "planned"  # 1 예정구역·기본계획
    designated = "designated"  # 2 정비구역 지정·정비계획
    committee = "committee"  # 3 추진위원회
    association = "association"  # 4 조합설립인가
    review = "review"  # 5 건축심의·통합심의
    project_approved = "project_approved"  # 6 사업시행계획인가
    disposal_approved = "disposal_approved"  # 7 관리처분계획
    construction = "construction"  # 8 착공·이주·철거
    completed = "completed"  # 9 준공
    transferred = "transferred"  # 10 이전고시
    dissolved = "dissolved"  # 11 조합해산·청산
    unknown = "unknown"  # 표에 없는 원문


class LocationKind(StrEnum):
    """원천 위치 텍스트의 종류. 지번(lot)만 지오코딩한다."""

    lot = "lot"  # 구역 지번(예: "가야동 410번지 일원")
    office = "office"  # 조합 사무실 주소(층·호·빌딩·도로명 건물번호)
    none = "none"  # 비었거나 위치로 쓸 수 없음


@dataclass(frozen=True)
class RedevelopmentFilter:
    bbox: BBox | None = None
    kinds: tuple[RedevelopmentKind, ...] = ()
    stages: tuple[RedevelopmentStage, ...] = ()
    include_closed: bool = False


@dataclass(frozen=True)
class Redevelopment:
    id: str
    area_name: str
    kind: RedevelopmentKind
    step_raw: str | None
    stage: RedevelopmentStage
    stage_order: int | None
    location: str | None
    location_kind: LocationKind
    lat: float | None
    lng: float | None
    location_precise: bool
    area_m2: float | None
    households: int | None
    buildings: int | None
    union_members: int | None
    floor_area_ratio: float | None
    building_coverage_ratio: float | None
    scale: str | None
    business_entity: str | None
    contractor: str | None
    engineer: str | None
    architect: str | None
    specialists: str | None
    tel: str | None
    images: dict[str, str]
    first_seen_at: datetime
    last_seen_at: datetime
    closed_at: datetime | None


@dataclass(frozen=True)
class RedevelopmentPage:
    items: list[Redevelopment]
    total: int

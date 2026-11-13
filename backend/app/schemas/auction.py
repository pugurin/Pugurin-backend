from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel

from app.core.geo import m2_to_pyeong
from app.repositories.types import Auction, AuctionGroup, AuctionSource
from app.schemas.common import DataMeta

ATTRIBUTION = "대법원 법원경매정보"
ATTRIBUTION_URL = "https://www.courtauction.go.kr/"


class AuctionMeta(DataMeta):
    reporting_lag_notice: bool = False
    source_attribution: str = ATTRIBUTION  # 화면에 출처로 표시해야 한다
    source_url: str = ATTRIBUTION_URL


class AuctionPageMeta(AuctionMeta):
    page: int
    page_size: int
    total: int
    total_pages: int


class AuctionOut(BaseModel):
    id: str
    source: AuctionSource
    case_no: str
    court_name: str | None
    title: str | None
    usage: str | None
    property_group: AuctionGroup
    address: str
    lat: float | None
    lng: float | None
    location_precise: bool
    complex_id: UUID | None
    appraisal_price: int | None
    min_price: int | None
    min_price_ratio: int | None  # 최저가 ÷ 감정가(%)
    deposit: int | None
    fail_count: int | None
    sale_date: date | None
    bid_start_at: datetime | None
    bid_end_at: datetime | None
    land_area_m2: float | None
    land_area_pyeong: float | None
    building_area_m2: float | None
    building_area_pyeong: float | None
    is_share: bool
    status: str | None
    is_closed: bool
    # 같은 단지·같은 평형(전용면적)의 최근 12개월 매매. 해제·직거래 제외. 거래가 없으면 모두 null
    recent_trade_price: int | None  # 가장 최근 1건의 가격
    recent_trade_date: date | None
    recent_trade_area_m2: float | None
    recent_trade_count: int | None  # 그 기간 거래 수
    recent_trade_avg_price: int | None  # 그 기간 평균가(원 단위 반올림)
    recent_trade_supply_area_pyeong: float | None  # 그 평형의 공급 평형. 건축물대장으로 못 채웠으면 null

    @classmethod
    def from_domain(cls, a: Auction) -> "AuctionOut":
        ratio = round(a.min_price * 100 / a.appraisal_price) if a.min_price is not None and a.appraisal_price else None
        return cls(
            id=a.id,
            source=a.source,
            case_no=a.case_no,
            court_name=a.court_name,
            title=a.title,
            usage=a.usage,
            property_group=a.group,
            address=a.address,
            lat=a.lat,
            lng=a.lng,
            location_precise=a.location_precise,
            complex_id=a.complex_id,
            appraisal_price=a.appraisal_price,
            min_price=a.min_price,
            min_price_ratio=ratio,
            deposit=a.deposit,
            fail_count=a.fail_count,
            sale_date=a.sale_date,
            bid_start_at=a.bid_start_at,
            bid_end_at=a.bid_end_at,
            land_area_m2=a.land_area_m2,
            land_area_pyeong=m2_to_pyeong(a.land_area_m2) if a.land_area_m2 is not None else None,
            building_area_m2=a.building_area_m2,
            building_area_pyeong=m2_to_pyeong(a.building_area_m2) if a.building_area_m2 is not None else None,
            is_share=a.is_share,
            status=a.status,
            is_closed=a.closed_at is not None,
            recent_trade_price=a.recent_trade.price if a.recent_trade else None,
            recent_trade_date=a.recent_trade.contract_date if a.recent_trade else None,
            recent_trade_area_m2=a.recent_trade.exclusive_area_m2 if a.recent_trade else None,
            recent_trade_count=a.recent_trade.count if a.recent_trade else None,
            recent_trade_avg_price=a.recent_trade.avg_price if a.recent_trade else None,
            recent_trade_supply_area_pyeong=a.recent_trade.supply_area_pyeong if a.recent_trade else None,
        )


class ScheduleOut(BaseModel):
    date: date | None
    kind: str | None
    min_price: int | None
    result: str | None
    sale_price: int | None


class SaleResultOut(BaseModel):
    date: date | None
    result: str | None
    bidders: int | None
    top_price: int | None


class AuctionDetailOut(AuctionOut):
    schedule: list[ScheduleOut]
    results: list[SaleResultOut]

    @classmethod
    def from_domain(cls, a: Auction) -> "AuctionDetailOut":
        base = AuctionOut.from_domain(a).model_dump()
        return cls(
            **base,
            schedule=[ScheduleOut.model_validate(s) for s in a.schedule],
            results=[SaleResultOut.model_validate(r) for r in a.results],
        )


class AuctionsResponse(BaseModel):
    data: list[AuctionOut]
    meta: AuctionPageMeta


class AuctionResponse(BaseModel):
    data: AuctionDetailOut
    meta: AuctionMeta

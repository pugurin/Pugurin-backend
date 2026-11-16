from datetime import datetime

from pydantic import BaseModel

from app.schemas.auction import ATTRIBUTION, ATTRIBUTION_URL, AuctionOut
from app.schemas.complex import ComplexOut, TransactionOut
from app.services.interest_service import InterestedAuction, InterestedComplex


class InterestAddedOut(BaseModel):
    target_id: str
    added_at: datetime


class InterestAddedResponse(BaseModel):
    data: InterestAddedOut


class InterestIdsOut(BaseModel):
    complexes: list[str]  # 최근 등록한 것부터
    auctions: list[str]


class InterestIdsResponse(BaseModel):
    data: InterestIdsOut


class InterestListMeta(BaseModel):
    total: int
    max_items: int  # 종류별 등록 한도


class InterestComplexOut(BaseModel):
    complex: ComplexOut
    latest_sale: TransactionOut | None  # 가장 최근 매매(해제 제외). 없으면 null
    added_at: datetime

    @classmethod
    def from_domain(cls, item: InterestedComplex) -> "InterestComplexOut":
        return cls(
            complex=ComplexOut.from_domain(item.complex),
            latest_sale=TransactionOut.from_domain(item.latest_sale) if item.latest_sale else None,
            added_at=item.added_at,
        )


class InterestComplexesResponse(BaseModel):
    data: list[InterestComplexOut]
    meta: InterestListMeta


class InterestAuctionOut(BaseModel):
    auction: AuctionOut
    added_at: datetime

    @classmethod
    def from_domain(cls, item: InterestedAuction) -> "InterestAuctionOut":
        return cls(auction=AuctionOut.from_domain(item.auction), added_at=item.added_at)


class InterestAuctionsMeta(InterestListMeta):
    source_attribution: str = ATTRIBUTION  # 화면에 출처로 표시해야 한다
    source_url: str = ATTRIBUTION_URL


class InterestAuctionsResponse(BaseModel):
    data: list[InterestAuctionOut]
    meta: InterestAuctionsMeta

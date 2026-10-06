import hashlib
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Protocol

from app.repositories.ports import SourceUnavailable
from app.repositories.types import DealType, PropertyType, TradeMethod


class QuotaExceeded(SourceUnavailable):
    """일일 호출 한도 초과. 다음 실행으로 이월한다."""


class SourceAuthError(SourceUnavailable):
    """서비스키 미등록·만료·미설정 등 인증 문제."""


class SourceKind(StrEnum):
    apt_trade = "apt_trade"
    apt_rent = "apt_rent"
    offi_trade = "offi_trade"
    offi_rent = "offi_rent"
    rh_trade = "rh_trade"
    rh_rent = "rh_rent"
    land_trade = "land_trade"

    @property
    def endpoint(self) -> str:
        return {
            "apt_trade": "RTMSDataSvcAptTradeDev/getRTMSDataSvcAptTradeDev",
            "apt_rent": "RTMSDataSvcAptRent/getRTMSDataSvcAptRent",
            "offi_trade": "RTMSDataSvcOffiTrade/getRTMSDataSvcOffiTrade",
            "offi_rent": "RTMSDataSvcOffiRent/getRTMSDataSvcOffiRent",
            "rh_trade": "RTMSDataSvcRHTrade/getRTMSDataSvcRHTrade",
            "rh_rent": "RTMSDataSvcRHRent/getRTMSDataSvcRHRent",
            "land_trade": "RTMSDataSvcLandTrade/getRTMSDataSvcLandTrade",
        }[self.value]

    @property
    def is_rent(self) -> bool:
        return self.value.endswith("_rent")

    @property
    def property_type(self) -> PropertyType:
        return {
            "apt": PropertyType.apartment,
            "offi": PropertyType.officetel,
            "rh": PropertyType.villa,
            "land": PropertyType.land,
        }[self.value.split("_")[0]]


@dataclass(frozen=True)
class RawTrade:
    """원천 응답 한 행을 타입만 입히고 의미는 바꾸지 않은 레코드. 금액은 만원, 면적은 ㎡ 그대로다."""

    kind: SourceKind
    sgg_cd: str
    umd_nm: str
    jibun: str  # 토지는 '8**'처럼 가려져 온다
    contract_date: date
    umd_cd: str | None = None  # 아파트 매매만 제공
    name: str | None = None  # 단지·건물명 (토지는 없음)
    complex_seq: str | None = None  # 아파트만 제공하는 단지 식별자(aptSeq)
    apt_dong: str | None = None
    build_year: int | None = None
    exclusive_area_m2: float | None = None
    land_area_m2: float | None = None  # 토지 거래면적, 연립다세대 매매의 대지권 면적
    floor: int | None = None
    amount_man: int | None = None  # 매매가
    deposit_man: int | None = None
    monthly_rent_man: int | None = None
    trade_method: TradeMethod | None = None
    is_cancelled: bool = False
    cancelled_date: date | None = None
    registered_date: date | None = None
    jimok: str | None = None
    land_use: str | None = None  # 토지 거래의 용도지역
    house_type: str | None = None
    contract_type: str | None = None  # 신규/갱신
    contract_term: str | None = None
    use_renewal_right: str | None = None
    land_leasehold: bool = False
    ordinal: int = 0  # 같은 응답 안의 완전히 같은 행을 구분하는 순번
    raw: dict[str, str] = field(default_factory=dict, compare=False, hash=False, repr=False)

    @property
    def deal_type(self) -> DealType:
        if not self.kind.is_rent:
            return DealType.sale
        return DealType.jeonse if not self.monthly_rent_man else DealType.monthly

    @property
    def property_type(self) -> PropertyType:
        return self.kind.property_type

    @property
    def identity(self) -> tuple:
        """순번을 뺀 거래 식별 값. 해제·등기일처럼 나중에 바뀌는 값은 넣지 않는다."""
        area = self.exclusive_area_m2 if self.exclusive_area_m2 is not None else self.land_area_m2
        return (
            self.kind.value,
            self.sgg_cd,
            self.umd_nm,
            self.jibun,
            self.name or "",
            self.complex_seq or "",
            self.apt_dong or "",
            "" if self.floor is None else self.floor,
            "" if area is None else f"{area:.4f}",
            self.contract_date.isoformat(),
            "" if self.amount_man is None else self.amount_man,
            "" if self.deposit_man is None else self.deposit_man,
            "" if self.monthly_rent_man is None else self.monthly_rent_man,
        )

    @property
    def source_key(self) -> str:
        return hashlib.sha1("|".join(map(str, (*self.identity, self.ordinal))).encode()).hexdigest()


class TradeSource(Protocol):
    @property
    def calls_made(self) -> int: ...

    async def fetch_month(self, kind: SourceKind, lawd_cd: str, deal_ymd: str) -> list[RawTrade]:
        """시군구 5자리 코드 + 계약월(YYYYMM)의 모든 거래. 페이징은 구현이 처리한다."""
        ...

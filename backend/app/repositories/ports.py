from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.core.geo import BBox
from app.repositories.types import (
    Account,
    AddressHit,
    Complex,
    ComplexMarkerRow,
    ComplexSearchHit,
    DealType,
    GlossaryCategory,
    GlossaryTerm,
    Level,
    ParcelData,
    ParcelMarkerRow,
    PropertyType,
    RegionAggregate,
    RegionRef,
    RegionSearchHit,
    RotateOutcome,
    SignupClaim,
    SocialProvider,
    StatsResult,
    Transaction,
    TransactionFilter,
    TransactionPage,
    ZoningLimits,
)


class SourceUnavailable(Exception):
    """외부 데이터 소스 장애."""


class MarketRepository(Protocol):
    @property
    def data_as_of(self) -> datetime: ...

    async def aggregate_regions(self, level: Level, bbox: BBox, flt: TransactionFilter) -> list[RegionAggregate]: ...

    async def complex_markers(self, bbox: BBox, flt: TransactionFilter) -> list[ComplexMarkerRow]: ...

    async def parcel_markers(self, bbox: BBox, flt: TransactionFilter) -> list[ParcelMarkerRow]: ...

    async def get_complex(self, complex_id: UUID) -> Complex | None: ...

    async def get_region(self, region_code: str) -> RegionRef | None:
        """시군구(5자리)·법정동(10자리) 이름과 중심점. 모르는 지역이면 None."""
        ...

    async def region_stats(
        self,
        region_code: str,
        property_type: PropertyType,
        deal_type: DealType,
        period_months: int,
        exclude_direct: bool,
        *,
        include_renewals: bool = False,
    ) -> StatsResult | None:
        """시군구(5자리)·법정동(10자리) 통계. 모르는 지역이면 None."""
        ...

    async def complex_stats(
        self,
        complex_id: UUID,
        deal_type: DealType,
        period_months: int,
        exclude_direct: bool,
        *,
        include_renewals: bool = False,
    ) -> StatsResult | None: ...

    async def list_transactions(
        self,
        flt: TransactionFilter,
        *,
        bbox: BBox | None,
        region_code: str | None,
        include_cancelled: bool,
        sort: str,
        offset: int,
        limit: int,
    ) -> TransactionPage: ...

    async def get_transaction(self, transaction_id: UUID) -> Transaction | None: ...

    async def search_complexes(self, query: str, limit: int) -> list[ComplexSearchHit]: ...

    async def search_regions(self, query: str, limit: int) -> list[RegionSearchHit]: ...

    async def complex_transactions(
        self,
        complex_id: UUID,
        *,
        deal_type: DealType | None,
        exclude_direct: bool,
        include_cancelled: bool,
        sort: str,
        offset: int,
        limit: int,
        include_renewals: bool = False,
    ) -> TransactionPage: ...


class AddressSearch(Protocol):
    async def search_addresses(self, query: str, limit: int) -> list[AddressHit]:
        """주소(지번·도로명) 검색. 부산 밖 결과는 제외한다. 장애 시 SourceUnavailable."""
        ...


class GlossaryRepository(Protocol):
    async def list_terms(
        self, *, q: str | None, category: GlossaryCategory | None, popular: bool | None
    ) -> list[GlossaryTerm]: ...

    async def get_term(self, term_id: UUID) -> GlossaryTerm | None: ...

    async def get_term_by_name(self, term: str) -> GlossaryTerm | None: ...


class ParcelSource(Protocol):
    async def resolve_pnu(self, lat: float, lng: float) -> str | None:
        """좌표가 속한 필지의 PNU. 필지가 없으면(도로·바다 등) None. 장애 시 SourceUnavailable."""
        ...

    async def fetch_parcel(self, pnu: str) -> ParcelData | None:
        """PNU로 필지 정보 조회. 존재하지 않으면 None. 장애 시 SourceUnavailable."""
        ...


class ZoningRuleRepository(Protocol):
    source_label: str

    def limits(self, zone_type: str) -> ZoningLimits | None: ...


class AccountRepository(Protocol):
    """사용자·소셜 연결·refresh token·가입 토큰. 토큰은 해시로만 주고받는다. 탈퇴한 사용자는 없는 사용자로 본다."""

    async def find_by_identity(self, provider: SocialProvider, provider_user_id: str) -> Account | None: ...

    async def get(self, user_id: UUID) -> Account | None: ...

    async def create(self, claim: SignupClaim, nickname: str, *, marketing: bool, now: datetime) -> Account:
        """같은 소셜 계정이 이미(동시에) 가입했으면 새로 만들지 않고 그 계정을 돌려준다."""
        ...

    async def update(self, user_id: UUID, *, nickname: str | None, settings: dict[str, bool] | None) -> Account | None:
        """None인 항목은 그대로 둔다. settings는 준 키만 덮어쓴다."""
        ...

    async def withdraw(self, user_id: UUID, *, now: datetime) -> None:
        """한 트랜잭션에서 소셜 연결 삭제, 개인 항목 익명화, deleted_at 기록, refresh token 전부 폐기."""
        ...

    async def save_signup_token(
        self, token_hash: str, claim: SignupClaim, *, expires_at: datetime, now: datetime
    ) -> None: ...

    async def take_signup_token(self, token_hash: str, *, now: datetime) -> SignupClaim | None:
        """1회용: 꺼내면서 지운다. 없거나 만료됐으면 None."""
        ...

    async def add_refresh_token(
        self,
        user_id: UUID,
        family_id: UUID,
        token_hash: str,
        device_id: UUID | None,
        *,
        now: datetime,
        expires_at: datetime,
    ) -> None: ...

    async def rotate_refresh_token(
        self, old_hash: str, new_hash: str, device_id: UUID | None, *, now: datetime, expires_at: datetime
    ) -> tuple[RotateOutcome, UUID | None]:
        """살아 있는 토큰이면 폐기하고 같은 family로 새 토큰을 넣는다.
        이미 폐기된 토큰이면 그 사용자의 refresh token을 모두 폐기하고 reused."""
        ...

    async def revoke_refresh_token(self, token_hash: str, *, now: datetime) -> None: ...

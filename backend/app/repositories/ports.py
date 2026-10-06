from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.core.geo import BBox
from app.repositories.types import (
    Complex,
    ComplexMarkerRow,
    DealType,
    GlossaryCategory,
    GlossaryTerm,
    Level,
    ParcelData,
    ParcelMarkerRow,
    RegionAggregate,
    TransactionFilter,
    TransactionPage,
    ZoningLimits,
)


class SourceUnavailable(Exception):
    """외부 데이터 소스(VWorld 등) 장애."""


class MarketRepository(Protocol):
    @property
    def data_as_of(self) -> datetime: ...

    async def aggregate_regions(self, level: Level, bbox: BBox, flt: TransactionFilter) -> list[RegionAggregate]: ...

    async def complex_markers(self, bbox: BBox, flt: TransactionFilter) -> list[ComplexMarkerRow]: ...

    async def parcel_markers(self, bbox: BBox, flt: TransactionFilter) -> list[ParcelMarkerRow]: ...

    async def get_complex(self, complex_id: UUID) -> Complex | None: ...

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
    ) -> TransactionPage: ...


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

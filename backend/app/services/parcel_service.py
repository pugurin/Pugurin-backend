import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from app.core.cache import TTLCache
from app.core.config import Settings
from app.core.dates import Clock
from app.repositories.ports import GlossaryRepository, ParcelSource, SourceUnavailable, ZoningRuleRepository
from app.repositories.types import ParcelData, ZoningLimits, ZoningPart

BUSAN_PNU_PREFIX = "26"

GLOSSARY_FIELD_TERMS = {
    "land_category": "지목",
    "official_land_price_per_m2": "공시지가",
    "road_side": "도로접면",
    "zone_type": "용도지역",
    "max_building_coverage_ratio": "건폐율",
    "max_floor_area_ratio": "용적률",
}


@dataclass(frozen=True)
class ZoningView:
    part: ZoningPart
    limits: ZoningLimits | None


@dataclass(frozen=True)
class ParcelView:
    data: ParcelData
    zonings: list[ZoningView]
    ratio_source: str
    restriction_term_ids: list[UUID | None]
    glossary: dict[str, UUID]


@dataclass(frozen=True)
class ParcelResult:
    parcel: ParcelView | None = None
    cached_at: datetime | None = None
    unavailable_reason: str | None = None


class ParcelService:
    def __init__(
        self,
        source: ParcelSource,
        glossary: GlossaryRepository,
        zoning_rules: ZoningRuleRepository,
        settings: Settings,
        clock: Clock,
    ):
        self._source = source
        self._glossary = glossary
        self._rules = zoning_rules
        self._settings = settings
        self._cache: TTLCache[ParcelData | None] = TTLCache(clock)

    async def lookup(self, lat: float, lng: float) -> ParcelResult:
        min_lng, min_lat, max_lng, max_lat = self._settings.busan_bbox
        if not (min_lng <= lng <= max_lng and min_lat <= lat <= max_lat):
            return ParcelResult(unavailable_reason="out_of_service_area")

        grid_key = f"no_parcel:{math.floor(lat / 0.0001)}:{math.floor(lng / 0.0001)}"
        if (hit := self._cache.get(grid_key)) is not None:
            return ParcelResult(cached_at=hit[1], unavailable_reason="no_parcel")

        try:
            pnu = await self._source.resolve_pnu(lat, lng)
        except SourceUnavailable:
            return ParcelResult(unavailable_reason="source_unavailable")
        if pnu is None:
            cached_at = self._cache.set(grid_key, None, self._negative_ttl)
            return ParcelResult(cached_at=cached_at, unavailable_reason="no_parcel")
        return await self.by_pnu(pnu)

    async def by_pnu(self, pnu: str) -> ParcelResult:
        if not pnu.startswith(BUSAN_PNU_PREFIX):
            return ParcelResult(unavailable_reason="out_of_service_area")

        if (hit := self._cache.get(pnu)) is not None:
            data, cached_at = hit
            if data is None:
                return ParcelResult(cached_at=cached_at, unavailable_reason="no_parcel")
            return ParcelResult(await self._view(data), cached_at)

        try:
            data = await self._source.fetch_parcel(pnu)
        except SourceUnavailable:
            return ParcelResult(unavailable_reason="source_unavailable")
        if data is None:
            cached_at = self._cache.set(pnu, None, self._negative_ttl)
            return ParcelResult(cached_at=cached_at, unavailable_reason="no_parcel")
        cached_at = self._cache.set(pnu, data, timedelta(days=self._settings.parcel_cache_ttl_days))
        return ParcelResult(await self._view(data), cached_at)

    @property
    def _negative_ttl(self) -> timedelta:
        return timedelta(days=self._settings.parcel_negative_cache_ttl_days)

    async def _view(self, data: ParcelData) -> ParcelView:
        zonings = sorted(data.zonings, key=lambda z: -(z.area_ratio if z.area_ratio is not None else -1))
        glossary: dict[str, UUID] = {}
        for field_name, term in GLOSSARY_FIELD_TERMS.items():
            if (found := await self._glossary.get_term_by_name(term)) is not None:
                glossary[field_name] = found.id
        term_ids: list[UUID | None] = []
        for r in data.restrictions:
            found = await self._glossary.get_term_by_name(r.name)
            term_ids.append(found.id if found else None)
        return ParcelView(
            data=data,
            zonings=[ZoningView(z, self._rules.limits(z.zone_type)) for z in zonings],
            ratio_source=self._rules.source_label,
            restriction_term_ids=term_ids,
            glossary=glossary,
        )

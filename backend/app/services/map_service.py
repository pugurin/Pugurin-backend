from dataclasses import dataclass, field
from datetime import datetime

from app.core.config import Settings
from app.core.geo import BBox
from app.repositories.ports import MarketRepository
from app.repositories.types import (
    ComplexMarkerRow,
    Level,
    ParcelMarkerRow,
    PropertyType,
    RegionAggregate,
    TransactionFilter,
)


@dataclass
class MarkersResult:
    level: Level
    data_as_of: datetime
    regions: list[RegionAggregate] = field(default_factory=list)
    complexes: list[ComplexMarkerRow] = field(default_factory=list)
    parcels: list[ParcelMarkerRow] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.regions) + len(self.complexes) + len(self.parcels)


class MapService:
    def __init__(self, repo: MarketRepository, settings: Settings):
        self._repo = repo
        self._settings = settings

    def level_for_zoom(self, zoom: int) -> Level:
        if zoom < self._settings.zoom_dong_min:
            return Level.sigungu
        if zoom < self._settings.zoom_complex_min:
            return Level.dong
        return Level.complex

    async def markers(self, bbox: BBox, zoom: int, flt: TransactionFilter) -> MarkersResult:
        level = self.level_for_zoom(zoom)
        while True:
            result = await self._build(level, bbox, flt)
            if result.count <= self._settings.max_markers or level == Level.sigungu:
                return result
            level = Level.dong if level == Level.complex else Level.sigungu

    async def _build(self, level: Level, bbox: BBox, flt: TransactionFilter) -> MarkersResult:
        result = MarkersResult(level=level, data_as_of=self._repo.data_as_of)
        is_land = flt.property_type == PropertyType.land
        if level == Level.sigungu:
            result.regions = await self._repo.aggregate_regions(Level.sigungu, bbox, flt)
        elif level == Level.dong or is_land:
            # 토지는 가까이 확대해도 동 집계를 유지하고, 지번이 공개된 거래만 필지 마커로 더한다
            result.regions = await self._repo.aggregate_regions(Level.dong, bbox, flt)
            if level == Level.complex:
                result.parcels = await self._repo.parcel_markers(bbox, flt)
        else:
            result.complexes = await self._repo.complex_markers(bbox, flt)
        return result

from datetime import datetime

from app.core.errors import data_not_ready, not_found
from app.core.geo import BBox
from app.repositories.postgres.redevelopments import PostgresRedevelopmentRepository
from app.repositories.types import Redevelopment, RedevelopmentFilter, RedevelopmentPage, RedevelopmentZone

NOT_READY = "정비사업 구역 데이터가 아직 없어요. 동기화가 끝나면 다시 시도해 주세요"
ZONES_NOT_READY = "정비구역 경계 데이터가 아직 없어요"
ZONE_LIMIT = 300


def simplify_tolerance(zoom: int | None) -> float:
    """화면 1px쯤(도 단위)보다 작은 굴곡은 버린다. 앱 줌 = 18 - 카카오 레벨, 1px = 2^(15-zoom) m."""
    if zoom is None:
        return 0.0
    meters_per_px = 2.0 ** (15 - zoom)
    return meters_per_px / 111_000


class RedevelopmentService:
    """정비사업 구역은 DB에만 있다. DB가 없거나 동기화가 한 번도 끝나지 않았으면 503 DATA_NOT_READY."""

    def __init__(self, repo: PostgresRedevelopmentRepository | None):
        self._repo = repo

    async def _ready(self) -> tuple[PostgresRedevelopmentRepository, datetime]:
        if self._repo is None:
            raise data_not_ready(NOT_READY)
        as_of = await self._repo.data_as_of()
        if as_of is None:
            raise data_not_ready(NOT_READY)
        return self._repo, as_of

    async def search(
        self, flt: RedevelopmentFilter, *, page: int, page_size: int
    ) -> tuple[RedevelopmentPage, datetime]:
        repo, as_of = await self._ready()
        return await repo.search(flt, offset=(page - 1) * page_size, limit=page_size), as_of

    async def get(self, zone_id: str) -> tuple[Redevelopment, datetime]:
        repo, as_of = await self._ready()
        found = await repo.get(zone_id)
        if found is None:
            raise not_found("정비사업 구역을 찾을 수 없습니다")
        return found, as_of

    async def zones(self, bbox: BBox, zoom: int | None) -> tuple[list[RedevelopmentZone], datetime]:
        """화면 안의 정비구역 경계. 경계 파일을 한 번도 안 실었으면 503 DATA_NOT_READY."""
        if self._repo is None:
            raise data_not_ready(ZONES_NOT_READY)
        as_of = await self._repo.zones_as_of()
        if as_of is None:
            raise data_not_ready(ZONES_NOT_READY)
        return await self._repo.zones_in(bbox, tolerance=simplify_tolerance(zoom), limit=ZONE_LIMIT), as_of

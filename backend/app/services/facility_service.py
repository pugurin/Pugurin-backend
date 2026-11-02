from datetime import datetime

from app.core.dates import Clock
from app.core.errors import data_not_ready
from app.core.geo import BBox
from app.repositories.postgres.facilities import PostgresFacilityRepository
from app.repositories.types import Hospital, HospitalTier, Nearby, School, SchoolLevel

NOT_READY = "학교·병원 데이터가 아직 없어요. 동기화가 끝나면 다시 시도해 주세요"

SCHOOL_RADIUS_M = 3000  # 초·중·고 각각 이 안에서 가까운 순
SCHOOLS_PER_LEVEL = 3
HOSPITAL_RADIUS_M = 1000  # 병원 수를 세는 반경
NEAREST_HOSPITALS = 5
MAX_LAYER_ITEMS = 500


class FacilityService:
    """학교·병원은 DB에만 있다. DB가 없거나 동기화가 한 번도 끝나지 않았으면 503 DATA_NOT_READY."""

    def __init__(self, repo: PostgresFacilityRepository | None, clock: Clock):
        self._repo = repo
        self._clock = clock

    async def _ready(self) -> tuple[PostgresFacilityRepository, datetime]:
        if self._repo is None:
            raise data_not_ready(NOT_READY)
        as_of = await self._repo.data_as_of()
        if as_of is None:
            raise data_not_ready(NOT_READY)
        return self._repo, as_of

    async def nearby(self, lat: float, lng: float) -> tuple[Nearby, datetime]:
        repo, as_of = await self._ready()
        found = await repo.nearby(
            lat,
            lng,
            today=self._clock().date(),
            school_radius_m=SCHOOL_RADIUS_M,
            schools_per_level=SCHOOLS_PER_LEVEL,
            hospital_radius_m=HOSPITAL_RADIUS_M,
            nearest_hospitals=NEAREST_HOSPITALS,
        )
        return found, as_of

    async def schools(self, bbox: BBox, levels: tuple[SchoolLevel, ...]) -> tuple[list[School], datetime]:
        repo, as_of = await self._ready()
        return await repo.schools_in(bbox, levels, today=self._clock().date(), limit=MAX_LAYER_ITEMS), as_of

    async def hospitals(self, bbox: BBox, tiers: tuple[HospitalTier, ...]) -> tuple[list[Hospital], datetime]:
        repo, as_of = await self._ready()
        return await repo.hospitals_in(bbox, tiers, limit=MAX_LAYER_ITEMS), as_of

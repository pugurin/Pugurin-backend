import logging
from collections.abc import Callable
from datetime import date

from sqlalchemy.exc import SQLAlchemyError

from app.core.text import normalize
from app.repositories.memory.market import match_score
from app.repositories.ports import (
    AddressSearch,
    MarketRepository,
    SchoolSearch,
    SourceUnavailable,
    StationSearch,
)
from app.repositories.types import AddressHit, ComplexSearchHit, RegionSearchHit, School, StationHit

logger = logging.getLogger("uvicorn.error")

ADDRESS_SCORE = 50
ADDRESS_LIMIT = 3

SearchResult = ComplexSearchHit | RegionSearchHit | AddressHit | StationHit | School

# 점수가 같을 때의 순서: 역 → 학교 → 단지 → 주소. 동네(구·동)는 점수와 상관없이 맨 위
_STATION, _SCHOOL, _COMPLEX, _ADDRESS = range(4)


def station_score(query: str, name: str) -> int:
    """역 이름(노선 앞 부분)과 비교한다. '서면'과 '서면역'은 모두 '서면역'에 정확 일치로 본다.
    '서면역 1호선'처럼 노선까지 친 검색어는 노선을 포함한 전체 이름과도 비교한다."""
    q = normalize(query)
    base = normalize(name.split()[0]) if name.split() else ""
    if q and (q == base or f"{q}역" == base):
        return 100
    return max(match_score(q, base), match_score(q, normalize(name)))


class SearchService:
    def __init__(
        self,
        repo: MarketRepository,
        address_search: AddressSearch | None,
        *,
        station_search: StationSearch | None = None,
        school_search: SchoolSearch | None = None,
        today: Callable[[], date] | None = None,
    ):
        self._repo = repo
        self._address_search = address_search
        self._station_search = station_search
        self._school_search = school_search
        self._today = today or date.today

    async def search(self, q: str, limit: int) -> list[SearchResult]:
        text = q.strip()
        if len(normalize(text)) < 2:
            return []
        regions = await self._repo.search_regions(text, limit)
        complexes = await self._repo.search_complexes(text, limit)
        addresses = await self._addresses(text, {h.complex.pnu for h in complexes if h.complex.pnu})
        stations = await self._stations(text, limit)
        schools = await self._schools(text, limit)

        # 동네가 먼저(동네끼리는 점수 순), 나머지는 점수 높은 순, 같으면 역 → 학교 → 단지 → 주소,
        # 단지끼리는 거래가 많은 순
        key = normalize(text)
        ranked: list[tuple[tuple, SearchResult]] = [
            *(((0, -h.score, 0, 0, h.name), h) for h in regions),
            *(((1, -s, _STATION, 0, h.name), h) for h in stations if (s := station_score(text, h.name))),
            *(((1, -s, _SCHOOL, 0, h.name), h) for h in schools if (s := match_score(key, normalize(h.name)))),
            *(((1, -h.score, _COMPLEX, -h.popularity, h.complex.name), h) for h in complexes),
            *(((1, -ADDRESS_SCORE, _ADDRESS, 0, h.address), h) for h in addresses),
        ]
        ranked.sort(key=lambda pair: pair[0])
        return [hit for _, hit in ranked[:limit]]

    async def _addresses(self, text: str, known_pnus: set[str]) -> list[AddressHit]:
        # 지번·도로명 번호가 있는 검색어만 외부 지오코딩을 부른다(호출 한도 절약)
        if self._address_search is None or not any(ch.isdigit() for ch in text):
            return []
        try:
            found = await self._address_search.search_addresses(text, ADDRESS_LIMIT)
        except SourceUnavailable as e:
            logger.warning("주소 검색을 건너뜁니다: %s", e)
            return []
        return [h for h in found if h.pnu is None or h.pnu not in known_pnus]

    async def _stations(self, text: str, limit: int) -> list[StationHit]:
        if self._station_search is None:
            return []
        try:
            return await self._station_search.search_stations(text, limit)
        except SourceUnavailable as e:
            logger.warning("역 검색을 건너뜁니다: %s", e)
            return []

    async def _schools(self, text: str, limit: int) -> list[School]:
        if self._school_search is None:
            return []
        try:
            found = await self._school_search.search_schools(text, limit, today=self._today())
        except (SourceUnavailable, SQLAlchemyError, OSError) as e:  # DB가 잠깐 안 돼도 나머지 결과는 준다
            logger.warning("학교 검색을 건너뜁니다: %s", e)
            return []
        return [s for s in found if s.lat is not None and s.lng is not None]

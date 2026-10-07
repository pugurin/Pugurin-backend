import logging

from app.core.text import normalize
from app.repositories.ports import AddressSearch, MarketRepository, SourceUnavailable
from app.repositories.types import AddressHit, ComplexSearchHit, RegionSearchHit

logger = logging.getLogger("uvicorn.error")

ADDRESS_SCORE = 50
ADDRESS_LIMIT = 3

SearchResult = ComplexSearchHit | RegionSearchHit | AddressHit


class SearchService:
    def __init__(self, repo: MarketRepository, address_search: AddressSearch | None):
        self._repo = repo
        self._address_search = address_search

    async def search(self, q: str, limit: int) -> list[SearchResult]:
        text = q.strip()
        if len(normalize(text)) < 2:
            return []
        regions = await self._repo.search_regions(text, limit)
        complexes = await self._repo.search_complexes(text, limit)
        addresses = await self._addresses(text, {h.complex.pnu for h in complexes if h.complex.pnu})

        # 점수 높은 순, 같으면 지역 → 단지 → 주소 순, 단지끼리는 거래가 많은 순
        ranked: list[tuple[tuple, SearchResult]] = [
            *(((-h.score, 0, 0, h.name), h) for h in regions),
            *(((-h.score, 1, -h.popularity, h.complex.name), h) for h in complexes),
            *(((-ADDRESS_SCORE, 2, 0, h.address), h) for h in addresses),
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

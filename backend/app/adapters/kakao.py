import asyncio
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

import httpx

from app.adapters.molit.types import QuotaExceeded, SourceAuthError
from app.core.text import normalize
from app.repositories.ports import SourceUnavailable
from app.repositories.types import AddressHit, StationHit

logger = logging.getLogger(__name__)

SUBWAY_CATEGORY = "SW8"  # 카카오 장소 분류: 지하철역
BUSAN_RECT = "128.75,34.85,129.35,35.4"  # 부산 범위(경도,위도,경도,위도). 설정의 busan_bbox와 같다
STATION_CACHE_SIZE = 1000  # 역 이름은 거의 안 바뀌어 검색어별 결과를 메모리에 둔다


@dataclass(frozen=True)
class GeoResult:
    lat: float
    lng: float
    b_code: str | None  # 법정동 코드 10자리
    pnu: str | None  # 지번까지 맞았을 때만
    precise: bool  # True: 지번 단위, False: 법정동 중심점


class Geocoder(Protocol):
    async def address(self, sgg_name: str, umd_nm: str, jibun: str) -> GeoResult | None: ...

    async def region(self, sgg_name: str, umd_nm: str) -> GeoResult | None: ...


def make_pnu(b_code: str, main_no: str, sub_no: str, mountain: bool) -> str | None:
    if len(b_code) != 10 or not main_no.isdigit() or (sub_no and not sub_no.isdigit()):
        return None
    return f"{b_code}{2 if mountain else 1}{int(main_no):04d}{int(sub_no or 0):04d}"


def parse_documents(documents: list[dict], *, want_address: bool) -> GeoResult | None:
    """카카오 주소 검색 결과에서 쓸 수 있는 첫 문서를 고른다."""
    for doc in documents:
        addr = doc.get("address") or {}
        b_code = (addr.get("b_code") or "").strip() or None
        try:
            lng, lat = float(doc["x"]), float(doc["y"])
        except (KeyError, ValueError):
            continue
        if doc.get("address_type") in ("REGION_ADDR", "ROAD_ADDR") and want_address and b_code:
            pnu = make_pnu(
                b_code, addr.get("main_address_no", ""), addr.get("sub_address_no", ""), addr.get("mountain_yn") == "Y"
            )
            if pnu:
                return GeoResult(lat, lng, b_code, pnu, precise=True)
        if b_code:  # 지번을 못 찾았어도 법정동은 찾은 경우
            return GeoResult(lat, lng, b_code, None, precise=False)
    return None


class KakaoGeocoder:
    """카카오 로컬 주소 검색(REST 키). 지번 주소 → 좌표·법정동 코드·PNU, 법정동 이름 → 중심점·법정동 코드."""

    def __init__(
        self,
        rest_key: str,
        *,
        http: httpx.AsyncClient | None = None,
        base_url: str = "https://dapi.kakao.com",
        retries: int = 2,
        backoff_seconds: float = 0.5,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._key = rest_key
        self._http = http or httpx.AsyncClient(timeout=15)
        self._url = f"{base_url.rstrip('/')}/v2/local/search/address.json"
        self._keyword_url = f"{base_url.rstrip('/')}/v2/local/search/keyword.json"
        self._stations: OrderedDict[str, list[StationHit]] = OrderedDict()
        self._retries = retries
        self._backoff = backoff_seconds
        self._sleep = sleep
        self.calls_made = 0

    async def address(self, sgg_name: str, umd_nm: str, jibun: str) -> GeoResult | None:
        return parse_documents(await self._search(f"부산 {sgg_name} {umd_nm} {jibun}"), want_address=True)

    async def region(self, sgg_name: str, umd_nm: str) -> GeoResult | None:
        return parse_documents(await self._search(f"부산 {sgg_name} {umd_nm}".strip()), want_address=False)

    async def lookup(self, query: str) -> GeoResult | None:
        """완성된 주소 한 줄(예: "부산 해운대구 우동 639-3") → 좌표·법정동 코드·PNU."""
        return parse_documents(await self._search(query), want_address=True)

    async def search_addresses(self, query: str, limit: int) -> list[AddressHit]:
        """자유 입력 주소를 검색한다. 부산 밖(법정동 코드가 26으로 시작하지 않는 것)은 버린다."""
        text = query.strip()
        if not text.startswith("부산"):
            text = f"부산 {text}"
        hits: list[AddressHit] = []
        for doc in await self._search(text):
            addr = doc.get("address") or {}
            b_code = (addr.get("b_code") or "").strip()
            if not b_code.startswith("26") or doc.get("address_type") not in ("REGION_ADDR", "ROAD_ADDR"):
                continue
            try:
                lng, lat = float(doc["x"]), float(doc["y"])
            except (KeyError, ValueError):
                continue
            pnu = make_pnu(
                b_code, addr.get("main_address_no", ""), addr.get("sub_address_no", ""), addr.get("mountain_yn") == "Y"
            )
            hits.append(AddressHit(addr.get("address_name") or doc.get("address_name", ""), lat, lng, pnu))
            if len(hits) >= limit:
                break
        return hits

    async def search_stations(self, query: str, limit: int) -> list[StationHit]:
        """부산 안의 지하철역을 이름으로 찾는다(카카오 키워드 검색, 지하철역 분류만). 검색어별로 기억해 둔다."""
        key = normalize(query)
        if key not in self._stations:
            params = {"query": query.strip(), "category_group_code": SUBWAY_CATEGORY, "rect": BUSAN_RECT, "size": 15}
            documents = await self._get(self._keyword_url, params)
            self._stations[key] = [h for h in map(_station, documents) if h]
            if len(self._stations) > STATION_CACHE_SIZE:
                self._stations.popitem(last=False)
        self._stations.move_to_end(key)
        return self._stations[key][:limit]

    async def _search(self, query: str) -> list[dict]:
        return await self._get(self._url, {"query": query})

    async def _get(self, url: str, params: dict) -> list[dict]:
        last: Exception | None = None
        for attempt in range(self._retries + 1):
            self.calls_made += 1
            try:
                response = await self._http.get(url, params=params, headers={"Authorization": f"KakaoAK {self._key}"})
            except httpx.TransportError as e:
                last = e
            else:
                code = response.status_code
                if code in (401, 403):
                    raise SourceAuthError(f"카카오 인증 거부(HTTP {code})")
                if code == 429:
                    raise QuotaExceeded("카카오 호출 한도 초과(HTTP 429)")
                if code >= 500:
                    last = SourceUnavailable(f"카카오 서버 오류(HTTP {code})")
                elif code >= 400:
                    return []
                else:
                    return response.json().get("documents", [])
            if attempt < self._retries:
                await self._sleep(self._backoff * 2**attempt)
        raise SourceUnavailable(f"카카오 호출 실패: {last}") from last

    async def aclose(self) -> None:
        await self._http.aclose()


def _station(doc: dict) -> StationHit | None:
    """카카오 장소 한 건 → 역. 부산 밖(사각 범위에 걸친 김해·양산 등)이나 좌표가 없으면 버린다."""
    address = doc.get("road_address_name") or doc.get("address_name") or ""
    if doc.get("category_group_code") != SUBWAY_CATEGORY or not (doc.get("address_name") or "").startswith("부산"):
        return None
    try:
        lng, lat = float(doc["x"]), float(doc["y"])
    except (KeyError, ValueError):
        return None
    # 분류 예: "교통,수송 > 지하철,전철 > 부산도시철도 1호선". 노선 단계가 없으면 비운다
    parts = [p.strip() for p in (doc.get("category_name") or "").split(">")]
    line = parts[2] if len(parts) > 2 and parts[2] else None
    return StationHit(str(doc.get("id", "")), doc.get("place_name", ""), line, address, lat, lng)

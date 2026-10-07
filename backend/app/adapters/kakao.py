import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

import httpx

from app.adapters.molit.types import QuotaExceeded, SourceAuthError
from app.repositories.ports import SourceUnavailable
from app.repositories.types import AddressHit

logger = logging.getLogger(__name__)


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
        self._retries = retries
        self._backoff = backoff_seconds
        self._sleep = sleep
        self.calls_made = 0

    async def address(self, sgg_name: str, umd_nm: str, jibun: str) -> GeoResult | None:
        return parse_documents(await self._search(f"부산 {sgg_name} {umd_nm} {jibun}"), want_address=True)

    async def region(self, sgg_name: str, umd_nm: str) -> GeoResult | None:
        return parse_documents(await self._search(f"부산 {sgg_name} {umd_nm}".strip()), want_address=False)

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

    async def _search(self, query: str) -> list[dict]:
        last: Exception | None = None
        for attempt in range(self._retries + 1):
            self.calls_made += 1
            try:
                response = await self._http.get(
                    self._url, params={"query": query}, headers={"Authorization": f"KakaoAK {self._key}"}
                )
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

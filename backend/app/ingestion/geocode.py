import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

import httpx

from app.adapters.molit.types import QuotaExceeded, SourceAuthError
from app.repositories.ports import SourceUnavailable

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


def address_key(sgg_name: str, umd_nm: str, jibun: str) -> str:
    return f"addr|{sgg_name}|{umd_nm}|{jibun}"


def region_key(sgg_name: str, umd_nm: str) -> str:
    return f"region|{sgg_name}|{umd_nm}"


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


class GeocodeCache:
    """지오코딩 결과를 JSON 파일에 쌓는다. 못 찾은 주소도 기록해 같은 호출을 반복하지 않는다."""

    def __init__(self, path: Path):
        self._path = path
        self._items: dict[str, dict] = {}
        self._dirty = 0
        if path.exists():
            self._items = json.loads(path.read_text(encoding="utf-8"))

    def __contains__(self, key: str) -> bool:
        return key in self._items

    def get(self, key: str) -> GeoResult | None:
        entry = self._items.get(key)
        if not entry or entry.get("status") != "ok":
            return None
        return GeoResult(entry["lat"], entry["lng"], entry["b_code"], entry["pnu"], entry["precise"])

    def put(self, key: str, result: GeoResult | None) -> None:
        self._items[key] = {"status": "ok", **asdict(result)} if result else {"status": "not_found"}
        self._dirty += 1

    def save(self) -> None:
        if not self._dirty:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._items, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self._path)
        self._dirty = 0

    def __len__(self) -> int:
        return len(self._items)


async def geocode_missing(
    geocoder: Geocoder,
    cache: GeocodeCache,
    addresses: Iterable[tuple[str, str, str]],
    regions: Iterable[tuple[str, str]],
    *,
    concurrency: int = 8,
    on_progress: Callable[[int, int], None] | None = None,
) -> int:
    """캐시에 없는 주소만 조회해 채운다. 한도·인증 오류가 나면 지금까지의 결과를 저장하고 예외를 그대로 낸다."""
    todo: list[tuple[str, Callable[[], Awaitable[GeoResult | None]]]] = []
    for sgg, umd, jibun in dict.fromkeys(addresses):
        key = address_key(sgg, umd, jibun)
        if key not in cache:
            todo.append((key, lambda s=sgg, u=umd, j=jibun: geocoder.address(s, u, j)))
    for sgg, umd in dict.fromkeys(regions):
        key = region_key(sgg, umd)
        if key not in cache:
            todo.append((key, lambda s=sgg, u=umd: geocoder.region(s, u)))

    gate = asyncio.Semaphore(concurrency)
    done = 0
    failure: Exception | None = None

    async def work(key: str, call: Callable[[], Awaitable[GeoResult | None]]) -> None:
        nonlocal done, failure
        async with gate:
            if failure is not None:
                return
            try:
                cache.put(key, await call())
            except (QuotaExceeded, SourceAuthError, SourceUnavailable) as e:
                failure = failure or e
                return
            done += 1
            if done % 200 == 0:
                cache.save()
                if on_progress:
                    on_progress(done, len(todo))

    await asyncio.gather(*(work(k, c) for k, c in todo))
    cache.save()
    if failure is not None:
        raise failure
    return done

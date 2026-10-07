import asyncio
import json
import os
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import asdict
from pathlib import Path

from app.adapters.kakao import Geocoder, GeoResult
from app.adapters.molit.types import QuotaExceeded, SourceAuthError
from app.repositories.ports import SourceUnavailable


def address_key(sgg_name: str, umd_nm: str, jibun: str) -> str:
    return f"addr|{sgg_name}|{umd_nm}|{jibun}"


def region_key(sgg_name: str, umd_nm: str) -> str:
    return f"region|{sgg_name}|{umd_nm}"


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

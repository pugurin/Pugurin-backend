"""대중교통 길찾기(집→직장). ODsay 결과를 같은 출발·도착(소수 5자리)마다 24시간 캐시한다.

경로를 줄 수 없으면 예외 대신 `unavailable_reason`으로 알린다(200 + 빈 목록). ODsay 장애·한도·키 오류는 로그만 남긴다.
"""

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from app.adapters.molit.types import QuotaExceeded, SourceAuthError
from app.adapters.odsay import OdsayError, Route, attach_paths
from app.core.dates import Clock
from app.repositories.ports import SourceUnavailable

logger = logging.getLogger("uvicorn.error")

CACHE_SECONDS = 24 * 60 * 60
CACHE_MAX_ENTRIES = 5_000
MAX_LIMIT = 5
# 대한민국 대략 범위. 밖이면 ODsay를 부르지 않고 no_route
KOREA_LAT = (33.0, 38.7)
KOREA_LNG = (124.5, 132.0)
# ODsay 에러 코드 → 이유. 그 밖의 코드(500 등)는 source_unavailable.
# -8·-9(입력 형식·누락)는 다시 시도해도 같은 결과라 no_route로 본다(좌표 범위는 라우터가 먼저 막는다)
_TOO_CLOSE = {"-98"}
_NO_ROUTE = {"-99", "-8", "-9", "3", "4", "5", "6"}


class UnavailableReason(StrEnum):
    too_close = "too_close"
    no_route = "no_route"
    source_unavailable = "source_unavailable"


class TransitSource(Protocol):
    async def search(self, from_lng: float, from_lat: float, to_lng: float, to_lat: float) -> list[Route]: ...

    async def lanes(self, map_obj: str) -> list[tuple[int | None, list[list[float]]]]: ...


@dataclass(frozen=True)
class TransitResult:
    routes: list[Route]
    unavailable_reason: UnavailableReason | None
    data_as_of: datetime


@dataclass
class _Entry:
    expires: float
    fetched_at: datetime
    routes: list[Route]
    reason: UnavailableReason | None
    with_path: dict[int, Route] = field(default_factory=dict)  # 노선 그래픽을 붙인 경로(필요한 만큼만 부른다)


def _key(*coords: float) -> tuple[float, ...]:
    return tuple(round(c, 5) for c in coords)


def in_korea(lat: float, lng: float) -> bool:
    return KOREA_LAT[0] <= lat <= KOREA_LAT[1] and KOREA_LNG[0] <= lng <= KOREA_LNG[1]


class TransitService:
    def __init__(self, source: TransitSource | None, clock: Clock, monotonic: Callable[[], float] = time.monotonic):
        self._source = source
        self._clock = clock
        self._monotonic = monotonic
        self._cache: OrderedDict[tuple[float, ...], _Entry] = OrderedDict()
        # 같은 출발·도착을 동시에 물으면 ODsay는 한 번만 부르고 결과를 나눠 쓴다
        self._inflight: dict[tuple[float, ...], asyncio.Task[_Entry | None]] = {}

    async def routes(
        self, from_lat: float, from_lng: float, to_lat: float, to_lng: float, limit: int, *, include_path: bool = False
    ) -> TransitResult:
        """`include_path=True`일 때만 노선 그래픽(loadLane)을 경로마다 더 부른다(그만큼 호출 한도를 쓴다)."""
        limit = max(1, min(limit, MAX_LIMIT))
        key = _key(from_lat, from_lng, to_lat, to_lng)
        if not (in_korea(from_lat, from_lng) and in_korea(to_lat, to_lng)):
            return TransitResult([], UnavailableReason.no_route, self._clock())
        entry = self._cached(key)
        if entry is None:
            entry = await self._fetch_once(key)
            if entry is None:
                return TransitResult([], UnavailableReason.source_unavailable, self._clock())
        if entry.reason is not None:
            return TransitResult([], entry.reason, entry.fetched_at)
        count = min(limit, len(entry.routes))
        if not include_path:
            return TransitResult(entry.routes[:count], None, entry.fetched_at)
        return TransitResult([await self._with_path(entry, i) for i in range(count)], None, entry.fetched_at)

    async def _fetch_once(self, key) -> _Entry | None:
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.ensure_future(self._fetch(key))
            self._inflight[key] = task
            task.add_done_callback(lambda _: self._inflight.pop(key, None))
        return await asyncio.shield(task)

    def _cached(self, key) -> _Entry | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        if entry.expires <= self._monotonic():
            del self._cache[key]
            return None
        self._cache.move_to_end(key)
        return entry

    async def _fetch(self, key) -> _Entry | None:
        """ODsay를 부른다. 장애·한도·키 오류면 None(캐시하지 않아 다음 요청에서 다시 시도)."""
        if self._source is None:
            logger.warning("ODSAY_API_KEY가 없어 대중교통 길찾기를 할 수 없습니다")
            return None
        from_lat, from_lng, to_lat, to_lng = key
        reason = None
        routes: list[Route] = []
        try:
            routes = await self._source.search(from_lng, from_lat, to_lng, to_lat)
            if not routes:
                reason = UnavailableReason.no_route
        except OdsayError as e:
            if e.code in _TOO_CLOSE:
                reason = UnavailableReason.too_close
            elif e.code in _NO_ROUTE:
                reason = UnavailableReason.no_route
            else:
                logger.warning("ODsay 길찾기 실패: %s", e)
                return None
        except (SourceUnavailable, SourceAuthError, QuotaExceeded) as e:
            logger.warning("ODsay 길찾기 실패: %s", e)
            return None
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            logger.warning("ODsay 길찾기 응답을 읽지 못했습니다: %s", type(e).__name__)
            return None
        entry = _Entry(self._monotonic() + CACHE_SECONDS, self._clock(), routes, reason)
        self._cache[key] = entry
        while len(self._cache) > CACHE_MAX_ENTRIES:
            self._cache.popitem(last=False)
        return entry

    async def _with_path(self, entry: _Entry, i: int) -> Route:
        """i번째 경로에 구간 좌표를 붙인다. 실패하면 path=null로 두고 다음 요청에서 다시 시도한다."""
        if i in entry.with_path:
            return entry.with_path[i]
        route = entry.routes[i]
        if not route.map_obj or self._source is None:
            entry.with_path[i] = route
            return route
        try:
            done = attach_paths(route, await self._source.lanes(route.map_obj))
        except (OdsayError, SourceUnavailable, SourceAuthError, QuotaExceeded) as e:
            logger.warning("ODsay 노선 그래픽 실패: %s", e)
            return route
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            logger.warning("ODsay 노선 그래픽 응답을 읽지 못했습니다: %s", type(e).__name__)
            return route
        entry.with_path[i] = done
        return done

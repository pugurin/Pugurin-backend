"""ODsay LAB 대중교통 길찾기(`searchPubTransPathT`)와 노선 그래픽(`loadLane`).

- 좌표는 WGS84이고, ODsay는 X=경도·Y=위도로 받는다.
- 키(`ODSAY_API_KEY`, Server 플랫폼 키, 허용 IP 등록)는 요청 파라미터로만 쓰고 예외 메시지·로그에 넣지 않는다.
- 에러 응답 모양은 문서에 없어서 `{"error": {...}}`와 `{"error": [{...}]}` 둘 다 받는다.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace

import httpx

from app.adapters.json_http import JsonApiClient

BASE_URL = "https://api.odsay.com/v1/api"

# trafficType: 1 지하철, 2 버스, 3 도보. 4 열차·5 고속버스·6 시외버스·7 항공 등은 etc
_MODES = {1: "subway", 2: "bus", 3: "walk"}
# loadLane의 class: 1 버스, 2 지하철
_LANE_CLASS = {"bus": 1, "subway": 2}

# 부산권 도시철도 공식 노선색. 이름에 이 글자가 들어 있으면 그 색을 쓴다(모르면 null)
_SUBWAY_COLORS = (
    ("부산 1호선", "#F06A00"),
    ("부산 2호선", "#81BF48"),
    ("부산 3호선", "#BB8C00"),
    ("부산 4호선", "#217DCB"),
    ("동해", "#0054A6"),
    ("김해", "#8652A1"),
)


class OdsayError(Exception):
    """ODsay가 돌려준 에러 코드(-98 700m 이내, -99 결과 없음, 3~6 정류장 없음·서비스 지역 아님, -8·-9·500 등)."""

    def __init__(self, code: str, message: str):
        super().__init__(f"ODsay 오류 {code}: {message}")
        self.code = code


@dataclass(frozen=True)
class Line:
    name: str
    color: str | None


@dataclass(frozen=True)
class Leg:
    mode: str  # subway·bus·walk·etc
    minutes: int
    distance_m: int
    line: Line | None
    start_name: str | None
    end_name: str | None
    stop_count: int | None
    path: list[list[float]] | None = None  # [[lng, lat], ...]


@dataclass(frozen=True)
class Route:
    total_minutes: int
    transfer_count: int
    fare: int | None
    walk_minutes: int
    distance_m: int
    legs: list[Leg]
    map_obj: str | None = field(default=None, compare=False)


def _error(data: dict) -> OdsayError | None:
    err = data.get("error")
    if isinstance(err, list):
        err = err[0] if err else None
    if not isinstance(err, dict):
        return None
    code = str(err.get("code", "")).strip()
    return OdsayError(code, str(err.get("msg") or err.get("message") or ""))


def _int(v) -> int | None:
    try:
        return round(float(v))
    except (TypeError, ValueError):
        return None


def _text(v) -> str | None:
    s = str(v).strip() if v is not None else ""
    return s or None


def line_color(name: str) -> str | None:
    return next((color for key, color in _SUBWAY_COLORS if key in name), None)


def _line(mode: str, lanes) -> Line | None:
    if isinstance(lanes, dict):  # 문서 예시는 lane을 배열이 아니라 객체로 보여준다
        lanes = [lanes]
    if mode not in ("subway", "bus") or not lanes:
        return None
    first = lanes[0]
    name = _text(first.get("name")) if mode == "subway" else _text(first.get("busNo"))
    if name is None:
        return None
    return Line(name, line_color(name) if mode == "subway" else None)


def _leg(sub: dict) -> Leg:
    mode = _MODES.get(_int(sub.get("trafficType")), "etc")
    walk = mode == "walk"
    return Leg(
        mode=mode,
        minutes=_int(sub.get("sectionTime")) or 0,
        distance_m=_int(sub.get("distance")) or 0,
        line=_line(mode, sub.get("lane")),
        start_name=None if walk else _text(sub.get("startName")),
        end_name=None if walk else _text(sub.get("endName")),
        stop_count=None if walk else _int(sub.get("stationCount")),
    )


def _name_walks(legs: list[Leg]) -> list[Leg]:
    """걷기 구간 이름은 앞뒤 대중교통 정류장 이름. 처음·끝은 null."""
    named = []
    for i, leg in enumerate(legs):
        if leg.mode == "walk":
            before = legs[i - 1].end_name if i > 0 else None
            after = legs[i + 1].start_name if i + 1 < len(legs) else None
            leg = replace(leg, start_name=before, end_name=after)
        named.append(leg)
    return named


def _route(path: dict) -> Route | None:
    info = path.get("info") or {}
    total = _int(info.get("totalTime"))
    if total is None:
        return None
    legs = _name_walks([_leg(s) for s in path.get("subPath") or [] if isinstance(s, dict)])
    bus, subway = _int(info.get("busTransitCount")), _int(info.get("subwayTransitCount"))
    if bus is None and subway is None:  # 도시간 경로는 transitCount 하나만 준다
        rides = _int(info.get("transitCount")) or 0
    else:
        rides = (bus or 0) + (subway or 0)
    fare = _int(info.get("payment"))
    return Route(
        total_minutes=total,
        transfer_count=max(rides - 1, 0),
        fare=fare if fare is not None else _int(info.get("totalPayment")),
        walk_minutes=sum(leg.minutes for leg in legs if leg.mode == "walk"),
        distance_m=_int(info.get("totalDistance")) or sum(leg.distance_m for leg in legs),
        legs=legs,
        map_obj=_text(info.get("mapObj")),
    )


def parse_routes(data: dict) -> list[Route]:
    """searchPubTransPathT 응답 → 소요시간 짧은 순 경로. 에러 응답이면 OdsayError."""
    if (err := _error(data)) is not None:
        raise err
    paths = (data.get("result") or {}).get("path") or []
    routes = [r for p in paths if isinstance(p, dict) and (r := _route(p)) is not None]
    return sorted(routes, key=lambda r: (r.total_minutes, r.transfer_count, r.walk_minutes))


def parse_lanes(data: dict) -> list[tuple[int | None, list[list[float]]]]:
    """loadLane 응답 → (class, [[lng, lat], ...]) 목록. 한 노선의 여러 section은 이어 붙인다."""
    if (err := _error(data)) is not None:
        raise err
    lanes = []
    for lane in (data.get("result") or {}).get("lane") or []:
        coords = [
            [float(p["x"]), float(p["y"])]
            for section in lane.get("section") or []
            for p in section.get("graphPos") or []
            if p.get("x") is not None and p.get("y") is not None
        ]
        lanes.append((_int(lane.get("class")), coords))
    return lanes


def attach_paths(route: Route, lanes: list[tuple[int | None, list[list[float]]]]) -> Route:
    """노선 그래픽을 대중교통 구간에 순서대로 붙인다(같은 종류 class끼리). 짝이 안 맞는 구간은 path=null."""
    queue = list(lanes)
    legs = []
    for leg in route.legs:
        wanted = _LANE_CLASS.get(leg.mode)
        if wanted is not None:
            i = next((k for k, (cls, _) in enumerate(queue) if cls == wanted), None)
            if i is not None:
                _, coords = queue.pop(i)
                leg = replace(leg, path=coords or None)
        legs.append(leg)
    return replace(route, legs=legs)


class OdsayClient(JsonApiClient):
    def __init__(
        self,
        key: str,
        *,
        base_url: str = BASE_URL,
        http: httpx.AsyncClient | None = None,
        retries: int = 1,
        backoff_seconds: float = 0.5,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        super().__init__(http or httpx.AsyncClient(timeout=10), retries, backoff_seconds, sleep)
        self._key = key
        self._base = base_url.rstrip("/")

    async def search(self, from_lng: float, from_lat: float, to_lng: float, to_lat: float) -> list[Route]:
        params = {"SX": from_lng, "SY": from_lat, "EX": to_lng, "EY": to_lat, "apiKey": self._key}
        return parse_routes(await self.get_json(f"{self._base}/searchPubTransPathT", params, "ODsay 길찾기"))

    async def lanes(self, map_obj: str) -> list[tuple[int | None, list[list[float]]]]:
        params = {"mapObject": f"0:0@{map_obj}", "apiKey": self._key}
        return parse_lanes(await self.get_json(f"{self._base}/loadLane", params, "ODsay 노선 그래픽"))

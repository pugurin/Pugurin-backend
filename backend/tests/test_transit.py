"""대중교통 길찾기: ODsay 응답 해석, 캐시·이유 처리, API. 실제 ODsay는 부르지 않는다(가짜 응답)."""

import asyncio
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from app.adapters.odsay import OdsayClient, OdsayError, attach_paths, line_color, parse_lanes, parse_routes
from app.repositories.ports import SourceUnavailable
from app.services.transit_service import CACHE_SECONDS, TransitService, UnavailableReason
from tests.conftest import API, FIXED_NOW
from tests.fakes.app import create_app

HOME = (35.16310, 129.16360)  # 해운대 (lat, lng)
WORK = (35.15460, 129.05990)  # 서면


def walk(minutes: int, distance: float) -> dict:
    return {"trafficType": 3, "sectionTime": minutes, "distance": distance}


def subway_path() -> dict:
    """해운대 → 2호선 → 서면. 38분, 환승 0회."""
    return {
        "pathType": 1,
        "info": {"totalTime": 38, "payment": 1550, "busTransitCount": 0, "subwayTransitCount": 1,
                 "totalDistance": 12840, "mapObj": "72:2:1:15"},
        "subPath": [
            walk(5, 350),
            {"trafficType": 1, "sectionTime": 24, "distance": 11900, "stationCount": 15,
             "startName": "해운대", "endName": "서면", "lane": [{"name": "부산 2호선", "subwayCode": 72}]},
            walk(4, 290.4),
        ],
    }  # fmt: skip


def bus_subway_path() -> dict:
    """버스 → 지하철 환승. 45분, 환승 1회. lane이 객체로 온다(문서 예시 모양)."""
    return {
        "pathType": 3,
        "info": {"totalTime": 45, "payment": 1550, "busTransitCount": 1, "subwayTransitCount": 1,
                 "totalDistance": 13500, "mapObj": "1001:1:3:9@72:2:5:15"},
        "subPath": [
            walk(3, 200),
            {"trafficType": 2, "sectionTime": 12, "distance": 3000, "stationCount": 6,
             "startName": "해운대해수욕장", "endName": "센텀시티역", "lane": {"busNo": "1003", "type": 11}},
            walk(2, 100),
            {"trafficType": 1, "sectionTime": 20, "distance": 9800, "stationCount": 10,
             "startName": "센텀시티", "endName": "서면", "lane": [{"name": "부산 2호선"}]},
            walk(8, 400),
        ],
    }  # fmt: skip


def search_response(*paths: dict) -> dict:
    return {"result": {"searchType": 0, "path": list(paths)}}


def lane_response(*lanes: tuple[int, list[tuple[float, float]]]) -> dict:
    return {
        "result": {
            "lane": [
                {"class": cls, "type": 1, "section": [{"graphPos": [{"x": x, "y": y} for x, y in pts]}]}
                for cls, pts in lanes
            ]
        }
    }


# ---- 응답 해석 ----


def test_parse_routes_maps_fields_and_sorts_by_time():
    routes = parse_routes(search_response(bus_subway_path(), subway_path()))
    assert [r.total_minutes for r in routes] == [38, 45]
    fast = routes[0]
    assert (fast.transfer_count, fast.fare, fast.walk_minutes, fast.distance_m) == (0, 1550, 9, 12840)
    walk_in, ride, walk_out = fast.legs
    assert (walk_in.mode, walk_in.minutes, walk_in.distance_m, walk_in.line) == ("walk", 5, 350, None)
    # 걷기 구간 이름은 앞뒤 정류장. 처음·끝은 null
    assert (walk_in.start_name, walk_in.end_name) == (None, "해운대")
    assert (walk_out.start_name, walk_out.end_name, walk_out.distance_m) == ("서면", None, 290)
    assert (ride.mode, ride.minutes, ride.stop_count, ride.start_name, ride.end_name) == (
        "subway", 24, 15, "해운대", "서면",
    )  # fmt: skip
    assert (ride.line.name, ride.line.color) == ("부산 2호선", "#81BF48")
    assert walk_in.stop_count is None and walk_in.path is None


def test_transfers_bus_line_and_middle_walk_names():
    [route] = parse_routes(search_response(bus_subway_path()))
    assert route.transfer_count == 1
    bus = route.legs[1]
    assert (bus.mode, bus.line.name, bus.line.color) == ("bus", "1003", None)  # 버스 색은 모르면 null
    assert (route.legs[2].start_name, route.legs[2].end_name) == ("센텀시티역", "센텀시티")
    assert route.walk_minutes == 3 + 2 + 8


def test_other_traffic_types_are_etc_and_missing_fare_is_null():
    path = {"info": {"totalTime": 120, "transitCount": 1, "totalDistance": 90000},
            "subPath": [{"trafficType": 6, "sectionTime": 120, "distance": 90000, "startName": "부산종합버스터미널",
                         "endName": "울산", "stationCount": 1}]}  # fmt: skip
    [route] = parse_routes(search_response(path))
    assert route.legs[0].mode == "etc" and route.legs[0].line is None
    assert (route.fare, route.transfer_count) == (None, 0)


@pytest.mark.parametrize(
    "body",
    [
        {"error": {"code": "-98", "msg": "출, 도착지가 700m이내입니다."}},
        {"error": [{"code": -98, "message": "출, 도착지가 700m이내입니다."}]},
    ],
)
def test_error_responses_in_either_shape(body):
    with pytest.raises(OdsayError) as e:
        parse_routes(body)
    assert e.value.code == "-98"


def test_line_colors_for_busan_lines():
    assert line_color("부산 1호선") == "#F06A00"
    assert line_color("부산 4호선") == "#217DCB"
    assert line_color("동해선") == "#0054A6"
    assert line_color("부산김해경전철") == "#8652A1"
    assert line_color("서울 2호선") is None


def test_attach_paths_matches_lanes_by_class_in_order():
    [route] = parse_routes(search_response(bus_subway_path()))
    lanes = parse_lanes(lane_response((1, [(129.16, 35.16), (129.13, 35.17)]), (2, [(129.13, 35.17), (129.06, 35.16)])))
    done = attach_paths(route, lanes)
    assert done.legs[1].path == [[129.16, 35.16], [129.13, 35.17]]  # 버스
    assert done.legs[3].path == [[129.13, 35.17], [129.06, 35.16]]  # 지하철
    assert all(leg.path is None for leg in done.legs if leg.mode == "walk")
    # 짝이 없으면 null
    only_bus = attach_paths(route, parse_lanes(lane_response((1, [(129.16, 35.16)]))))
    assert only_bus.legs[3].path is None


def test_client_sends_lng_as_x_and_keeps_key_out_of_errors():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("searchPubTransPathT"):
            return httpx.Response(200, json=search_response(subway_path()))
        return httpx.Response(503)

    client = OdsayClient("SECRET-KEY", http=httpx.AsyncClient(transport=httpx.MockTransport(handler)), retries=0)

    async def run():
        routes = await client.search(HOME[1], HOME[0], WORK[1], WORK[0])
        with pytest.raises(SourceUnavailable) as e:
            await client.lanes("72:2:1:15")
        return routes, str(e.value)

    routes, message = asyncio.run(run())
    assert routes[0].total_minutes == 38
    q = seen[0].url.params
    assert (q["SX"], q["SY"], q["EX"], q["EY"]) == (str(HOME[1]), str(HOME[0]), str(WORK[1]), str(WORK[0]))
    assert seen[1].url.params["mapObject"] == "0:0@72:2:1:15"
    assert "SECRET-KEY" not in message


# ---- 서비스(캐시·이유) ----


class FakeOdsay:
    def __init__(self, search=None, lanes=None, error: Exception | None = None):
        self.search_body = search if search is not None else search_response(subway_path(), bus_subway_path())
        self.lanes_body = lanes if lanes is not None else lane_response((2, [(129.15, 35.16), (129.06, 35.16)]))
        self.error = error
        self.searches = 0
        self.lane_calls: list[str] = []

    async def search(self, from_lng, from_lat, to_lng, to_lat):
        self.searches += 1
        if self.error:
            raise self.error
        return parse_routes(self.search_body)

    async def lanes(self, map_obj):
        self.lane_calls.append(map_obj)
        return parse_lanes(self.lanes_body)


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def service(source, clock=None) -> TransitService:
    return TransitService(source, lambda: FIXED_NOW, monotonic=clock or Clock())


def routes(svc, *, home=HOME, work=WORK, limit=3):
    return asyncio.run(svc.routes(home[0], home[1], work[0], work[1], limit))


def test_caches_same_trip_for_24_hours_by_5_decimals():
    fake, clock = FakeOdsay(), Clock()
    svc = service(fake, clock)
    first = routes(svc, limit=1)
    assert [r.total_minutes for r in first.routes] == [38]
    assert first.routes[0].legs[1].path == [[129.15, 35.16], [129.06, 35.16]]
    assert first.unavailable_reason is None and first.data_as_of == FIXED_NOW
    routes(svc, home=(HOME[0] + 0.000001, HOME[1]), limit=1)  # 소수 5자리가 같으면 같은 출발
    assert (fake.searches, fake.lane_calls) == (1, ["72:2:1:15"])
    # 더 많이 달라고 하면 모자란 경로의 노선 그래픽만 더 부른다
    assert len(routes(svc, limit=5).routes) == 2
    assert (fake.searches, len(fake.lane_calls)) == (1, 2)
    clock.t = CACHE_SECONDS + 1
    routes(svc)
    assert fake.searches == 2


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (OdsayError("-98", "700m"), UnavailableReason.too_close),
        (OdsayError("-99", "없음"), UnavailableReason.no_route),
        (OdsayError("6", "서비스 지역 아님"), UnavailableReason.no_route),
        (OdsayError("500", "서버 오류"), UnavailableReason.source_unavailable),
        (OdsayError("-8", "입력 오류"), UnavailableReason.source_unavailable),
        (SourceUnavailable("ODsay 호출 실패"), UnavailableReason.source_unavailable),
    ],
)
def test_reasons(error, reason):
    fake = FakeOdsay(error=error)
    svc = service(fake)
    result = routes(svc)
    assert (result.routes, result.unavailable_reason) == ([], reason)
    routes(svc)
    # 장애는 캐시하지 않고 다시 부른다. 700m·경로 없음은 캐시한다
    assert fake.searches == (2 if reason is UnavailableReason.source_unavailable else 1)


def test_empty_result_is_no_route():
    result = routes(service(FakeOdsay(search=search_response())))
    assert result.unavailable_reason is UnavailableReason.no_route


def test_outside_korea_is_no_route_without_calling():
    fake = FakeOdsay()
    result = routes(service(fake), work=(35.68, 139.76))  # 도쿄
    assert result.unavailable_reason is UnavailableReason.no_route and fake.searches == 0


def test_without_key_is_source_unavailable():
    assert routes(service(None)).unavailable_reason is UnavailableReason.source_unavailable


def test_lane_failure_keeps_route_without_path():
    class NoLanes(FakeOdsay):
        async def lanes(self, map_obj):
            self.lane_calls.append(map_obj)
            raise SourceUnavailable("노선 그래픽 실패")

    fake = NoLanes()
    svc = service(fake)
    result = routes(svc, limit=1)
    assert result.routes[0].total_minutes == 38 and result.routes[0].legs[1].path is None
    routes(svc, limit=1)
    assert len(fake.lane_calls) == 2  # 실패한 그래픽은 다음 요청에서 다시 시도


# ---- API ----


def client(source) -> TestClient:
    return TestClient(create_app(clock=lambda: FIXED_NOW, transit_source=source), headers={"X-Device-Id": str(uuid4())})


PARAMS = {"from_lat": HOME[0], "from_lng": HOME[1], "to_lat": WORK[0], "to_lng": WORK[1]}


def test_api_returns_routes_in_spec_shape():
    with client(FakeOdsay()) as c:
        r = c.get(f"{API}/transit/routes", params={**PARAMS, "limit": 1})
    assert r.status_code == 200
    body = r.json()
    assert body["data"]["unavailable_reason"] is None
    [route] = body["data"]["routes"]
    assert {k: route[k] for k in ("total_minutes", "transfer_count", "fare", "walk_minutes", "distance_m")} == {
        "total_minutes": 38, "transfer_count": 0, "fare": 1550, "walk_minutes": 9, "distance_m": 12840,
    }  # fmt: skip
    assert route["legs"][0] == {
        "mode": "walk", "minutes": 5, "distance_m": 350, "line": None,
        "start_name": None, "end_name": "해운대", "stop_count": None, "path": None,
    }  # fmt: skip
    assert route["legs"][1]["line"] == {"name": "부산 2호선", "color": "#81BF48"}
    assert route["legs"][1]["path"] == [[129.15, 35.16], [129.06, 35.16]]
    assert body["meta"]["data_as_of"].startswith("2026-")


def test_api_default_limit_is_3_and_unavailable_is_200():
    with client(FakeOdsay(error=OdsayError("-98", "700m"))) as c:
        r = c.get(f"{API}/transit/routes", params=PARAMS)
    assert r.status_code == 200
    assert r.json()["data"] == {"routes": [], "unavailable_reason": "too_close"}


@pytest.mark.parametrize(
    "bad",
    [{"from_lat": "abc"}, {"to_lat": 91}, {"limit": 6}, {"limit": 0}],
)
def test_api_rejects_bad_parameters(bad):
    with client(FakeOdsay()) as c:
        r = c.get(f"{API}/transit/routes", params={**PARAMS, **bad})
    assert (r.status_code, r.json()["error"]["code"]) == (400, "VALIDATION_ERROR")


def test_api_without_key_says_source_unavailable():
    with client(None) as c:
        r = c.get(f"{API}/transit/routes", params=PARAMS)
    assert (r.status_code, r.json()["data"]["unavailable_reason"]) == (200, "source_unavailable")

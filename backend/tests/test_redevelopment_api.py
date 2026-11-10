"""정비사업 구역 조회 API (실제 PostGIS). 데이터는 가짜 정비사업 정보 응답으로 동기화해 채운다."""

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.db import migrate
from app.db.engine import make_engine
from app.ingestion.geocode import GeocodeCache
from app.ingestion.redevelopment import sync_redevelopments
from tests.conftest import API, FIXED_NOW, create_database
from tests.fakes.app import create_app
from tests.fakes.madangs import FakeLookup
from tests.fakes.redevelopment import FakeRedevelopmentSource, zone_row

# FakeLookup은 지번 끝 번호 n을 (35.16 + n/100000, 129.16 + n/100000)에 둔다
AROUND = "129.15,35.15,129.17,35.17"
OFFICE = "부산광역시 부산진구 가상로 266, 2층(가야동)"


def zone_rows() -> list[dict]:
    return [
        zone_row(1),  # 재개발 · 조합설립인가(4) · 가야동 101 → 화면 안
        zone_row(2, areaName="가상2아파트 재건축", step="착공", location="부산진구 가야동 202"),  # 8, 화면 안
        zone_row(3, areaName="가상3 가로주택정비사업", step="준공", location=OFFICE),  # 9, 좌표 없음
        zone_row(4, areaName="가상4맨션 소규모재건축", step="안전진단", location=""),  # 모르는 단계
        zone_row(5, step="이전고시", location="해운대구 우동 5000"),  # 10, 화면 밖
        zone_row(7, aCode="", areaName="가상7구역 재개발"),  # aCode 없음, 조합설립인가(4)
    ]


async def _populate(url: str, cache: GeocodeCache) -> None:
    engine = make_engine(url)
    first = [*zone_rows(), zone_row(6, areaName="가상6구역 재개발", step="관리처분계획")]
    for minutes, rows in ((0, first), (60, zone_rows())):  # 6번은 두 번째 수집에서 빠져 닫힌다
        await sync_redevelopments(
            engine, FakeRedevelopmentSource(rows), FakeLookup(), cache,
            now=FIXED_NOW + timedelta(minutes=minutes), out=lambda _: None,
        )  # fmt: skip
    await engine.dispose()


@pytest.fixture(scope="module")
def redevelopment_api_db(postgis: str, tmp_path_factory) -> str:
    url = create_database(postgis, "redevelopment_api")
    migrate.upgrade(url)
    asyncio.run(_populate(url, GeocodeCache(tmp_path_factory.mktemp("redevelopment_geo") / "geocode.json")))
    return url


def client(url: str | None) -> TestClient:
    settings = Settings(database_url=url) if url else Settings()
    return TestClient(create_app(settings=settings, clock=lambda: FIXED_NOW), headers={"X-Device-Id": str(uuid4())})


def names(body: dict) -> list[str]:
    return [z["area_name"] for z in body["data"]]


def test_list_shows_open_zones_furthest_stage_first(redevelopment_api_db):
    with client(redevelopment_api_db) as c:
        r = c.get(f"{API}/redevelopments")
    assert r.status_code == 200
    body = r.json()
    assert names(body) == [
        "가상5구역 재개발",  # 이전고시 10
        "가상3 가로주택정비사업",  # 준공 9
        "가상2아파트 재건축",  # 착공 8
        "가상1구역 재개발",  # 조합설립 4
        "가상7구역 재개발",  # 조합설립 4
        "가상4맨션 소규모재건축",  # 모르는 단계는 맨 뒤
    ]
    meta = body["meta"]
    assert meta["source_attribution"] == "부산광역시 정비사업 정보(공공데이터포털)"
    assert (meta["page"], meta["page_size"], meta["total"], meta["total_pages"]) == (1, 100, 6, 1)
    assert meta["data_as_of"] and meta["reporting_lag_notice"] is False


def test_a_zone_carries_stage_location_numbers_and_the_city_page(redevelopment_api_db):
    with client(redevelopment_api_db) as c:
        zone = c.get(f"{API}/redevelopments/BARA_0000000000001").json()["data"]
    assert zone == {
        "id": "BARA_0000000000001",
        "area_name": "가상1구역 재개발",
        "kind": "redevelopment",
        "step_raw": "조합설립인가",
        "stage": "association",
        "stage_order": 4,
        "location": "부산진구 가야동 101번지 일원(가상초교 남측 일원)",
        "location_kind": "lot",
        "lat": pytest.approx(35.16101),
        "lng": pytest.approx(129.16101),
        "location_precise": True,
        "area_m2": 45123.5,
        "area_pyeong": 13649.8,
        "households": 1234,
        "buildings": 12,
        "union_members": 800,
        "floor_area_ratio": 299.9,
        "building_coverage_ratio": 19.5,
        "scale": "지하3층지상32층",
        "business_entity": "가상1구역 재개발정비사업조합",
        "contractor": "가상건설",
        "engineer": None,
        "architect": None,
        "specialists": "가상정비",
        "tel": "051-000-0000",
        "images": {
            "view": "https://dynamice.busan.go.kr/data/busi_ara/BARA_20260001_view.jpg",
            "location": "https://dynamice.busan.go.kr/data/busi_ara/BARA_20260001_loct.jpg",
            "panorama": None,
            "layout": None,
        },
        "source_url": "https://dynamice.busan.go.kr/home/BARA_0000000000001/main.do",
        "is_closed": False,
    }


def test_office_addresses_and_missing_codes_have_no_point_or_page(redevelopment_api_db):
    with client(redevelopment_api_db) as c:
        data = {z["area_name"]: z for z in c.get(f"{API}/redevelopments").json()["data"]}
    office = data["가상3 가로주택정비사업"]
    assert (office["location_kind"], office["lat"], office["location_precise"]) == ("office", None, False)
    no_code = data["가상7구역 재개발"]
    assert no_code["id"].startswith("NOCODE_") and no_code["source_url"] is None
    unknown = data["가상4맨션 소규모재건축"]
    assert (unknown["stage"], unknown["stage_order"], unknown["step_raw"]) == ("unknown", None, "안전진단")


def test_filters_by_bbox_kind_and_stage(redevelopment_api_db):
    with client(redevelopment_api_db) as c:
        inside = c.get(f"{API}/redevelopments", params={"bbox": AROUND}).json()
        kinds = c.get(f"{API}/redevelopments", params={"kind": "reconstruction,street_housing"}).json()
        stages = c.get(f"{API}/redevelopments", params={"stage": "association,unknown"}).json()
        both = c.get(f"{API}/redevelopments", params={"kind": "redevelopment", "stage": "transferred"}).json()
    assert names(inside) == ["가상2아파트 재건축", "가상1구역 재개발", "가상7구역 재개발"]
    assert names(kinds) == ["가상3 가로주택정비사업", "가상2아파트 재건축"]
    assert names(stages) == ["가상1구역 재개발", "가상7구역 재개발", "가상4맨션 소규모재건축"]
    assert names(both) == ["가상5구역 재개발"]


def test_closed_zones_appear_only_when_asked(redevelopment_api_db):
    with client(redevelopment_api_db) as c:
        everything = c.get(f"{API}/redevelopments", params={"include_closed": "true"}).json()
        closed = c.get(f"{API}/redevelopments/BARA_0000000000006")
    assert everything["meta"]["total"] == 7
    assert {z["area_name"]: z["is_closed"] for z in everything["data"]}["가상6구역 재개발"] is True
    assert closed.status_code == 200 and closed.json()["data"]["is_closed"] is True


def test_pages(redevelopment_api_db):
    with client(redevelopment_api_db) as c:
        second = c.get(f"{API}/redevelopments", params={"page": 2, "page_size": 4}).json()
        too_big = c.get(f"{API}/redevelopments", params={"page_size": 501})
    assert names(second) == ["가상7구역 재개발", "가상4맨션 소규모재건축"]
    assert (second["meta"]["total"], second["meta"]["total_pages"]) == (6, 2)
    assert too_big.status_code == 400


@pytest.mark.parametrize(("param", "value"), [("kind", "castle"), ("stage", "finished"), ("bbox", "1,2,3")])
def test_bad_filters_are_400_with_the_field(redevelopment_api_db, param, value):
    with client(redevelopment_api_db) as c:
        r = c.get(f"{API}/redevelopments", params={param: value})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "VALIDATION_ERROR" and r.json()["error"]["field"] == param


def test_unknown_id_is_404(redevelopment_api_db):
    with client(redevelopment_api_db) as c:
        r = c.get(f"{API}/redevelopments/BARA_9999999999999")
    assert r.status_code == 404 and r.json()["error"]["code"] == "RESOURCE_NOT_FOUND"


def test_before_the_first_sync_it_says_there_is_no_data(postgis):
    url = create_database(postgis, "redevelopment_empty")
    migrate.upgrade(url)
    with client(url) as c:
        r = c.get(f"{API}/redevelopments")
        one = c.get(f"{API}/redevelopments/BARA_0000000000001")
    assert r.status_code == 503 and "정비사업" in r.json()["error"]["message"]
    assert one.status_code == 503 and one.json()["error"]["code"] == "DATA_NOT_READY"


def test_without_a_database_it_says_there_is_no_data():
    with client(None) as c:
        r = c.get(f"{API}/redevelopments")
    assert r.status_code == 503 and r.json()["error"]["code"] == "DATA_NOT_READY"

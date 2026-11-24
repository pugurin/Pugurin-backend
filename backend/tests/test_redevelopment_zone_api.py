"""정비구역 경계 적재·잇기와 조회 API (실제 PostGIS).

우리 구역은 가짜 정비사업 정보 응답으로 동기화하고, 경계는 가짜 브이월드 SHP로 싣는다.
FakeLookup은 지번 끝 번호 n을 (35.16 + n/100000, 129.16 + n/100000)에 둔다. 가상1구역(가야동 101)은 ORIGIN 바로 옆이다.
"""

import asyncio
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.config import Settings
from app.db import migrate
from app.db.engine import make_engine
from app.ingestion.geocode import GeocodeCache
from app.ingestion.redevelopment import sync_redevelopments
from app.ingestion.redevelopment_zones import load_redevelopment_zones
from tests.conftest import API, FIXED_NOW, create_database
from tests.fakes.app import create_app
from tests.fakes.madangs import FakeLookup
from tests.fakes.redevelopment import FakeRedevelopmentSource, zone_row
from tests.fakes.redevelopment_zones import OFFICE_5186, PROMOTION_5186, FakeZone, write_zone_files
from tests.fakes.schoolzone import FAR_5186, NORTH_5186, ORIGIN_5186, square

OFFICE = "부산광역시 부산진구 가상로 266, 2층(가야동)"
AROUND = "129.15,35.15,129.17,35.18"
NORTH = "129.155,35.165,129.165,35.175"


def ours() -> list[dict]:
    return [
        zone_row(1),  # 가상1구역 재개발 · 가야동 101(지번 좌표) → 경계 A 안
        zone_row(2, areaName="가상2구역 재건축", location=OFFICE),  # 좌표 없음 → 이름으로 경계 B
        zone_row(3, areaName="가상3 가로주택정비사업", location=OFFICE),  # 가로주택은 잇지 않는다
        # 조합 사무실이 이름 없는 경계 H 안에 있고, 사무실 동네(가상)가 구역 이름과 맞는다 → 사무실로 잇는다
        zone_row(9, areaName="가상9구역 재개발", location="부산광역시 해운대구 가상로 2500, 2층(가상동)"),
        # 재정비촉진 구역: 경계 이름이 "서,금사 재정비촉진(A)구역-국토…"로 적혀 있어도 이름으로 잇는다
        zone_row(10, areaName="서금사재정비촉진A구역 재개발", location=OFFICE),
    ]


def boundaries() -> list[FakeZone]:
    x, y = ORIGIN_5186
    return [
        FakeZone("A", rings=[square(ORIGIN_5186, 300)]),  # 이름 없음
        FakeZone("B", name="가상2주택재건축정비구역", designated="20210315", rings=[square(NORTH_5186, 200)]),
        FakeZone("C", name="가상3가로주택", rings=[square(FAR_5186, 10)]),  # 400㎡ → 너무 작다
        FakeZone("D", name="가상동 일원", rings=[square((x + 100, y + 100), 50)]),  # A 안에 통째로 든 조각
        FakeZone("E", name="정비기반시설(도로)", rings=[square(ORIGIN_5186, 20)]),  # 시설
        FakeZone("F", name="효제1구역", sigungu="11110", rings=[square(FAR_5186, 300)]),  # 서울
        FakeZone("G", name="부산광역시 고시 제2016-415호", rings=[square(FAR_5186, 200)]),  # 이을 구역 없음
        FakeZone("H", rings=[square(OFFICE_5186, 150)]),  # 이름 없음, 가상9구역 사무실이 안에 있다
    ]


def promotion() -> list[FakeZone]:
    law = "-국토의계획및이용에관한법률제56조규정에 의한 개발행위허가제한구역"
    return [
        FakeZone("P1", name=f"서,금사 재정비촉진(A)구역{law}", rings=[square(PROMOTION_5186, 150)]),
        FakeZone("P2", name="2구역:존치관리구역", rings=[square(PROMOTION_5186, 400)]),  # 존치 구역은 뺀다
        FakeZone("P3", name="교육9", rings=[square(OFFICE_5186, 400)]),  # 지구 안 시설
        FakeZone("P4", note="영도제1재정비촉진지구", rings=[square(PROMOTION_5186, 600)]),  # 지구 바깥 경계
    ]


async def _sync(url: str, cache: GeocodeCache, rows: list[dict], minutes: int = 0) -> None:
    engine = make_engine(url)
    await sync_redevelopments(
        engine, FakeRedevelopmentSource(rows), FakeLookup(), cache,
        now=FIXED_NOW + timedelta(minutes=minutes), out=lambda _: None,
    )  # fmt: skip
    await engine.dispose()


async def _load(url: str, directory):
    engine = make_engine(url)
    try:
        return await load_redevelopment_zones(engine, directory, now=FIXED_NOW, out=lambda _: None)
    finally:
        await engine.dispose()


@pytest.fixture(scope="module")
def zone_db(postgis: str, tmp_path_factory) -> tuple[str, object]:
    url = create_database(postgis, "redevelopment_zones")
    migrate.upgrade(url)
    cache = GeocodeCache(tmp_path_factory.mktemp("redev_zone_geo") / "geocode.json")
    asyncio.run(_sync(url, cache, ours()))
    files = write_zone_files(tmp_path_factory.mktemp("redev_zones"), boundaries())
    write_zone_files(files, promotion(), folder="promotion")
    report = asyncio.run(_load(url, files))
    return url, report


def client(url: str) -> TestClient:
    return TestClient(
        create_app(settings=Settings(database_url=url), clock=lambda: FIXED_NOW),
        headers={"X-Device-Id": str(uuid4())},
    )


def test_load_keeps_real_zones_and_links_by_point_then_name(zone_db):
    _, report = zone_db
    assert report.zones == 5  # A, B, G, H, P1
    assert report.layers == {"zone": 4, "promotion": 1}
    assert report.skipped == {"시설": 4, "도형 없음": 0, "1,000㎡ 미만": 1, "다른 구역 안": 1}
    assert (report.linked_by_point, report.linked_by_name, report.linked_by_office) == (1, 2, 1)


def test_promotion_and_office_links(zone_db):
    url, _ = zone_db
    with client(url) as c:
        body = c.get(f"{API}/redevelopment-zones", params={"bbox": "128.75,34.85,129.35,35.4"}).json()
    by_id = {z["zone_id"]: z for z in body["data"]}
    assert (by_id["P1"]["layer"], by_id["P1"]["redevelopment_id"]) == ("promotion", "BARA_0000000000010")
    assert by_id["P1"]["name"] == "서,금사 재정비촉진(A)구역"  # 법 조문 꼬리를 뗀다
    assert (by_id["H"]["layer"], by_id["H"]["redevelopment_id"]) == ("zone", "BARA_0000000000009")


def test_zones_in_view_carry_geometry_names_and_the_linked_zone(zone_db):
    url, _ = zone_db
    with client(url) as c:
        r = c.get(f"{API}/redevelopment-zones", params={"bbox": AROUND, "zoom": 16})
    assert r.status_code == 200, r.text
    body = r.json()
    by_id = {z["zone_id"]: z for z in body["data"]}
    assert set(by_id) == {"A", "B"}
    a, b = by_id["A"], by_id["B"]
    assert a["name"] is None and a["redevelopment_id"] == "BARA_0000000000001"
    assert (a["area_name"], a["kind"], a["stage"]) == ("가상1구역 재개발", "redevelopment", "association")
    assert b["name"] == "가상2주택재건축정비구역" and b["redevelopment_id"] == "BARA_0000000000002"
    assert b["designated_on"] == "2021-03-15" and b["notice"] is None
    assert a["geometry"]["type"] == "MultiPolygon"
    ring = a["geometry"]["coordinates"][0][0]
    assert all(129.15 < lng < 129.17 and 35.15 < lat < 35.17 for lng, lat in ring)
    assert body["meta"]["source_attribution"] == "브이월드 용도지역지구정보"
    assert body["meta"]["data_as_of"]


def test_an_unlinked_zone_keeps_its_notice(zone_db):
    url, _ = zone_db
    with client(url) as c:
        body = c.get(f"{API}/redevelopment-zones", params={"bbox": "128.75,34.85,129.35,35.4"}).json()
    g = next(z for z in body["data"] if z["zone_id"] == "G")
    assert (g["name"], g["notice"], g["redevelopment_id"], g["area_name"]) == (
        None,
        "부산광역시 고시 제2016-415호",
        None,
        None,
    )


def test_zones_need_a_bbox_and_answer_empty_outside(zone_db):
    url, _ = zone_db
    with client(url) as c:
        assert c.get(f"{API}/redevelopment-zones").status_code == 400
        r = c.get(f"{API}/redevelopment-zones", params={"bbox": "126.9,37.5,127.0,37.6"})
    assert r.status_code == 200 and r.json()["data"] == []


def test_zones_without_coordinates_get_the_boundary_point(zone_db):
    url, _ = zone_db
    with client(url) as c:
        data = {z["id"]: z for z in c.get(f"{API}/redevelopments").json()["data"]}
        north = [z["id"] for z in c.get(f"{API}/redevelopments", params={"bbox": NORTH}).json()["data"]]
    one, two, three = (data[f"BARA_000000000000{n}"] for n in (1, 2, 3))
    assert (one["position_source"], one["zone_id"]) == ("lot", "A")
    assert (two["position_source"], two["zone_id"], two["location_precise"]) == ("zone", "B", False)
    assert 35.165 < two["lat"] < 35.175 and 129.155 < two["lng"] < 129.165
    assert (three["position_source"], three["zone_id"], three["lat"]) == ("none", None, None)
    assert north == ["BARA_0000000000002"]  # 경계 대표점으로 화면 안에 든다


def test_zones_are_not_ready_before_the_boundary_file_is_loaded(postgis, tmp_path):
    url = create_database(postgis, "redevelopment_zones_empty")
    migrate.upgrade(url)
    asyncio.run(_sync(url, GeocodeCache(tmp_path / "geocode.json"), ours()))
    with client(url) as c:
        r = c.get(f"{API}/redevelopment-zones", params={"bbox": AROUND})
    assert r.status_code == 503 and r.json()["error"]["code"] == "DATA_NOT_READY"


def test_the_daily_sync_relinks_zones(postgis, tmp_path):
    url = create_database(postgis, "redevelopment_zones_relink")
    migrate.upgrade(url)
    cache = GeocodeCache(tmp_path / "geocode.json")
    asyncio.run(_sync(url, cache, ours()))
    asyncio.run(_load(url, write_zone_files(tmp_path / "files", boundaries())))
    asyncio.run(_sync(url, cache, ours()[:1], minutes=60))  # 가상2구역이 원천에서 빠져 닫힌다

    async def links():
        engine = make_engine(url)
        async with engine.connect() as conn:
            rows = (await conn.execute(text("SELECT zone_id, redevelopment_id FROM redevelopment_zones"))).all()
        await engine.dispose()
        return dict(rows)

    assert asyncio.run(links()) == {"A": "BARA_0000000000001", "B": None, "G": None, "H": None}


def test_a_load_that_keeps_nothing_answers_an_empty_list(postgis, tmp_path):
    url = create_database(postgis, "redevelopment_zones_nothing")
    migrate.upgrade(url)
    asyncio.run(_sync(url, GeocodeCache(tmp_path / "geocode.json"), ours()))
    report = asyncio.run(
        _load(url, write_zone_files(tmp_path / "files", [FakeZone("C", rings=[square(FAR_5186, 10)])]))
    )
    assert report.zones == 0  # 1,000㎡ 미만이라 모두 걸러졌다
    with client(url) as c:
        r = c.get(f"{API}/redevelopment-zones", params={"bbox": AROUND})
    assert r.status_code == 200, r.text
    assert r.json()["data"] == [] and r.json()["meta"]["data_as_of"]


def test_zones_loaded_before_the_load_record_existed_stay_ready(postgis, tmp_path):
    # 0016 전에 실은 경계에는 etl_runs 기록이 없다. 마이그레이션이 그 적재를 기록으로 채워 503으로 돌아가지 않게 한다
    url = create_database(postgis, "redevelopment_zones_backfill")
    migrate.upgrade(url, "0015")
    asyncio.run(_sync(url, GeocodeCache(tmp_path / "geocode.json"), ours()))

    async def load_without_record():
        engine = make_engine(url)
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO redevelopment_zones (zone_id, layer, sigungu_code, geom, label_point, area_m2, "
                    "base_date, loaded_at) VALUES ('OLD', 'zone', '26230', "
                    "ST_GeomFromText('MULTIPOLYGON(((129.15 35.16,129.16 35.16,129.16 35.17,129.15 35.16)))', 4326), "
                    "ST_SetSRID(ST_MakePoint(129.155, 35.165), 4326), 9000, '2026-11-09', :now)"
                ),
                {"now": FIXED_NOW},
            )
        await engine.dispose()

    asyncio.run(load_without_record())
    migrate.upgrade(url)
    with client(url) as c:
        r = c.get(f"{API}/redevelopment-zones", params={"bbox": AROUND})
    assert r.status_code == 200, r.text
    assert [z["zone_id"] for z in r.json()["data"]] == ["OLD"]
    assert datetime.fromisoformat(r.json()["meta"]["data_as_of"].replace("Z", "+00:00")) == FIXED_NOW

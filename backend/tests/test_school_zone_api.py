"""학구도(초등학교 통학구역) 적재와 조회 API (실제 PostGIS). 학교는 가짜 나이스 응답, 구역은 가짜 학구도 파일로 채운다."""

import asyncio
from datetime import date, datetime
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.db import migrate
from app.db.engine import make_engine
from app.ingestion.geocode import GeocodeCache
from app.ingestion.school_zones import NoElementarySchools, load_school_zones
from app.repositories.postgres.facilities import PostgresFacilityRepository
from app.repositories.types import (
    AcademyCategory,
    HospitalTier,
    Nearby,
    School,
    SchoolLevel,
    SchoolZoneInfo,
)
from app.schemas.facility import NearbyData, NearbyResponse
from tests.conftest import API, FIXED_NOW, create_database
from tests.fakes.schoolzone import FAR_5186, NORTH_5186, ORIGIN_5186, FakeZone, link, square, write_zone_files
from tests.test_facility_api import ORIGIN, _populate, client

NORTH = {"lat": 35.17, "lng": 129.16}
OUTSIDE = {"lat": 35.25, "lng": 129.25}

# 가상 n초등학교는 (35.16 + n/100000, 129.16 + n/100000)에 있고 ID는 72400nn이다 (tests/test_facility_api.py)
ZONES = [
    # ORIGIN을 덮는 일반 구역. 가장 가까운 가상10초가 아니라 가상40초가 배정 학교다
    FakeZone("Z000000001", "가상40초통학구역", rings=[square(ORIGIN_5186, 300)]),
    # NORTH를 덮는 공동통학구역. 가상20초(약 1,087m)·가상30초(약 1,076m) 중 가까운 가상30초
    FakeZone("Z000000002", "가상20초가상30초공동통학구역", gb="1", rings=[square(NORTH_5186, 300)]),
    # 가상30초의 다른 구역(떨어져 있어 합치면 MultiPolygon)
    FakeZone("Z000000003", "가상30초통학구역", rings=[square(FAR_5186, 200)]),
    # 부산 밖 구역은 싣지 않는다
    FakeZone("Z000000004", "서울가상초통학구역", sd_cd="11", rings=[square(ORIGIN_5186, 1000)]),
    # 우리 목록에 없는 학교의 구역
    FakeZone("Z000000005", "연학초통학구역", rings=[square(FAR_5186, 50)]),
]
LINKS = [
    link("Z000000001", "B1", "가상40초등학교"),
    link("Z000000002", "B2", "가상20초등학교"),
    link("Z000000002", "B3", "가상30초등학교"),
    link("Z000000003", "B3", "가상30초등학교"),
    link("Z000000004", "B4", "가상10초등학교"),
    link("Z000000005", "B5", "연학초등학교"),
]


async def _load(url: str, directory) -> object:
    engine = make_engine(url)
    try:
        return await load_school_zones(engine, directory, now=FIXED_NOW, out=lambda _: None)
    finally:
        await engine.dispose()


@pytest.fixture(scope="module")
def zone_db(postgis: str, tmp_path_factory) -> tuple[str, object]:
    url = create_database(postgis, "school_zone_api")
    migrate.upgrade(url)
    asyncio.run(_populate(url, GeocodeCache(tmp_path_factory.mktemp("zone_geo") / "geocode.json")))
    report = asyncio.run(_load(url, write_zone_files(tmp_path_factory.mktemp("schoolzone"), ZONES, LINKS)))
    return url, report


def test_loading_keeps_busan_zones_and_reports_unmatched_schools(zone_db):
    _, report = zone_db
    assert (report.zones, report.links) == (4, 4)
    assert report.unmatched == ["연학초등학교"] and report.ambiguous == []


def test_loading_again_replaces_everything(zone_db, tmp_path):
    url, _ = zone_db
    only_one = write_zone_files(tmp_path, ZONES[:1], LINKS[:1])
    asyncio.run(_load(url, only_one))
    try:

        async def count():
            engine = make_engine(url)
            async with engine.connect() as conn:
                zones = (await conn.execute(text("SELECT count(*) FROM school_zones"))).scalar()
                links = (await conn.execute(text("SELECT count(*) FROM school_zone_schools"))).scalar()
            await engine.dispose()
            return zones, links

        assert asyncio.run(count()) == (1, 1)
    finally:
        asyncio.run(_load(url, write_zone_files(tmp_path / "all", ZONES, LINKS)))


def test_zone_geometry_is_stored_in_wgs84(zone_db):
    url, _ = zone_db

    async def check():
        engine = make_engine(url)
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT ST_SRID(geom), GeometryType(geom), ST_Intersects(geom, ST_SetSRID(ST_MakePoint(129.16, 35.16), 4326)) "
                        "FROM school_zones WHERE zone_id = 'Z000000001'"
                    )
                )
            ).one()
        await engine.dispose()
        return tuple(row)

    assert asyncio.run(check()) == (4326, "MULTIPOLYGON", True)


def test_zone_of_a_school(zone_db):
    url, _ = zone_db
    with client(url) as c:
        r = c.get(f"{API}/schools/7240040/zone")
    assert r.status_code == 200
    body = r.json()
    assert body["data"]["school_id"] == "7240040" and body["data"]["kind"] == "통학구역"
    geometry = body["data"]["geometry"]
    assert geometry["type"] == "Polygon"
    lngs = [p[0] for p in geometry["coordinates"][0]]
    lats = [p[1] for p in geometry["coordinates"][0]]
    assert min(lngs) < 129.16 < max(lngs) and min(lats) < 35.16 < max(lats)
    assert body["meta"]["source_attribution"] == ["한국교육시설안전원 학구도(기준일 2026-09-20)"]
    assert "실제 배정과 다를 수" in body["meta"]["zone_notice"]


def test_zone_of_a_school_in_several_zones_is_their_union(zone_db):
    url, _ = zone_db
    with client(url) as c:
        body = c.get(f"{API}/schools/7240030/zone").json()
    assert body["data"]["kind"] == "공동통학구역"  # 하나라도 공동통학구역이면
    assert body["data"]["geometry"]["type"] == "MultiPolygon" and len(body["data"]["geometry"]["coordinates"]) == 2


def test_school_without_a_zone_is_not_found(zone_db):
    url, _ = zone_db
    with client(url) as c:
        nearest_but_unzoned = c.get(f"{API}/schools/7240010/zone")
        unknown = c.get(f"{API}/schools/없는학교/zone")
    assert (
        nearest_but_unzoned.status_code == 404 and nearest_but_unzoned.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
    )
    assert unknown.status_code == 404


def test_nearby_includes_the_assigned_elementary_school(zone_db):
    url, _ = zone_db
    with client(url) as c:
        inside = c.get(f"{API}/nearby", params=ORIGIN).json()
        shared = c.get(f"{API}/nearby", params=NORTH).json()
        outside = c.get(f"{API}/nearby", params=OUTSIDE).json()
    assigned = inside["data"]["schools"]["assigned_elementary"]
    assert assigned["id"] == "7240040" and assigned["name"] == "가상40초등학교" and assigned["level"] == "elementary"
    assert 40 <= assigned["distance_m"] <= 70
    assert inside["data"]["schools"]["elementary"][0]["name"] == "가상10초등학교"  # 가까운 학교 목록은 그대로
    assert shared["data"]["schools"]["assigned_elementary"]["name"] == "가상30초등학교"
    assert "assigned_elementary" in outside["data"]["schools"]
    assert outside["data"]["schools"]["assigned_elementary"] is None
    assert "한국교육시설안전원 학구도(기준일 2026-09-20)" in inside["meta"]["source_attribution"]


def test_complex_nearby_has_the_key_too(zone_db):
    url, _ = zone_db
    with client(url) as c:
        hits = c.get(f"{API}/search", params={"q": "해운대"}).json()["data"]
        ids = [h["complex_id"] for h in hits if h.get("complex_id")]
        r = c.get(f"{API}/complexes/{ids[0]}/nearby")
    assert r.status_code == 200 and "assigned_elementary" in r.json()["data"]["schools"]


@pytest.fixture(scope="module")
def unloaded_db(postgis: str, tmp_path_factory) -> str:
    url = create_database(postgis, "school_zone_unloaded")
    migrate.upgrade(url)
    asyncio.run(_populate(url, GeocodeCache(tmp_path_factory.mktemp("zone_geo2") / "geocode.json")))
    return url


def test_before_zones_are_loaded_the_key_is_absent_and_zone_is_not_ready(unloaded_db):
    with client(unloaded_db) as c:
        nearby = c.get(f"{API}/nearby", params=ORIGIN)
        zone = c.get(f"{API}/schools/7240040/zone")
    assert nearby.status_code == 200 and "assigned_elementary" not in nearby.json()["data"]["schools"]
    assert nearby.json()["meta"]["source_attribution"] == ["나이스 교육정보 개방 포털", "건강보험심사평가원"]
    assert zone.status_code == 503 and zone.json()["error"]["code"] == "DATA_NOT_READY"


def test_zone_without_a_database_is_not_ready():
    with client(None) as c:
        r = c.get(f"{API}/schools/7240040/zone")
    assert r.status_code == 503 and r.json()["error"]["code"] == "DATA_NOT_READY"


def test_loading_needs_the_school_list_first(postgis, tmp_path):
    url = create_database(postgis, f"school_zone_noschools_{uuid4().hex[:8]}")
    migrate.upgrade(url)
    with pytest.raises(NoElementarySchools):
        asyncio.run(_load(url, write_zone_files(tmp_path, ZONES, LINKS)))


# ── 주변 학교 응답의 assigned_elementary 키 (앱 계약: 미적재면 키 없음, 구역 밖이면 null) ──


def _nearby(zone_info, assigned):
    return Nearby(
        schools={},
        school_radius_m=3000,
        hospital_radius_m=1000,
        hospital_counts={t: 0 for t in HospitalTier},
        nearest_general=None,
        nearest_hospitals=[],
        academy_radius_m=1000,
        academy_counts={c: 0 for c in AcademyCategory},
        zone_info=zone_info,
        assigned_elementary=assigned,
    )


def test_assigned_elementary_key_follows_the_zone_load_state():
    info = SchoolZoneInfo(date(2026, 9, 20), datetime(2026, 10, 6))
    school = School(
        id="7240040", name="가상40초등학교", kind="초등학교", level=SchoolLevel.elementary, foundation="공립",
        coed=None, address="주소", lat=35.16, lng=129.16, tel=None, homepage=None, opened_on=None, distance_m=57,
    )  # fmt: skip

    def dumped(n):
        data = NearbyResponse.model_validate(
            {"data": NearbyData.from_domain(n).model_dump(), "meta": {"data_as_of": info.loaded_at}}
        )
        return data.model_dump(mode="json")["data"]["schools"]

    assert "assigned_elementary" not in dumped(_nearby(None, None))
    assert dumped(_nearby(info, None))["assigned_elementary"] is None
    assert dumped(_nearby(info, school))["assigned_elementary"]["distance_m"] == 57


# ── 같은 이름 학교(부산 송정초 둘)는 구역 위치로 구분해 연결한다 ──

# (id, 이름, 위도, 경도). ORIGIN 구역 안 / NORTH 구역 안 / 좌표 없음
DONGMYEONG_ORIGIN = ("7249001", "동명초등학교", 35.1601, 129.1601)
DONGMYEONG_NORTH = ("7249002", "동명초등학교", 35.1701, 129.1601)
DONGMYEONG_NOWHERE = ("7249003", "동명초등학교", None, None)


async def _insert_schools(url: str, schools: list[tuple[str, str, float | None, float | None]]) -> None:
    engine = make_engine(url)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
                INSERT INTO schools (id, name, kind, address, location, last_seen_at)
                VALUES (:id, :name, '초등학교', '부산',
                        ST_SetSRID(ST_MakePoint(CAST(:lng AS float8), CAST(:lat AS float8)), 4326), now())
                """
            ),
            [{"id": i, "name": n, "lat": lat, "lng": lng} for i, n, lat, lng in schools],
        )
    await engine.dispose()


async def _links(url: str) -> list[tuple[str, str]]:
    engine = make_engine(url)
    async with engine.connect() as conn:
        rows = (await conn.execute(text("SELECT zone_id, school_id FROM school_zone_schools ORDER BY 1"))).all()
    await engine.dispose()
    return [tuple(r) for r in rows]


def _fresh_db(postgis: str, name: str, schools) -> str:
    url = create_database(postgis, f"school_zone_{name}_{uuid4().hex[:8]}")
    migrate.upgrade(url)
    asyncio.run(_insert_schools(url, schools))
    return url


def test_same_named_schools_are_linked_to_their_own_zone(postgis, tmp_path):
    url = _fresh_db(postgis, "same_name", [DONGMYEONG_ORIGIN, DONGMYEONG_NORTH, DONGMYEONG_NOWHERE])
    zones = [
        FakeZone("Z000000011", "동명초통학구역", rings=[square(ORIGIN_5186, 300)]),
        FakeZone("Z000000012", "동명초통학구역", rings=[square(NORTH_5186, 300)]),
        FakeZone("Z000000014", "동명초통학구역"),  # 도형 없음: 위치로 고를 수 없지만 ambiguous가 아니다
    ]
    links = [
        link("Z000000011", "B11", "동명초등학교"),
        link("Z000000012", "B12", "동명초등학교"),
        link("Z000000014", "B14", "동명초등학교"),
    ]
    report = asyncio.run(_load(url, write_zone_files(tmp_path, zones, links)))
    assert asyncio.run(_links(url)) == [("Z000000011", "7249001"), ("Z000000012", "7249002")]
    assert report.ambiguous == [] and report.without_geometry == ["Z000000014"]
    assert report.by_location == [
        ("동명초등학교", "Z000000011", "7249001"),
        ("동명초등학교", "Z000000012", "7249002"),
    ]


def test_a_far_zone_of_another_source_school_is_not_linked(postgis, tmp_path):
    # 우리 목록에는 동명초가 하나뿐인데 학구도에는 학교ID가 다른 동명초가 둘(하나는 약 5km 떨어진 구역)
    url = _fresh_db(postgis, "far", [DONGMYEONG_ORIGIN])
    zones = [
        FakeZone("Z000000011", "동명초통학구역", rings=[square(ORIGIN_5186, 300)]),
        FakeZone("Z000000013", "동명초통학구역", rings=[square(FAR_5186, 200)]),
    ]
    links = [link("Z000000011", "B11", "동명초등학교"), link("Z000000013", "B13", "동명초등학교")]
    report = asyncio.run(_load(url, write_zone_files(tmp_path, zones, links)))
    assert asyncio.run(_links(url)) == [("Z000000011", "7249001")]
    assert report.ambiguous == ["동명초등학교(B13)"]


def test_one_school_is_not_given_to_two_source_schools(postgis, tmp_path):
    # 하나뿐인 우리 학교가 두 구역 모두에서 2km 안이라 둘 다 고르면 일대일이 깨진다 → 둘 다 연결하지 않는다
    url = _fresh_db(postgis, "one_to_one", [DONGMYEONG_ORIGIN])
    zones = [
        FakeZone("Z000000011", "동명초통학구역", rings=[square(ORIGIN_5186, 300)]),
        FakeZone("Z000000012", "동명초통학구역", rings=[square(NORTH_5186, 300)]),
    ]
    links = [link("Z000000011", "B11", "동명초등학교"), link("Z000000012", "B12", "동명초등학교")]
    report = asyncio.run(_load(url, write_zone_files(tmp_path, zones, links)))
    assert asyncio.run(_links(url)) == []
    assert report.ambiguous == ["동명초등학교(B11)", "동명초등학교(B12)"]


def test_same_named_schools_without_locations_are_skipped(postgis, tmp_path):
    url = _fresh_db(
        postgis, "no_location", [(i, n, None, None) for i, n, _, _ in [DONGMYEONG_ORIGIN, DONGMYEONG_NORTH]]
    )
    zones = [FakeZone("Z000000011", "동명초통학구역", rings=[square(ORIGIN_5186, 300)])]
    report = asyncio.run(_load(url, write_zone_files(tmp_path, zones, [link("Z000000011", "B11", "동명초등학교")])))
    assert asyncio.run(_links(url)) == []
    assert report.ambiguous == ["동명초등학교(B11)"]


# ── 구멍 뚫린 구역, 좌표 없는 배정 학교 ──


def test_points_in_a_hole_are_outside_and_unlocated_schools_are_still_assigned(postgis, tmp_path):
    url = _fresh_db(
        postgis,
        "hole",
        [("7249101", "구멍초등학교", 35.1601, 129.1601), ("7249102", "무좌표초등학교", None, None)],
    )
    zones = [
        # ORIGIN을 둘러싼 500m 구역에 가운데 100m 구멍
        FakeZone("Z000000021", "구멍초통학구역", rings=[square(ORIGIN_5186, 500), square(ORIGIN_5186, 100)[::-1]]),
        FakeZone("Z000000022", "무좌표초통학구역", rings=[square(NORTH_5186, 300)]),
    ]
    links = [link("Z000000021", "B21", "구멍초등학교"), link("Z000000022", "B22", "무좌표초등학교")]
    report = asyncio.run(_load(url, write_zone_files(tmp_path, zones, links)))
    assert report.linked_without_location == ["무좌표초등학교"]

    async def assigned(lat: float, lng: float):
        engine = make_engine(url)
        try:
            found = await PostgresFacilityRepository(engine).nearby(
                lat, lng, today=FIXED_NOW.date(), school_radius_m=3000, schools_per_level=3,
                hospital_radius_m=1000, nearest_hospitals=5, academy_radius_m=1000,
            )  # fmt: skip
        finally:
            await engine.dispose()
        assert found.zone_info is not None
        return found.assigned_elementary

    assert asyncio.run(assigned(35.16, 129.16)) is None  # 구멍 안
    assert asyncio.run(assigned(35.16, 129.1633)).id == "7249101"  # 동쪽 약 300m, 구멍 밖 구역 안
    unlocated = asyncio.run(assigned(35.17, 129.16))
    assert (unlocated.id, unlocated.distance_m, unlocated.lat) == ("7249102", None, None)

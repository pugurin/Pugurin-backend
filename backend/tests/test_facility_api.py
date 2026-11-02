"""학교·병원 조회 API (실제 PostGIS). 데이터는 가짜 나이스·심평원 응답으로 동기화해 채운다."""

import asyncio
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.db import migrate
from app.db.engine import make_engine
from app.ingestion.facilities import sync_facilities
from app.ingestion.geocode import GeocodeCache
from tests.conftest import API, FIXED_NOW, create_database
from tests.fakes.app import create_app
from tests.fakes.madangs import FakeLookup
from tests.test_facilities import FakeHospitals, FakeSchools, hospital, school

# FakeLookup은 "…가상로 n"을 (35.16 + n/100000, 129.16 + n/100000)에 둔다
ORIGIN = {"lat": 35.16, "lng": 129.16}
AROUND = "129.15,35.15,129.17,35.17"


def schools_rows() -> list[dict]:
    return [
        school(30),  # 약 42m
        school(10),  # 약 14m
        school(20),  # 약 28m
        school(40),
        school(50, SD_SCHUL_CODE="7240099", SCHUL_KND_SC_NM="중학교", SCHUL_NM="가상중학교"),
        school(60, SD_SCHUL_CODE="       ", SCHUL_NM="(가칭)가상초등학교", FOAS_MEMRD="20300301"),  # 개교 전
        school(70, SCHUL_KND_SC_NM="특수학교"),
    ]


def hospital_rows() -> list[dict]:
    near = {"XPos": "129.1601", "YPos": "35.1601"}  # 약 14m
    return [
        hospital(1, **near, drTotCnt=1),
        hospital(2, **near, clCdNm="치과의원", drTotCnt=3),
        hospital(3, XPos="129.1650", YPos="35.1650", clCdNm="한의원"),  # 약 710m
        hospital(4, XPos="129.1800", YPos="35.1800", clCdNm="종합병원", drTotCnt=120),  # 약 2.8km
        hospital(5, XPos="129.2500", YPos="35.2500", clCdNm="상급종합", drTotCnt=300),  # 더 멀다
        # 경도 1도는 위도 1도보다 짧다: 각도로는 7번이 가깝지만 실제로는 6번(약 912m)이 7번(약 943m)보다 가깝다
        hospital(6, XPos="129.1700", YPos="35.1600"),
        hospital(7, XPos="129.1600", YPos="35.1685"),
    ]


async def _populate(url: str, cache: GeocodeCache) -> None:
    engine = make_engine(url)
    await sync_facilities(
        engine, FakeSchools(schools_rows()), FakeHospitals(hospital_rows()), FakeLookup(), cache,
        now=FIXED_NOW, out=lambda _: None,
    )  # fmt: skip
    await engine.dispose()


@pytest.fixture(scope="module")
def facility_api_db(postgis: str, tmp_path_factory) -> str:
    url = create_database(postgis, "facility_api")
    migrate.upgrade(url)
    asyncio.run(_populate(url, GeocodeCache(tmp_path_factory.mktemp("facility_geo") / "geocode.json")))
    return url


def client(url: str | None) -> TestClient:
    settings = Settings(database_url=url) if url else Settings()
    return TestClient(create_app(settings=settings, clock=lambda: FIXED_NOW), headers={"X-Device-Id": str(uuid4())})


def test_nearby_lists_closest_open_schools_per_level_and_counts_hospitals(facility_api_db):
    with client(facility_api_db) as c:
        r = c.get(f"{API}/nearby", params=ORIGIN)
    assert r.status_code == 200
    body = r.json()
    schools = body["data"]["schools"]
    assert [s["name"] for s in schools["elementary"]] == ["가상10초등학교", "가상20초등학교", "가상30초등학교"]
    assert [s["distance_m"] for s in schools["elementary"]] == sorted(s["distance_m"] for s in schools["elementary"])
    assert 10 <= schools["elementary"][0]["distance_m"] <= 20
    assert [s["name"] for s in schools["middle"]] == ["가상중학교"]
    assert schools["high"] == []
    hospitals = body["data"]["hospitals"]
    assert hospitals["radius_m"] == 1000 and hospitals["total"] == 5
    assert (hospitals["counts"]["clinic"], hospitals["counts"]["dental"], hospitals["counts"]["oriental"]) == (3, 1, 1)
    assert hospitals["counts"]["general"] == 0
    assert hospitals["nearest_general"]["name"] == "가상4의원" and hospitals["nearest_general"]["tier"] == "general"
    assert 2500 < hospitals["nearest_general"]["distance_m"] < 3100
    assert [h["name"] for h in hospitals["nearest"]] == [
        "가상1의원",
        "가상2의원",
        "가상3의원",
        "가상6의원",
        "가상7의원",
    ]
    assert [h["distance_m"] for h in hospitals["nearest"]] == sorted(h["distance_m"] for h in hospitals["nearest"])
    meta = body["meta"]
    assert meta["school_notice"].startswith("배정 학교")
    assert meta["source_attribution"] == ["나이스 교육정보 개방 포털", "건강보험심사평가원"]


def test_map_layers_filter_by_level_and_tier(facility_api_db):
    with client(facility_api_db) as c:
        elementary = c.get(f"{API}/schools", params={"bbox": AROUND, "level": "elementary"}).json()["data"]
        special = c.get(f"{API}/schools", params={"bbox": AROUND, "level": "special"}).json()["data"]
        everything = c.get(f"{API}/schools", params={"bbox": AROUND}).json()["data"]
        general = c.get(f"{API}/hospitals", params={"bbox": "129.0,35.0,129.3,35.3", "tier": "general"}).json()
        bad = c.get(f"{API}/hospitals", params={"bbox": AROUND, "tier": "castle"})
    assert len(elementary) == 4 and {s["level"] for s in elementary} == {"elementary"}
    assert [s["name"] for s in special] == ["가상70초등학교"]
    assert "(가칭)가상초등학교" not in {s["name"] for s in everything}  # 개교 전은 빠진다
    assert [h["name"] for h in general["data"]] == ["가상5의원", "가상4의원"]  # 의사 많은 순
    assert bad.status_code == 400 and bad.json()["error"]["field"] == "tier"


def test_points_outside_busan_are_rejected(facility_api_db):
    with client(facility_api_db) as c:
        r = c.get(f"{API}/nearby", params={"lat": 37.56, "lng": 126.97})
    assert r.status_code == 400 and r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_complex_nearby_uses_the_complex_location(facility_api_db):
    with client(facility_api_db) as c:
        complex_id = c.get(f"{API}/search", params={"q": "해운대"}).json()["data"]
        ids = [h["complex_id"] for h in complex_id if h.get("complex_id")]
        assert ids
        r = c.get(f"{API}/complexes/{ids[0]}/nearby")
        missing = c.get(f"{API}/complexes/{uuid4()}/nearby")
    assert r.status_code == 200 and set(r.json()["data"]) == {"schools", "hospitals"}
    assert missing.status_code == 404


def test_before_the_first_sync_it_says_there_is_no_data(postgis):
    url = create_database(postgis, "facility_empty")
    migrate.upgrade(url)
    with client(url) as c:
        r = c.get(f"{API}/nearby", params=ORIGIN)
    assert r.status_code == 503 and "학교·병원" in r.json()["error"]["message"]


def test_without_a_database_it_says_there_is_no_data():
    with client(None) as c:
        r = c.get(f"{API}/schools", params={"bbox": AROUND})
    assert r.status_code == 503 and r.json()["error"]["code"] == "DATA_NOT_READY"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("초등학교", "elementary"),
        ("중학교", "middle"),
        ("고등학교", "high"),
        ("특수학교", "special"),
        ("외국인학교", "other"),
    ],
)
def test_school_kinds_map_to_levels(value, expected):
    from app.repositories.postgres.facilities import school_level

    assert school_level(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("상급종합", "general"),
        ("종합병원", "general"),
        ("요양병원", "hospital"),
        ("의원", "clinic"),
        ("치과의원", "dental"),
        ("한의원", "oriental"),
        ("보건소", "public"),
        ("조산원", "other"),
    ],
)
def test_hospital_classes_map_to_tiers(value, expected):
    from app.repositories.postgres.facilities import hospital_tier

    assert hospital_tier(value) == expected

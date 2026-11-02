"""경매·공매 조회 API (실제 PostGIS). 데이터는 가짜 경매마당 응답으로 동기화해 채운다."""

import asyncio
from datetime import timedelta
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.config import Settings
from app.db import migrate
from app.db.engine import make_engine
from app.ingestion.auctions import sync_auctions
from app.ingestion.geocode import GeocodeCache
from app.main import create_app
from tests.conftest import API, BUSAN, FIXED_NOW, create_database
from tests.fakes.madangs import PERSONAL, FakeAuctionSource, FakeLookup, court_row, public_row

COMPLEX_ID = UUID("6f1d2a8e-0000-4000-8000-000000000001")
# FakeLookup이 "부산 해운대구 우동 601"에 주는 PNU
PNU_601 = "2635010500" + "1" + "0601" + "0000"


def client(url: str) -> TestClient:
    return TestClient(
        create_app(settings=Settings(database_url=url), clock=lambda: FIXED_NOW),
        headers={"X-Device-Id": "2f8f1d52-7b0e-4c55-9c0e-3f6f9d1c2a11"},
    )


async def _populate(url: str, cache: GeocodeCache) -> None:
    engine = make_engine(url)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
                INSERT INTO complexes (id, property_type, name, name_key, region_code, jibun, pnu, location,
                                       location_source)
                VALUES (:id, 'apartment', '가상마린', '가상마린', '2635010500', '601', :pnu,
                        ST_SetSRID(ST_MakePoint(129.166, 35.166), 4326), 'geocode')
                """
            ),
            {"id": COMPLEX_ID, "pnu": PNU_601},
        )
    court = [
        court_row(1),
        court_row(2, 물건용도="오피스텔(주거)"),
        court_row(3, 물건용도="근린생활시설", 매각기일="2026-11-20"),
        court_row(4, 물건용도="대지"),
    ]
    source = FakeAuctionSource(court, [public_row(1)])
    await sync_auctions(engine, source, FakeLookup(), cache, now=FIXED_NOW, out=lambda _: None)
    # 4번은 다음 동기화에서 목록에서 빠져 끝난 물건이 된다
    source.court = court[:3]
    await sync_auctions(engine, source, FakeLookup(), cache, now=FIXED_NOW + timedelta(hours=1), out=lambda _: None)
    await engine.dispose()


@pytest.fixture(scope="module")
def auction_api_db(postgis: str, tmp_path_factory) -> str:
    url = create_database(postgis, "auction_api")
    migrate.upgrade(url)
    asyncio.run(_populate(url, GeocodeCache(tmp_path_factory.mktemp("auction_geo") / "geocode.json")))
    return url


def ids(r) -> list[str]:
    return [a["id"] for a in r.json()["data"]]


def test_lists_open_items_with_prices_areas_and_source_attribution(auction_api_db):
    with client(auction_api_db) as c:
        r = c.get(f"{API}/auctions")
    assert r.status_code == 200
    body = r.json()
    assert body["meta"]["total"] == 4  # 경매 3 + 공매 1, 끝난 1건은 빠진다
    assert body["meta"]["source_attribution"] == "대법원 법원경매정보"
    assert body["meta"]["data_as_of"].startswith("2026-")
    # 매각기일 순: 공매(11-04 마감) → 경매 11-10 두 건 → 11-20
    assert [a["id"] for a in body["data"]] == [
        "GO-2026-1000-000001",
        "CA-41-2025-1001-1",
        "CA-41-2025-1002-1",
        "CA-41-2025-1003-1",
    ]
    first = body["data"][1]
    assert (first["property_group"], first["min_price_ratio"], first["fail_count"]) == ("apartment", 70, 1)
    assert first["building_area_pyeong"] == 25.7 and first["lat"] is not None
    assert first["complex_id"] == str(COMPLEX_ID)


def test_filters_by_bbox_source_group_and_complex(auction_api_db):
    with client(auction_api_db) as c:
        assert len(ids(c.get(f"{API}/auctions", params={"bbox": BUSAN}))) == 4
        assert ids(c.get(f"{API}/auctions", params={"bbox": "126.9,37.4,127.1,37.6"})) == []
        assert ids(c.get(f"{API}/auctions", params={"source": "public"})) == ["GO-2026-1000-000001"]
        assert ids(c.get(f"{API}/auctions", params={"property_group": "officetel,other"})) == [
            "CA-41-2025-1002-1",
            "CA-41-2025-1003-1",
        ]
        assert ids(c.get(f"{API}/auctions", params={"property_group": "villa"})) == ["GO-2026-1000-000001"]
        assert ids(c.get(f"{API}/auctions", params={"complex_id": str(COMPLEX_ID)})) == ["CA-41-2025-1001-1"]
        closed = c.get(f"{API}/auctions", params={"include_closed": True, "property_group": "land"}).json()["data"]
        assert [(a["id"], a["is_closed"]) for a in closed] == [("CA-41-2025-1004-1", True)]
        bad = c.get(f"{API}/auctions", params={"property_group": "castle"})
        assert bad.status_code == 400 and bad.json()["error"]["field"] == "property_group"


def test_detail_has_schedule_and_results_without_personal_data(auction_api_db):
    with client(auction_api_db) as c:
        r = c.get(f"{API}/auctions/CA-41-2025-1001-1")
        missing = c.get(f"{API}/auctions/CA-00-0000-0-0")
    assert r.status_code == 200
    data = r.json()["data"]
    assert [s["result"] for s in data["schedule"]] == ["유찰", "진행"]
    assert data["results"] == [{"date": "2026-09-01", "result": "유찰", "bidders": 0, "top_price": None}]
    for name in PERSONAL:
        assert name not in r.text
    assert missing.status_code == 404


def test_before_the_first_sync_it_says_there_is_no_data(postgis):
    url = create_database(postgis, "auction_empty")
    migrate.upgrade(url)
    with client(url) as c:
        r = c.get(f"{API}/auctions")
    assert r.status_code == 503 and r.json()["error"]["code"] == "DATA_NOT_READY"
    assert "경매" in r.json()["error"]["message"]


def test_without_a_database_it_says_there_is_no_data():
    from tests.fakes.app import create_app as fake_app

    with TestClient(fake_app(clock=lambda: FIXED_NOW), headers={"X-Device-Id": str(COMPLEX_ID)}) as c:
        r = c.get(f"{API}/auctions")
    assert r.status_code == 503 and r.json()["error"]["code"] == "DATA_NOT_READY"


@pytest.mark.parametrize(
    ("usage", "group"),
    [
        ("아파트", "apartment"),
        ("아파트(생활주택)", "apartment"),
        ("주상복합", "apartment"),
        ("오피스텔(주거)", "officetel"),
        ("오피스텔", "officetel"),
        ("오피스텔(상가)", "other"),
        ("다세대(생활주택)", "villa"),
        ("다세대주택", "villa"),
        ("연립", "villa"),
        ("연립주택", "villa"),
        ("도시형생활주택", "villa"),  # 공매 이름
        ("빌라", "villa"),
        ("기타토지", "land"),
        ("대지", "land"),
        ("임야", "land"),
        ("단독주택", "other"),
        ("근린생활시설", "other"),
        (None, "other"),
    ],
)
def test_usage_names_map_to_our_property_groups(usage, group):
    from app.repositories.postgres.auctions import auction_group

    assert auction_group(usage) == group

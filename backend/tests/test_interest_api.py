"""관심 단지·관심 경매 물건 API (실제 PostGIS). 단지·거래·경매는 경매 API 테스트와 같은 데이터로 채운다."""

import asyncio
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import app.services.interest_service as interest_service
from app.core.config import Settings
from app.db import migrate
from app.ingestion.geocode import GeocodeCache
from app.main import create_app
from tests.conftest import API, FIXED_NOW, create_database
from tests.test_auction_api import COMPLEX_ID, OFFICETEL_ID
from tests.test_auction_api import _populate as populate_market_and_auctions


@pytest.fixture(scope="module")
def interest_db(postgis: str, tmp_path_factory) -> str:
    url = create_database(postgis, "interest_api")
    migrate.upgrade(url)
    cache = GeocodeCache(tmp_path_factory.mktemp("interest_geo") / "geocode.json")
    asyncio.run(populate_market_and_auctions(url, cache))
    return url


def client(url: str | None, device: str | None = None) -> TestClient:
    settings = Settings(database_url=url) if url else Settings()
    return TestClient(
        create_app(settings=settings, clock=lambda: FIXED_NOW), headers={"X-Device-Id": device or str(uuid4())}
    )


def auction_ids(c: TestClient) -> dict[str, str]:
    """사건번호 → id (끝난 물건 포함)."""
    r = c.get(f"{API}/auctions", params={"include_closed": "true"})
    assert r.status_code == 200
    return {a["case_no"]: a["id"] for a in r.json()["data"]}


def test_complex_interest_is_idempotent_and_lists_latest_sale(interest_db):
    with client(interest_db) as c:
        first = c.put(f"{API}/interests/complexes/{COMPLEX_ID}")
        again = c.put(f"{API}/interests/complexes/{COMPLEX_ID}")
        assert first.status_code == again.status_code == 200
        assert first.json()["data"] == again.json()["data"]  # 다시 넣어도 처음 등록 시각 그대로
        assert first.json()["data"]["target_id"] == str(COMPLEX_ID)

        r = c.get(f"{API}/interests/complexes")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "private, no-cache"
    body = r.json()
    assert body["meta"] == {"total": 1, "max_items": 200}
    [item] = body["data"]
    assert item["complex"]["id"] == str(COMPLEX_ID)
    assert item["complex"]["name"] == "가상마린"
    # 해제 거래(9/20)는 빼고 가장 최근 매매
    assert (item["latest_sale"]["contract_date"], item["latest_sale"]["price"]) == ("2026-09-25", 400_000_000)
    assert item["added_at"]


def test_lists_are_separate_per_device_and_newest_first(interest_db):
    device = str(uuid4())
    with client(interest_db, device) as c:
        assert c.put(f"{API}/interests/complexes/{COMPLEX_ID}").status_code == 200
        assert c.put(f"{API}/interests/complexes/{OFFICETEL_ID}").status_code == 200
        ids = c.get(f"{API}/interests").json()["data"]
    assert ids == {"complexes": [str(OFFICETEL_ID), str(COMPLEX_ID)], "auctions": []}
    with client(interest_db) as other:
        assert other.get(f"{API}/interests").json()["data"] == {"complexes": [], "auctions": []}
        assert other.get(f"{API}/interests/complexes").json()["data"] == []


def test_auction_interest_keeps_closed_items(interest_db):
    with client(interest_db) as c:
        ids = auction_ids(c)
        open_id, closed_id = ids["2025타경1001"], ids["2025타경1004"]
        assert c.put(f"{API}/interests/auctions/{open_id}").status_code == 200
        assert c.put(f"{API}/interests/auctions/{closed_id}").status_code == 200
        r = c.get(f"{API}/interests/auctions")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "private, no-cache"
    body = r.json()
    assert [(i["auction"]["id"], i["auction"]["is_closed"]) for i in body["data"]] == [
        (closed_id, True),
        (open_id, False),
    ]
    assert body["meta"]["total"] == 2
    assert body["meta"]["source_attribution"] == "대법원 법원경매정보"


def test_delete_is_idempotent(interest_db):
    with client(interest_db) as c:
        auction = auction_ids(c)["2025타경1002"]
        c.put(f"{API}/interests/complexes/{COMPLEX_ID}")
        c.put(f"{API}/interests/auctions/{auction}")
        for _ in range(2):
            assert c.delete(f"{API}/interests/complexes/{COMPLEX_ID}").status_code == 204
            assert c.delete(f"{API}/interests/auctions/{auction}").status_code == 204
        assert c.get(f"{API}/interests").json()["data"] == {"complexes": [], "auctions": []}


def test_unknown_targets_are_404(interest_db):
    with client(interest_db) as c:
        r = c.put(f"{API}/interests/complexes/{uuid4()}")
        assert (r.status_code, r.json()["error"]["code"]) == (404, "RESOURCE_NOT_FOUND")
        r = c.put(f"{API}/interests/auctions/CA-00-0000-0000-0")
        assert (r.status_code, r.json()["error"]["code"]) == (404, "RESOURCE_NOT_FOUND")
        assert c.get(f"{API}/interests").json()["data"] == {"complexes": [], "auctions": []}


def test_limit_per_kind_is_409(interest_db, monkeypatch):
    monkeypatch.setattr(interest_service, "MAX_PER_KIND", 1)
    with client(interest_db) as c:
        assert c.put(f"{API}/interests/complexes/{COMPLEX_ID}").status_code == 200
        r = c.put(f"{API}/interests/complexes/{OFFICETEL_ID}")
        assert (r.status_code, r.json()["error"]["code"]) == (409, "CONFLICT")
        assert c.put(f"{API}/interests/complexes/{COMPLEX_ID}").status_code == 200  # 이미 있는 건 한도와 상관없다


def test_requires_device_id(interest_db):
    with TestClient(create_app(settings=Settings(database_url=interest_db), clock=lambda: FIXED_NOW)) as c:
        r = c.get(f"{API}/interests")
    assert (r.status_code, r.json()["error"]["field"]) == (400, "X-Device-Id")


def test_without_a_database_it_says_there_is_no_data():
    with client(None) as c:
        r = c.get(f"{API}/interests")
    assert (r.status_code, r.json()["error"]["code"]) == (503, "DATA_NOT_READY")

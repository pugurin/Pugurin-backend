"""관심 단지·관심 경매 물건 API (실제 PostGIS). 로그인한 회원만 쓴다(docs/AUTH.md).

단지·거래·경매는 경매 API 테스트와 같은 데이터로 채운다. 회원은 가짜 카카오로 가입한다.
"""

import asyncio
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

import app.services.interest_service as interest_service
from app.core.config import Settings
from app.db import migrate
from app.db.engine import make_engine
from app.ingestion.geocode import GeocodeCache
from app.main import create_app
from app.repositories.types import SocialProvider
from tests.conftest import API, FIXED_NOW, create_database
from tests.fakes.oauth import FakeOAuth
from tests.test_auction_api import COMPLEX_ID, OFFICETEL_ID
from tests.test_auction_api import _populate as populate_market_and_auctions

SECRET = "test-secret-" + "x" * 32
AGREE = {"terms": True, "privacy": True, "age14": True, "marketing": False}


@pytest.fixture(scope="module")
def interest_db(postgis: str, tmp_path_factory) -> str:
    url = create_database(postgis, "interest_api")
    migrate.upgrade(url)
    cache = GeocodeCache(tmp_path_factory.mktemp("interest_geo") / "geocode.json")
    asyncio.run(populate_market_and_auctions(url, cache))
    return url


def app_for(url: str | None, kakao: FakeOAuth | None = None):
    settings = Settings(database_url=url, jwt_secret=SECRET) if url else Settings(jwt_secret=SECRET)
    providers = {p: FakeOAuth(p) for p in SocialProvider}
    if kakao:
        providers[SocialProvider.kakao] = kakao
    return create_app(settings=settings, clock=lambda: FIXED_NOW, oauth_providers=providers)


def sign_up(c: TestClient, kakao: FakeOAuth) -> str:
    """새 카카오 회원으로 가입하고 access token을 돌려준다."""
    token = f"sdk-{uuid4()}"
    kakao.tokens[token] = kakao.user(str(uuid4()))
    first = c.post(f"{API}/auth/oauth/kakao", json={"access_token": token}).json()["data"]
    r = c.post(
        f"{API}/auth/signup", json={"signup_token": first["signup_token"], "nickname": "부산사람", "agreements": AGREE}
    )
    assert r.status_code == 200, r.text
    return r.json()["data"]["access_token"]


def client(url: str | None) -> TestClient:
    """가입을 마친 회원의 클라이언트(요청마다 Bearer 토큰을 붙인다)."""
    kakao = FakeOAuth(SocialProvider.kakao)
    c = TestClient(app_for(url, kakao), headers={"X-Device-Id": str(uuid4())})
    if url:
        with c:
            c.headers["Authorization"] = f"Bearer {sign_up(c, kakao)}"
    return c


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


def test_lists_are_separate_per_member_and_newest_first(interest_db):
    with client(interest_db) as c:
        assert c.put(f"{API}/interests/complexes/{COMPLEX_ID}").status_code == 200
        assert c.put(f"{API}/interests/complexes/{OFFICETEL_ID}").status_code == 200
        ids = c.get(f"{API}/interests").json()["data"]
    assert ids == {"complexes": [str(OFFICETEL_ID), str(COMPLEX_ID)], "auctions": [], "regions": []}
    with client(interest_db) as other:
        assert other.get(f"{API}/interests").json()["data"] == {"complexes": [], "auctions": [], "regions": []}
        assert other.get(f"{API}/interests/complexes").json()["data"] == []


def test_the_list_follows_the_member_across_devices(interest_db):
    with client(interest_db) as c:
        assert c.put(f"{API}/interests/complexes/{COMPLEX_ID}").status_code == 200
        # 같은 회원이 다른 기기(다른 X-Device-Id)에서 봐도 같은 목록이다
        r = c.get(f"{API}/interests", headers={"X-Device-Id": str(uuid4())})
    assert r.json()["data"]["complexes"] == [str(COMPLEX_ID)]


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
        assert c.get(f"{API}/interests").json()["data"] == {"complexes": [], "auctions": [], "regions": []}


def test_unknown_targets_are_404(interest_db):
    with client(interest_db) as c:
        r = c.put(f"{API}/interests/complexes/{uuid4()}")
        assert (r.status_code, r.json()["error"]["code"]) == (404, "RESOURCE_NOT_FOUND")
        r = c.put(f"{API}/interests/auctions/CA-00-0000-0000-0")
        assert (r.status_code, r.json()["error"]["code"]) == (404, "RESOURCE_NOT_FOUND")
        assert c.get(f"{API}/interests").json()["data"] == {"complexes": [], "auctions": [], "regions": []}


def test_limit_per_kind_is_409(interest_db, monkeypatch):
    monkeypatch.setattr(interest_service, "MAX_PER_KIND", 1)
    with client(interest_db) as c:
        assert c.put(f"{API}/interests/complexes/{COMPLEX_ID}").status_code == 200
        r = c.put(f"{API}/interests/complexes/{OFFICETEL_ID}")
        assert (r.status_code, r.json()["error"]["code"]) == (409, "CONFLICT")
        assert c.put(f"{API}/interests/complexes/{COMPLEX_ID}").status_code == 200  # 이미 있는 건 한도와 상관없다


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/interests"),
        ("GET", "/interests/complexes"),
        ("PUT", f"/interests/complexes/{COMPLEX_ID}"),
        ("DELETE", f"/interests/complexes/{COMPLEX_ID}"),
        ("GET", "/interests/auctions"),
        ("PUT", "/interests/auctions/CA-00-0000-0000-0"),
        ("DELETE", "/interests/auctions/CA-00-0000-0000-0"),
    ],
)
@pytest.mark.parametrize("authorization", [None, "Bearer not-a-token"])
def test_interests_need_login(interest_db, method, path, authorization):
    headers = {"Authorization": authorization} if authorization else {}
    with TestClient(app_for(interest_db)) as c:
        r = c.request(method, f"{API}{path}", headers=headers)
    assert (r.status_code, r.json()["error"]["code"]) == (401, "UNAUTHORIZED")


def test_withdrawing_deletes_the_list(interest_db):
    with client(interest_db) as c:
        assert c.put(f"{API}/interests/complexes/{COMPLEX_ID}").status_code == 200
        user_id = c.get(f"{API}/users/me").json()["data"]["id"]
        assert c.delete(f"{API}/users/me").status_code == 204

    async def rows() -> int:
        engine = make_engine(interest_db)
        async with engine.connect() as conn:
            n = await conn.execute(text("SELECT count(*) FROM user_interests WHERE user_id = :id"), {"id": user_id})
            count = n.scalar()
        await engine.dispose()
        return count

    assert asyncio.run(rows()) == 0


def test_without_a_database_interests_are_503():
    # DB가 없으면 로그인부터 쓸 수 없다(인증이 먼저 503)
    with client(None) as c:
        r = c.get(f"{API}/interests", headers={"Authorization": "Bearer x"})
    assert r.status_code == 503


def dong_codes(url: str, n: int, *, with_centroid: bool = False) -> list[str]:
    """테스트 DB의 법정동 코드 n개. `with_centroid`면 첫 동에 중심점을 심어 좌표 응답을 확인할 수 있게 한다."""

    async def pick() -> list[str]:
        engine = make_engine(url)
        async with engine.begin() as conn:
            rows = await conn.execute(
                text("SELECT code FROM regions WHERE level = 'dong' ORDER BY code LIMIT :n"), {"n": n}
            )
            codes = [r.code for r in rows]
            if with_centroid:
                await conn.execute(
                    text("UPDATE regions SET centroid = ST_SetSRID(ST_MakePoint(129.1, 35.1), 4326) WHERE code = :c"),
                    {"c": codes[0]},
                )
        await engine.dispose()
        return codes

    codes = asyncio.run(pick())
    assert len(codes) == n
    return codes


def test_region_interest_is_idempotent_and_listed(interest_db):
    [code] = dong_codes(interest_db, 1, with_centroid=True)
    with client(interest_db) as c:
        first = c.put(f"{API}/interests/regions/{code}")
        again = c.put(f"{API}/interests/regions/{code}")
        assert first.status_code == again.status_code == 200
        assert first.json()["data"] == again.json()["data"]  # 다시 넣어도 처음 등록 시각 그대로
        assert first.json()["data"]["target_id"] == code

        r = c.get(f"{API}/interests/regions")
        ids = c.get(f"{API}/interests").json()["data"]
    assert r.status_code == 200
    assert r.headers["cache-control"] == "private, no-cache"
    body = r.json()
    assert body["meta"] == {"total": 1, "max_items": 10}
    [item] = body["data"]
    assert set(item) == {"code", "name", "sigungu_name", "lat", "lng", "added_at"}
    assert item["code"] == code and item["name"] and item["sigungu_name"]
    assert (item["lat"], item["lng"]) == (pytest.approx(35.1), pytest.approx(129.1))
    assert ids["regions"] == [code]


def test_region_limit_is_10(interest_db):
    codes = dong_codes(interest_db, 11)
    with client(interest_db) as c:
        for code in codes[:10]:
            assert c.put(f"{API}/interests/regions/{code}").status_code == 200
        r = c.put(f"{API}/interests/regions/{codes[10]}")
        assert (r.status_code, r.json()["error"]["code"]) == (409, "CONFLICT")
        assert "10개" in r.json()["error"]["message"]
        assert c.put(f"{API}/interests/regions/{codes[0]}").status_code == 200  # 이미 있는 건 한도와 상관없다


@pytest.mark.parametrize("code", ["2699999999", "26230"])  # 없는 동, 시군구(동만 등록할 수 있다)
def test_unknown_or_sigungu_region_is_404(interest_db, code):
    with client(interest_db) as c:
        r = c.put(f"{API}/interests/regions/{code}")
        assert (r.status_code, r.json()["error"]["code"]) == (404, "RESOURCE_NOT_FOUND")
        assert c.get(f"{API}/interests").json()["data"]["regions"] == []


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("PUT", "/interests/regions/2635010500"),
        ("DELETE", "/interests/regions/2635010500"),
        ("GET", "/interests/regions"),
    ],
)
def test_region_needs_login(interest_db, method, path):
    with TestClient(app_for(interest_db)) as c:
        r = c.request(method, f"{API}{path}")
    assert (r.status_code, r.json()["error"]["code"]) == (401, "UNAUTHORIZED")

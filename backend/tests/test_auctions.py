"""경매·공매 동기화: 개인정보를 버리는지, 상세를 아껴 받는지, 진행 목록에서 빠진 물건을 닫는지 본다."""

import json
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import text

from app.adapters.madangs import MadangsClient, court_from_list, court_with_detail
from app.adapters.molit.types import QuotaExceeded, SourceAuthError
from app.db import migrate
from app.db.engine import make_engine
from app.ingestion.auctions import AUCTION_LOCK_KEY, geocode_query, sync_auctions
from app.ingestion.geocode import GeocodeCache
from app.ingestion.load import LoadBusy
from app.repositories.ports import SourceUnavailable
from tests.conftest import FIXED_NOW, create_database
from tests.fakes.madangs import PERSONAL, FakeAuctionSource, FakeLookup, court_detail, court_row, public_row

pytestmark = pytest.mark.anyio


# ── 어댑터: 허용한 칸만 옮긴다 ─────────────────────────────────


def test_detail_keeps_numbers_and_drops_every_personal_field():
    row = court_row(1)
    item = court_with_detail(court_from_list(row), court_detail(row))
    assert item.jibun_address == "부산 해운대구 우동 601"
    assert (item.deposit, item.building_area_m2, item.min_price) == (35_000_000, 84.97, 350_000_001)
    assert [s["result"] for s in item.schedule] == ["유찰", "진행"]
    assert item.results == ({"date": "2026-09-01", "result": "유찰", "bidders": 0, "top_price": None},)
    dumped = repr(item)
    for name in PERSONAL:
        assert name not in dumped
    assert "400000000" not in dumped  # 청구액(채무 정보)도 옮기지 않는다


def test_geocode_query_prefers_jibun_and_strips_unit_from_road_address():
    row = court_row(1)
    assert geocode_query(court_from_list(row)) == "부산광역시 해운대구 해운대로 601"
    assert geocode_query(court_with_detail(court_from_list(row), court_detail(row))) == "부산 해운대구 우동 601"


def client_for(handler) -> tuple[MadangsClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(record))
    return MadangsClient("test-key", http=http, sleep=_no_sleep), seen


async def _no_sleep(_):
    return None


def ok(body: dict) -> httpx.Response:
    return httpx.Response(200, json={"header": {"resultCode": "00", "resultMsg": "정상"}, "body": body})


async def test_list_follows_pages_and_sends_key_and_region():
    def handler(request):
        page = int(request.url.params["pageNo"])
        return ok({"items": [court_row(page)], "hasNext": page < 3})

    client, seen = client_for(handler)
    items = await client.court_list("26")
    assert [i.case_no for i in items] == ["2025타경1001", "2025타경1002", "2025타경1003"]
    assert {r.url.params["ServiceKey"] for r in seen} == {"test-key"}
    assert {r.url.params["addr"] for r in seen} == {"26"}
    assert seen[0].url.params["numOfRows"] == "100"
    assert client.calls_made == 3


async def test_public_list_keeps_only_sales():
    client, _ = client_for(lambda r: ok({"items": [public_row(1), public_row(2, 처분방식="임대")], "hasNext": False}))
    items = await client.public_list("26")
    assert [i.case_no for i in items] == ["2026-1000-000001"]
    assert items[0].sale_date.isoformat() == "2026-11-04" and items[0].bid_end_at.hour == 17


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (httpx.Response(429), QuotaExceeded),
        (httpx.Response(401), SourceAuthError),
        (httpx.Response(200, json={"header": {"resultCode": "99", "resultMsg": "x"}, "body": None}), SourceUnavailable),
        (httpx.Response(503), SourceUnavailable),
    ],
)
async def test_errors_are_mapped(response, error):
    client, _ = client_for(lambda r: response)
    with pytest.raises(error):
        await client.court_list("26")


# ── 동기화 (실제 PostGIS) ─────────────────────────────────────


@pytest.fixture(scope="module")
def auction_db(postgis: str) -> str:
    url = create_database(postgis, "auctions")
    migrate.upgrade(url)
    return url


@pytest.fixture
async def engine(auction_db):
    engine = make_engine(auction_db)
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE auctions, etl_runs RESTART IDENTITY CASCADE"))
    yield engine
    await engine.dispose()


@pytest.fixture
def cache(tmp_path) -> GeocodeCache:
    return GeocodeCache(tmp_path / "geocode.json")


async def sync(engine, source, cache, *, minutes=0, lookup=None, **kw):
    return await sync_auctions(
        engine,
        source,
        lookup or FakeLookup(),
        cache,
        now=FIXED_NOW + timedelta(minutes=minutes),
        out=lambda _: None,
        **kw,
    )


async def fetch(engine, sql: str, **params):
    async with engine.connect() as conn:
        return [tuple(r) for r in (await conn.execute(text(sql), params)).all()]


async def test_first_sync_stores_everything_with_locations_and_no_personal_data(engine, cache):
    source = FakeAuctionSource([court_row(1), court_row(2)], [public_row(1)])
    report = await sync(engine, source, cache)
    assert (report.status, report.inserted, report.details_fetched) == ("success", 3, 2)
    rows = await fetch(
        engine,
        "SELECT id, source, pnu IS NOT NULL, location IS NOT NULL, jibun_address, deposit FROM auctions ORDER BY id",
    )
    assert rows == [
        ("CA-41-2025-1001-1", "court", True, True, "부산 해운대구 우동 601", 35_000_000),
        ("CA-41-2025-1002-1", "court", True, True, "부산 해운대구 우동 602", 35_000_000),
        ("GO-2026-1000-000001", "public", True, True, "부산 수영구 광안동 101", None),
    ]
    everything = json.dumps(await fetch(engine, "SELECT row_to_json(a)::text FROM auctions a"), ensure_ascii=False)
    for name in PERSONAL:
        assert name not in everything
    [run] = await fetch(engine, "SELECT job, status, rows_inserted FROM etl_runs")
    assert run == ("auction", "success", 3)


async def test_unchanged_items_do_not_spend_detail_calls(engine, cache):
    source = FakeAuctionSource([court_row(1), court_row(2)], [])
    await sync(engine, source, cache)
    lookup = FakeLookup()
    again = await sync(engine, source, cache, minutes=1, lookup=lookup)
    assert len(source.detail_calls) == 2  # 처음 2건뿐
    assert (again.inserted, again.updated, again.details_fetched) == (0, 2, 0)
    assert lookup.queries == []  # 지오코딩도 캐시에서
    [(schedule,)] = await fetch(
        engine, "SELECT jsonb_array_length(schedule) FROM auctions WHERE id = 'CA-41-2025-1001-1'"
    )
    assert schedule == 2  # 상세 없이 갱신해도 기일 이력이 지워지지 않는다


async def test_changed_list_values_refetch_the_detail(engine, cache):
    source = FakeAuctionSource([court_row(1), court_row(2)], [])
    await sync(engine, source, cache)
    source.court[0] = court_row(1, 유찰횟수=2, 최저가=245_000_000)
    await sync(engine, source, cache, minutes=1)
    assert source.detail_calls[2:] == ["CA-41-2025-1001-1"]
    [(price, fails)] = await fetch(engine, "SELECT min_price, fail_count FROM auctions WHERE id = 'CA-41-2025-1001-1'")
    assert (price, fails) == (245_000_000, 2)


async def test_items_gone_from_the_list_are_closed_and_reopen_when_back(engine, cache):
    source = FakeAuctionSource([court_row(1), court_row(2)], [public_row(1)])
    await sync(engine, source, cache)
    source.court, source.public = [court_row(1)], []
    report = await sync(engine, source, cache, minutes=1)
    assert report.closed == 2
    assert await fetch(engine, "SELECT id FROM auctions WHERE closed_at IS NULL") == [("CA-41-2025-1001-1",)]
    source.court = [court_row(1), court_row(2)]
    await sync(engine, source, cache, minutes=2)
    assert await fetch(engine, "SELECT count(*) FROM auctions WHERE closed_at IS NULL") == [(2,)]


async def test_a_failed_list_does_not_close_that_source(engine, cache):
    source = FakeAuctionSource([court_row(1)], [public_row(1)])
    await sync(engine, source, cache)
    source.court_error = SourceUnavailable("경매마당 서버 오류(HTTP 503)")
    report = await sync(engine, source, cache, minutes=1)
    assert report.status == "partial" and report.closed == 0
    assert await fetch(engine, "SELECT count(*) FROM auctions WHERE closed_at IS NULL") == [(2,)]


async def test_details_cut_by_quota_are_fetched_next_time(engine, cache):
    source = FakeAuctionSource([court_row(n) for n in range(1, 6)], [])
    source.quota_after = 2
    first = await sync(engine, source, cache)
    assert first.status == "quota_exceeded"
    assert (first.inserted, first.details_fetched, first.details_pending) == (5, 2, 3)
    source.quota_after = None
    second = await sync(engine, source, cache, minutes=1)
    assert (second.status, second.details_fetched) == ("success", 3)
    assert sorted(source.detail_calls) == sorted(r["product_id"] for r in source.court)
    assert await fetch(engine, "SELECT count(*) FROM auctions WHERE jibun_address IS NULL") == [(0,)]


async def test_geocodes_outside_busan_are_dropped(engine, cache):
    source = FakeAuctionSource([court_row(1, 대표소재지명="서울특별시 구로구 개봉로 1")], [])
    source.court_detail = _no_detail(source)
    report = await sync(engine, source, cache)
    assert report.without_location == 1
    assert await fetch(engine, "SELECT location IS NULL, pnu IS NULL FROM auctions") == [(True, True)]


def _no_detail(source):
    async def detail(item):
        source.calls_made += 1
        return item

    return detail


async def test_a_second_sync_refuses_to_run_while_one_is_in_progress(engine, cache):
    async with engine.connect() as holder:
        await holder.execute(text("SELECT pg_advisory_lock(:k)"), {"k": AUCTION_LOCK_KEY})
        with pytest.raises(LoadBusy):
            await sync(engine, FakeAuctionSource([], []), cache)
        await holder.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": AUCTION_LOCK_KEY})
    assert await fetch(engine, "SELECT count(*) FROM etl_runs") == [(0,)]

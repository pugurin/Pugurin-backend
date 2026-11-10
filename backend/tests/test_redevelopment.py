"""정비사업 구역 동기화: 원천 응답 해석(유형·단계·위치·숫자), 페이지 넘김, 사라진 구역 처리."""

from datetime import timedelta

import httpx
import pytest
from sqlalchemy import text

from app.adapters.kakao import GeoResult
from app.adapters.molit.types import QuotaExceeded, SourceAuthError
from app.adapters.redevelopment import (
    BusanRedevelopmentClient,
    classify_kind,
    classify_location,
    normalize_stage,
    parse_number,
    parse_page,
    zone_from_row,
)
from app.db import migrate
from app.db.engine import make_engine
from app.ingestion.geocode import GeocodeCache
from app.ingestion.load import LoadBusy
from app.ingestion.redevelopment import REDEVELOPMENT_LOCK_KEY, sync_redevelopments
from app.repositories.ports import SourceUnavailable
from tests.conftest import FIXED_NOW, create_database
from tests.fakes.madangs import FakeLookup
from tests.fakes.redevelopment import FakeRedevelopmentSource, page_body, zone_row

pytestmark = pytest.mark.anyio


# ── 원천 해석 ───────────────────────────────────────────────


def test_a_row_keeps_the_code_numbers_and_images_and_drops_blanks():
    z = zone_from_row(zone_row(1))
    assert (z.id, z.area_name, z.kind, z.stage, z.stage_order) == (
        "BARA_0000000000001",
        "가상1구역 재개발",
        "redevelopment",
        "association",
        4,
    )
    assert z.step_raw == "조합설립인가"
    assert (z.area_m2, z.households, z.buildings, z.union_members) == (45123.5, 1234, 12, 800)
    assert (z.floor_area_ratio, z.building_coverage_ratio, z.scale) == (299.9, 19.5, "지하3층지상32층")
    assert (z.business_entity, z.contractor, z.engineer, z.architect) == (
        "가상1구역 재개발정비사업조합",
        "가상건설",
        None,
        None,
    )
    assert z.tel == "051-000-0000"  # phone(휴대전화일 수 있다)은 옮기지 않는다
    assert "010-0000-0000" not in repr(z)
    # 원천은 http로 주지만 http는 401이고 https만 열린다(2026-11 확인). https로 바꿔 저장한다
    assert z.images == {
        "view": "https://dynamice.busan.go.kr/data/busi_ara/BARA_20260001_view.jpg",
        "location": "https://dynamice.busan.go.kr/data/busi_ara/BARA_20260001_loct.jpg",
    }
    assert (z.location_kind, z.geocode_query) == ("lot", "부산광역시 부산진구 가야동 101")


def test_undecided_text_fields_are_null():
    z = zone_from_row(zone_row(1, contractor="미정"))
    assert z.contractor is None


def test_a_row_without_a_code_gets_a_stable_id_from_the_normalized_name():
    a = zone_from_row(zone_row(1, aCode="", areaName=" 가상1구역  재개발 "))
    b = zone_from_row(zone_row(2, aCode=None, areaName="가상1구역 재개발"))
    assert a.id == b.id and a.id.startswith("NOCODE_") and len(a.id) == len("NOCODE_") + 16


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1,234", 1234.0),
        ("45123.5㎡", 45123.5),
        ("299.9%", 299.9),
        ("800명", 800.0),
        ("1,020세대", 1020.0),
        ("12동", 12.0),
        (" 7 ", 7.0),
        ("미선정", None),
        ("", None),
        ("-", None),
        (None, None),
        ("약 300", None),
        ("300~400", None),
        ("미정", None),
        # 실데이터: 괄호 설명이나 "이하"가 붙으면 앞의 숫자를 읽는다
        ("1,174(임대 60)", 1174.0),
        ("998세대(임대50)", 998.0),
        ("261%이하", 261.0),
    ],
)
def test_numbers_are_read_or_left_null(value, expected):
    assert parse_number(value) == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("가상1구역 재개발", "redevelopment"),
        ("가상아파트 재건축", "reconstruction"),
        ("가상맨션 소규모재건축", "small_reconstruction"),
        ("가상동 소규모재개발", "small_redevelopment"),
        ("가상빌라 가로주택정비사업", "street_housing"),
        ("가상지구 주거환경개선", "residential_improvement"),
        ("가상지구 도시환경정비", "urban_environment"),
        ("가상구역", "other"),
    ],
)
def test_kind_comes_from_the_area_name_longest_word_first(name, expected):
    assert classify_kind(name) == expected


@pytest.mark.parametrize(
    ("step", "stage", "order"),
    [
        ("해제", "cancelled", 0),
        ("정비구역 해제", "cancelled", 0),
        ("정비예정구역", "planned", 1),
        ("기본계획 수립", "planned", 1),
        ("정비계획 수립 및 정비구역 지정", "designated", 2),
        ("추진위원회 구성", "committee", 3),
        ("조합설립인가", "association", 4),
        ("건축심의 및 통합심의", "review", 5),
        ("사업시행계획인가", "project_approved", 6),
        ("관리처분계획", "disposal_approved", 7),
        ("착공", "construction", 8),
        ("이주 및 철거", "construction", 8),
        ("준공", "completed", 9),
        ("이전고시", "transferred", 10),
        ("조합해산", "dissolved", 11),
        ("청산", "dissolved", 11),
    ],
)
def test_steps_map_to_ordered_stages(step, stage, order):
    assert normalize_stage(step) == (stage, order)


@pytest.mark.parametrize("step", ["안전진단", "", None, "미선정"])
def test_unknown_or_blank_steps_are_unknown_without_order(step):
    assert normalize_stage(step) == ("unknown", None)


@pytest.mark.parametrize(
    ("text", "kind", "query"),
    [
        ("가야동 410번지 일원(가야초교 남측 일원)", "lot", "부산광역시 가야동 410"),
        ("부산진구 가야동 122-2", "lot", "부산광역시 부산진구 가야동 122-2"),
        ("사하구 감천1동 202일원", "lot", "부산광역시 사하구 감천1동 202"),
        ("중구 영주동 산 10-3 일대", "lot", "부산광역시 중구 영주동 산 10-3"),
        ("동구 범일동 830-1번지, 830-2번지", "lot", "부산광역시 동구 범일동 830-1"),
        ("부산광역시 남구 용호동 123", "lot", "부산광역시 남구 용호동 123"),  # '호'가 들어간 동 이름
        ("중앙동4가 33", "lot", "부산광역시 중앙동4가 33"),
        # 실데이터에서 본 모양
        ("부산시 동구 범일동 330-190번지 일원", "lot", "부산광역시 동구 범일동 330-190"),
        ("대평동 1가 1번지 일원", "lot", "부산광역시 대평동1가 1"),
        ("동래구 칠산동246번지 일원", "lot", "부산광역시 동래구 칠산동 246"),
        ("부산광역시 해운대구 중동 785-8번지, 1층", "office", None),
        ("부산광역시 부산진구 냉정로 266, 2층(가야동)", "office", None),
        ("연제구 법원로32번길 15,502호", "office", None),
        ("우암로 134, 3층 (감만동, 삼진빌딩)", "office", None),
        ("가야동 12 가상상가 2동", "office", None),
        ("부산진구 가야동 일원", "none", None),
        ("", "none", None),
        (None, "none", None),
    ],
)
def test_location_text_is_sorted_into_lot_office_or_none(text, kind, query):
    assert classify_location(text) == (kind, query)


# ── 응답 감싸기 ─────────────────────────────────────────────


def test_page_accepts_a_list_a_single_object_or_no_items():
    rows, total = parse_page(page_body([zone_row(1), zone_row(2)], 351))
    assert (len(rows), total) == (2, 351)
    rows, total = parse_page(page_body(zone_row(3), 1))
    assert [r["aCode"] for r in rows] == ["BARA_0000000000003"] and total == 1
    assert parse_page(page_body("", 0)) == ([], 0)
    assert parse_page(page_body(None, 0)) == ([], 0)
    assert parse_page(page_body([], 0)) == ([], 0)


@pytest.mark.parametrize("total", [None, "", "모름"])
def test_rows_without_a_readable_total_are_refused(total):
    # totalCount를 못 읽으면 1쪽만 받고 나머지 구역을 닫게 되므로 실패로 본다
    data = page_body([zone_row(1)])
    data["response"]["body"]["totalCount"] = total
    with pytest.raises(SourceUnavailable):
        parse_page(data)


def test_page_reads_a_top_level_header_too():
    data = {"header": {"resultCode": "00"}, "body": {"totalCount": 1, "items": [zone_row(1)]}}
    rows, total = parse_page(data)
    assert (len(rows), total) == (1, 1)


@pytest.mark.parametrize(
    ("data", "error"),
    [
        (page_body([], code="30", msg="SERVICE_KEY_IS_NOT_REGISTERED_ERROR"), SourceAuthError),
        (page_body([], code="22", msg="LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR"), QuotaExceeded),
        (page_body([], code="99", msg="UNKNOWN_ERROR"), SourceUnavailable),
        ({"resultCode": "10", "resultMsg": "INVALID_REQUEST_PARAMETER_ERROR"}, SourceUnavailable),
        ({"something": "else"}, SourceUnavailable),
    ],
)
def test_error_codes_are_mapped(data, error):
    with pytest.raises(error):
        parse_page(data)


def test_no_data_code_is_an_empty_page():
    assert parse_page(page_body("", 0, code="03", msg="NODATA_ERROR")) == ([], 0)


# ── HTTP ───────────────────────────────────────────────────


async def _no_sleep(_):
    return None


def client_for(handler) -> tuple[BusanRedevelopmentClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(record))
    return BusanRedevelopmentClient("k", http=http, sleep=_no_sleep), seen


async def test_client_pages_until_the_total_with_string_counts():
    def handler(request):
        page = int(request.url.params["pageNo"])
        items = [zone_row(n) for n in range((page - 1) * 100 + 1, min(page * 100, 250) + 1)]
        return httpx.Response(200, json=page_body(items, "250"))

    client, seen = client_for(handler)
    zones = await client.zones()
    assert len(zones) == 250 and zones[-1].id == "BARA_0000000000250"
    assert [r.url.params["pageNo"] for r in seen] == ["1", "2", "3"]
    first = seen[0].url
    assert first.path.endswith("/6260000/MaintenanceBusinessStatus1/getMaintenanceBusiness1")
    assert (first.params["serviceKey"], first.params["numOfRows"], first.params["resultType"]) == ("k", "100", "json")
    assert client.calls_made == 3


async def test_client_stops_on_an_empty_page_even_if_the_total_says_more():
    client, seen = client_for(lambda r: httpx.Response(200, json=page_body("", 10)))
    assert await client.zones() == []
    assert len(seen) == 1


async def test_client_refuses_a_list_that_ends_before_the_total():
    # 둘째 쪽이 비어 버리면 나머지 구역이 닫히지 않게 실패로 본다
    def handler(request):
        page = int(request.url.params["pageNo"])
        items = [zone_row(n) for n in range(1, 101)] if page == 1 else ""
        return httpx.Response(200, json=page_body(items, "351"))

    client, _ = client_for(handler)
    with pytest.raises(SourceUnavailable):
        await client.zones()


async def test_client_counts_unique_codes_so_a_repeated_row_does_not_end_the_list_early():
    # 둘째 쪽 첫 행이 첫 쪽 끝과 겹치면 행 수로는 200에 닿지만 고유 구역은 199곳뿐이다
    def handler(request):
        page = int(request.url.params["pageNo"])
        starts = {1: 1, 2: 100}
        items = [zone_row(n) for n in range(starts[page], starts[page] + 100)] if page in starts else ""
        return httpx.Response(200, json=page_body(items, "200"))

    client, seen = client_for(handler)
    with pytest.raises(SourceUnavailable):
        await client.zones()
    assert len(seen) == 3


async def test_client_maps_key_errors_returned_as_xml():
    xml = "<OpenAPI_ServiceResponse><returnAuthMsg>SERVICE_KEY_IS_NOT_REGISTERED_ERROR</returnAuthMsg>"
    client, _ = client_for(lambda r: httpx.Response(200, text=xml))
    with pytest.raises(SourceAuthError):
        await client.zones()


async def test_client_retries_server_errors_then_gives_up():
    client, seen = client_for(lambda r: httpx.Response(503))
    with pytest.raises(SourceUnavailable):
        await client.zones()
    assert len(seen) == 3


# ── 동기화 (실제 PostGIS) ─────────────────────────────────────

OFFICE = "부산광역시 부산진구 가상로 266, 2층(가야동)"


@pytest.fixture(scope="module")
def redevelopment_db(postgis: str) -> str:
    url = create_database(postgis, "redevelopments")
    migrate.upgrade(url)
    return url


@pytest.fixture
async def engine(redevelopment_db):
    engine = make_engine(redevelopment_db)
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE redevelopments, etl_runs RESTART IDENTITY CASCADE"))
    yield engine
    await engine.dispose()


@pytest.fixture
def cache(tmp_path) -> GeocodeCache:
    return GeocodeCache(tmp_path / "geocode.json")


async def sync(engine, source, cache, *, minutes=0, lookup=None):
    return await sync_redevelopments(
        engine, source, lookup or FakeLookup(), cache, now=FIXED_NOW + timedelta(minutes=minutes), out=lambda _: None
    )


async def fetch(engine, sql: str):
    async with engine.connect() as conn:
        return [tuple(r) for r in (await conn.execute(text(sql))).all()]


async def test_first_sync_stores_zones_and_geocodes_only_lot_addresses(engine, cache):
    rows = [zone_row(1), zone_row(2, location=OFFICE, step="착공"), zone_row(3, location="", step="준공")]
    lookup = FakeLookup()
    report = await sync(engine, FakeRedevelopmentSource(rows), cache, lookup=lookup)
    assert (report.status, report.fetched, report.inserted, report.closed) == ("success", 3, 3, 0)
    assert (report.lot, report.office, report.none, report.without_location) == (1, 1, 1, 2)
    assert lookup.queries == ["부산광역시 부산진구 가야동 101"]
    got = await fetch(
        engine,
        "SELECT id, kind, stage, stage_order, location_kind, location_precise, round(lat::numeric, 5)::float8, "
        "geom IS NOT NULL FROM redevelopments ORDER BY id",
    )
    assert got == [
        ("BARA_0000000000001", "redevelopment", "association", 4, "lot", True, 35.16101, True),
        ("BARA_0000000000002", "redevelopment", "construction", 8, "office", False, None, False),
        ("BARA_0000000000003", "redevelopment", "completed", 9, "none", False, None, False),
    ]
    [(images, households, engineer, area)] = await fetch(
        engine, "SELECT images, households, engineer, area_m2 FROM redevelopments WHERE id = 'BARA_0000000000001'"
    )
    assert set(images) == {"view", "location"} and (households, engineer, area) == (1234, None, 45123.5)
    [(job, status, report_json)] = await fetch(engine, "SELECT job, status, report FROM etl_runs")
    assert (job, status) == ("redevelopment", "success")
    assert report_json["location_kinds"] == {"lot": 1, "office": 1, "none": 1}


async def test_test_rows_are_skipped_and_counted(engine, cache):
    rows = [zone_row(1), zone_row(2, areaName="교리테스트 재개발"), zone_row(3, areaName="가상 테스트구역")]
    report = await sync(engine, FakeRedevelopmentSource(rows), cache)
    assert (report.fetched, report.skipped_test) == (1, 2)
    assert await fetch(engine, "SELECT id FROM redevelopments") == [("BARA_0000000000001",)]


async def test_a_repeated_id_keeps_the_first_row_and_warns(engine, cache):
    rows = [zone_row(1), zone_row(1, areaName="가상1구역 재건축"), zone_row(2)]
    report = await sync(engine, FakeRedevelopmentSource(rows), cache)
    assert report.duplicates == ["BARA_0000000000001"] and report.fetched == 2
    assert await fetch(engine, "SELECT kind FROM redevelopments WHERE id = 'BARA_0000000000001'") == [
        ("redevelopment",)
    ]
    [(status, report_json)] = await fetch(engine, "SELECT status, report FROM etl_runs")
    assert status == "success" and report_json["duplicates"] == ["BARA_0000000000001"]


async def test_unknown_steps_are_kept_raw_and_listed_in_the_report(engine, cache):
    rows = [zone_row(1, step="안전진단 통과"), zone_row(2, step="안전진단 통과"), zone_row(3, step="미선정")]
    report = await sync(engine, FakeRedevelopmentSource(rows), cache)
    assert report.unknown_steps == {"안전진단 통과": 2}
    assert await fetch(engine, "SELECT step_raw, stage, stage_order FROM redevelopments ORDER BY id") == [
        ("안전진단 통과", "unknown", None),
        ("안전진단 통과", "unknown", None),
        (None, "unknown", None),
    ]


async def test_second_sync_uses_the_geocode_cache_closes_what_disappeared_and_reopens_returns(engine, cache):
    await sync(engine, FakeRedevelopmentSource([zone_row(1), zone_row(2)]), cache)
    lookup = FakeLookup()
    report = await sync(engine, FakeRedevelopmentSource([zone_row(1, step="착공")]), cache, minutes=1, lookup=lookup)
    assert lookup.queries == []
    assert (report.updated, report.closed) == (1, 1)
    assert await fetch(engine, "SELECT id, stage FROM redevelopments WHERE closed_at IS NULL") == [
        ("BARA_0000000000001", "construction")
    ]
    back = await sync(engine, FakeRedevelopmentSource([zone_row(1), zone_row(2)]), cache, minutes=2)
    assert (back.closed, back.inserted, back.updated) == (0, 0, 2)
    got = await fetch(engine, "SELECT first_seen_at, last_seen_at FROM redevelopments WHERE id = 'BARA_0000000000002'")
    assert got == [(FIXED_NOW, FIXED_NOW + timedelta(minutes=2))]
    assert await fetch(engine, "SELECT count(*) FROM redevelopments WHERE closed_at IS NULL") == [(2,)]


async def test_a_failed_fetch_neither_writes_nor_closes(engine, cache):
    source = FakeRedevelopmentSource([zone_row(1)])
    await sync(engine, source, cache)
    source.error = SourceUnavailable("정비사업 정보 서버 오류(HTTP 503)")
    report = await sync(engine, source, cache, minutes=1)
    assert (report.status, report.closed) == ("failed", 0)
    assert await fetch(engine, "SELECT count(*) FROM redevelopments WHERE closed_at IS NULL") == [(1,)]
    assert await fetch(engine, "SELECT status FROM etl_runs ORDER BY id") == [("success",), ("failed",)]


@pytest.mark.parametrize("rows", [[], [zone_row(9, areaName="교리테스트 재개발")]])
async def test_a_zero_result_run_closes_nothing_and_counts_as_failed(engine, cache, rows):
    await sync(engine, FakeRedevelopmentSource([zone_row(1), zone_row(2)]), cache)
    report = await sync(engine, FakeRedevelopmentSource(rows), cache, minutes=1)
    assert (report.status, report.closed) == ("failed", 0)
    assert any("0건" in e for e in report.errors)
    assert await fetch(engine, "SELECT count(*) FROM redevelopments WHERE closed_at IS NULL") == [(2,)]
    assert await fetch(engine, "SELECT status FROM etl_runs ORDER BY id") == [("success",), ("failed",)]


async def test_location_that_turns_into_an_office_address_drops_the_old_point(engine, cache):
    await sync(engine, FakeRedevelopmentSource([zone_row(1)]), cache)
    await sync(engine, FakeRedevelopmentSource([zone_row(1, location=OFFICE)]), cache, minutes=1)
    assert await fetch(engine, "SELECT location_kind, lat, geom IS NULL, location_precise FROM redevelopments") == [
        ("office", None, True, False)
    ]


class QuotaLookup:
    def __init__(self):
        self.queries: list[str] = []

    async def lookup(self, query: str):
        self.queries.append(query)
        raise QuotaExceeded("카카오 호출 한도 초과(HTTP 429)")


async def test_geocoder_quota_keeps_known_points_and_marks_the_run(engine, tmp_path):
    await sync(engine, FakeRedevelopmentSource([zone_row(1)]), GeocodeCache(tmp_path / "a.json"))
    lookup = QuotaLookup()
    rows = [zone_row(1), zone_row(2)]
    report = await sync(
        engine, FakeRedevelopmentSource(rows), GeocodeCache(tmp_path / "b.json"), minutes=1, lookup=lookup
    )
    assert report.status == "quota_exceeded" and len(lookup.queries) == 1  # 한도에 닿으면 더 부르지 않는다
    assert await fetch(engine, "SELECT id, lat IS NOT NULL FROM redevelopments ORDER BY id") == [
        ("BARA_0000000000001", True),
        ("BARA_0000000000002", False),
    ]


async def test_a_changed_location_that_could_not_be_geocoded_drops_the_old_point(engine, tmp_path):
    await sync(engine, FakeRedevelopmentSource([zone_row(1)]), GeocodeCache(tmp_path / "a.json"))
    moved = [zone_row(1, location="부산진구 가야동 999번지 일원")]
    await sync(
        engine, FakeRedevelopmentSource(moved), GeocodeCache(tmp_path / "b.json"), minutes=1, lookup=QuotaLookup()
    )
    assert await fetch(engine, "SELECT location, lat FROM redevelopments") == [("부산진구 가야동 999번지 일원", None)]


class DongCenterLookup:
    async def lookup(self, query: str):
        return GeoResult(35.15, 129.05, "2623010100", None, precise=False)


async def test_a_dong_center_result_is_not_precise_and_outside_busan_is_dropped(engine, cache):
    rows = [zone_row(1), zone_row(2, location="서울 가상동 12")]
    await sync(engine, FakeRedevelopmentSource(rows[:1]), cache, lookup=DongCenterLookup())
    report = await sync(engine, FakeRedevelopmentSource(rows), cache, minutes=1)
    assert report.without_location == 1
    assert await fetch(engine, "SELECT id, lat, location_precise FROM redevelopments ORDER BY id") == [
        ("BARA_0000000000001", 35.15, False),
        ("BARA_0000000000002", None, False),
    ]


async def test_a_second_sync_refuses_to_run_while_one_is_in_progress(engine, cache):
    async with engine.connect() as holder:
        await holder.execute(text("SELECT pg_advisory_lock(:k)"), {"k": REDEVELOPMENT_LOCK_KEY})
        with pytest.raises(LoadBusy):
            await sync(engine, FakeRedevelopmentSource([]), cache)
        await holder.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": REDEVELOPMENT_LOCK_KEY})
    assert await fetch(engine, "SELECT count(*) FROM etl_runs") == [(0,)]


# ── 명령·worker 일과 ──────────────────────────────────────────


async def test_the_command_skips_without_a_key(capsys):
    from app.core.config import Settings
    from app.ingestion.__main__ import _redevelopments

    assert await _redevelopments(Settings(redevelopment_api_key="")) == 2
    assert "PUGURIN_REDEVELOPMENT_API_KEY" in capsys.readouterr().out


@pytest.mark.parametrize(("key", "expected"), [("", []), ("k", ["redevelopments"])])
async def test_the_daily_routine_runs_the_sync_only_with_a_key(monkeypatch, key, expected):
    from app.core.config import Settings
    from app.ingestion import __main__ as cli

    calls: list[str] = []

    async def fake_refresh(settings, args):
        return 0

    async def fake_redevelopments(settings):
        calls.append("redevelopments")
        return 0

    monkeypatch.setattr(cli, "_refresh", fake_refresh)
    monkeypatch.setattr(cli, "_redevelopments", fake_redevelopments)
    settings = Settings(redevelopment_api_key=key, auction_api_key="", neis_api_key="", hospital_api_key="")
    assert await cli._daily(settings, None) == 0
    assert calls == expected

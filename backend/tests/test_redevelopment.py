"""정비사업 구역 동기화: 원천 응답 해석(유형·단계·위치·숫자), 페이지 넘김, 사라진 구역 처리."""

import httpx
import pytest

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
from app.repositories.ports import SourceUnavailable
from tests.fakes.redevelopment import page_body, zone_row

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
    assert z.images == {
        "view": "http://dynamice.busan.go.kr/data/busi_ara/BARA_20260001_view.jpg",
        "location": "http://dynamice.busan.go.kr/data/busi_ara/BARA_20260001_loct.jpg",
    }
    assert (z.location_kind, z.geocode_query) == ("lot", "부산광역시 부산진구 가야동 101")


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

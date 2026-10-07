import json

import httpx
import pytest

from app.adapters.kakao import GeoResult, KakaoGeocoder, make_pnu, parse_documents
from app.adapters.molit.types import QuotaExceeded, SourceAuthError
from app.ingestion.geocode import GeocodeCache, address_key, geocode_missing, region_key
from app.repositories.ports import SourceUnavailable

pytestmark = pytest.mark.anyio

ADDRESS_DOC = {
    "address_name": "부산 해운대구 우동 1407",
    "address_type": "REGION_ADDR",
    "x": "129.145059056791",
    "y": "35.1566107441159",
    "address": {"b_code": "2635010500", "main_address_no": "1407", "sub_address_no": "", "mountain_yn": "N"},
}
REGION_DOC = {
    "address_type": "REGION",
    "x": "129.148399576019",
    "y": "35.1727271517301",
    "address": {"b_code": "2635010500", "main_address_no": "", "sub_address_no": "", "mountain_yn": "N"},
}
REGION_WITHOUT_CODE = {"address_type": "REGION", "x": "129.1", "y": "35.1", "address": {"b_code": ""}}


def kakao(handler, **kwargs) -> tuple[KakaoGeocoder, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    async def no_sleep(_: float) -> None:
        return None

    http = httpx.AsyncClient(transport=httpx.MockTransport(wrapped))
    return KakaoGeocoder("rest-key", http=http, base_url="https://kakao.test", sleep=no_sleep, **kwargs), seen


def reply(*documents: dict) -> httpx.Response:
    return httpx.Response(200, json={"meta": {"total_count": len(documents)}, "documents": list(documents)})


def test_make_pnu_pads_main_and_sub_numbers():
    assert make_pnu("2635010500", "1407", "", False) == "2635010500114070000"
    assert make_pnu("2635010500", "939", "2", False) == "2635010500109390002"
    assert make_pnu("2635010500", "12", "3", True) == "2635010500200120003"
    assert make_pnu("2635010500", "", "", False) is None
    assert make_pnu("123", "1", "", False) is None


def test_parse_documents_prefers_address_match_and_keeps_pnu():
    result = parse_documents([ADDRESS_DOC], want_address=True)
    assert result == GeoResult(35.1566107441159, 129.145059056791, "2635010500", "2635010500114070000", True)


def test_parse_documents_falls_back_to_region_without_pnu():
    result = parse_documents([REGION_DOC], want_address=True)
    assert result and result.precise is False and result.pnu is None and result.b_code == "2635010500"


def test_parse_documents_skips_regions_without_a_legal_dong_code():
    result = parse_documents([REGION_WITHOUT_CODE, REGION_DOC], want_address=False)
    assert result and result.b_code == "2635010500"
    assert parse_documents([REGION_WITHOUT_CODE], want_address=False) is None
    assert parse_documents([], want_address=True) is None


async def test_address_query_and_auth_header():
    geocoder, seen = kakao(lambda _: reply(ADDRESS_DOC))
    result = await geocoder.address("해운대구", "우동", "1407")
    assert result and result.pnu == "2635010500114070000"
    request = seen[0]
    assert request.url.params["query"] == "부산 해운대구 우동 1407"
    assert request.headers["authorization"] == "KakaoAK rest-key"


async def test_region_query():
    geocoder, seen = kakao(lambda _: reply(REGION_DOC))
    result = await geocoder.region("기장군", "기장읍 교리")
    assert result and result.precise is False
    assert seen[0].url.params["query"] == "부산 기장군 기장읍 교리"


async def test_no_documents_means_none():
    geocoder, _ = kakao(lambda _: reply())
    assert await geocoder.address("해운대구", "우동", "99999") is None


async def test_auth_and_quota_errors():
    geocoder, _ = kakao(lambda _: httpx.Response(401, json={"errorType": "AccessDeniedError"}))
    with pytest.raises(SourceAuthError):
        await geocoder.address("해운대구", "우동", "1")
    geocoder, seen = kakao(lambda _: httpx.Response(429))
    with pytest.raises(QuotaExceeded):
        await geocoder.address("해운대구", "우동", "1")
    assert len(seen) == 1


async def test_server_errors_are_retried_then_reported():
    attempts = iter([httpx.Response(500), httpx.Response(502), reply(ADDRESS_DOC)])
    geocoder, seen = kakao(lambda _: next(attempts))
    assert await geocoder.address("해운대구", "우동", "1407")
    assert len(seen) == 3 and geocoder.calls_made == 3

    geocoder, seen = kakao(lambda _: httpx.Response(500), retries=1)
    with pytest.raises(SourceUnavailable):
        await geocoder.address("해운대구", "우동", "1")
    assert len(seen) == 2


async def test_bad_request_is_treated_as_not_found():
    geocoder, _ = kakao(lambda _: httpx.Response(400, json={"errorType": "InvalidArgument"}))
    assert await geocoder.address("해운대구", "우동", "") is None


def test_cache_round_trip_and_not_found_is_remembered(tmp_path):
    path = tmp_path / "geo.json"
    cache = GeocodeCache(path)
    hit = GeoResult(35.1, 129.1, "2635010500", "2635010500114070000", True)
    cache.put(address_key("해운대구", "우동", "1407"), hit)
    cache.put(address_key("해운대구", "우동", "0"), None)
    cache.save()

    again = GeocodeCache(path)
    assert again.get(address_key("해운대구", "우동", "1407")) == hit
    assert address_key("해운대구", "우동", "0") in again and again.get(address_key("해운대구", "우동", "0")) is None
    assert region_key("해운대구", "우동") not in again
    assert json.loads(path.read_text(encoding="utf-8"))[address_key("해운대구", "우동", "0")] == {"status": "not_found"}


class CountingGeocoder:
    def __init__(self, fail_on: int | None = None):
        self.addresses: list[tuple] = []
        self.regions: list[tuple] = []
        self.fail_on = fail_on

    async def address(self, sgg_name, umd_nm, jibun):
        self.addresses.append((sgg_name, umd_nm, jibun))
        if self.fail_on is not None and len(self.addresses) >= self.fail_on:
            raise QuotaExceeded("한도")
        return GeoResult(35.1, 129.1, "2635010500", "2635010500114070000", True)

    async def region(self, sgg_name, umd_nm):
        self.regions.append((sgg_name, umd_nm))
        return None


async def test_geocode_missing_skips_cached_and_deduplicates(tmp_path):
    cache = GeocodeCache(tmp_path / "geo.json")
    cache.put(address_key("해운대구", "우동", "1"), None)
    geocoder = CountingGeocoder()
    done = await geocode_missing(
        geocoder,
        cache,
        [("해운대구", "우동", "1"), ("해운대구", "우동", "2"), ("해운대구", "우동", "2")],
        [("해운대구", "우동"), ("해운대구", "우동")],
    )
    assert (
        done == 2 and geocoder.addresses == [("해운대구", "우동", "2")] and geocoder.regions == [("해운대구", "우동")]
    )
    assert (tmp_path / "geo.json").exists()


async def test_geocode_missing_saves_progress_when_quota_is_hit(tmp_path):
    cache = GeocodeCache(tmp_path / "geo.json")
    geocoder = CountingGeocoder(fail_on=3)
    with pytest.raises(QuotaExceeded):
        await geocode_missing(geocoder, cache, [("해운대구", "우동", str(i)) for i in range(10)], [], concurrency=1)
    saved = GeocodeCache(tmp_path / "geo.json")
    assert len(saved) == 2 and len(geocoder.addresses) == 3

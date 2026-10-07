from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient

from app.adapters.kakao import KakaoGeocoder
from app.adapters.molit.types import QuotaExceeded
from app.main import create_app
from app.repositories.memory.market import InMemoryMarketRepository
from app.repositories.ports import SourceUnavailable
from app.repositories.types import AddressHit
from tests.conftest import API, BUSAN, FIXED_NOW

HEADERS = {"X-Device-Id": "2f8f1d52-7b0e-4c55-9c0e-3f6f9d1c2a11"}


def search(client, q, **params):
    return client.get(f"{API}/search", params={"q": q, **params})


def data(client, q, **params):
    r = search(client, q, **params)
    assert r.status_code == 200, r.text
    return r.json()["data"]


class FakeAddressSearch:
    def __init__(self, hits=None, error: Exception | None = None):
        self.hits = (
            hits if hits is not None else [AddressHit("부산 해운대구 우동 1407", 35.15, 129.14, "2635010500114070000")]
        )
        self.error = error
        self.calls: list[tuple[str, int]] = []

    async def search_addresses(self, query, limit):
        self.calls.append((query, limit))
        if self.error:
            raise self.error
        return self.hits[:limit]


def app_with(address_search):
    return TestClient(create_app(clock=lambda: FIXED_NOW, address_search=address_search), headers=HEADERS)


def test_dong_name_finds_the_region_first(client):
    first = data(client, "우동")[0]
    assert first["type"] == "region" and first["region_level"] == "dong"
    assert first["name"] == "해운대구 우동" and first["region_code"] == "2635010500"
    assert set(first) == {"type", "region_level", "region_code", "name", "lat", "lng", "bbox"}
    min_lng, min_lat, max_lng, max_lat = first["bbox"]
    assert min_lng < max_lng and min_lat < max_lat
    assert min_lng <= first["lng"] <= max_lng and min_lat <= first["lat"] <= max_lat


def test_district_name_finds_the_sigungu(client):
    first = data(client, "해운대")[0]
    assert (first["type"], first["region_level"], first["name"], first["region_code"]) == (
        "region",
        "sigungu",
        "해운대구",
        "26350",
    )


@pytest.mark.parametrize("q", ["해운대구 우동", "해운대구우동", "해운대구 우"])
def test_district_plus_dong_finds_that_dong(client, q):
    first = data(client, q)[0]
    assert first["type"] == "region" and first["region_level"] == "dong" and first["region_code"] == "2635010500"


def test_sigungu_bbox_covers_its_dongs(client):
    by_code = {r["region_code"]: r for r in data(client, "해운대", limit=30) if r["type"] == "region"}
    sigungu = by_code["26350"]["bbox"]
    for dong in data(client, "우동"):
        if dong["type"] == "region" and dong["region_level"] == "dong":
            assert sigungu[0] <= dong["bbox"][0] and dong["bbox"][2] <= sigungu[2]
            assert sigungu[1] <= dong["bbox"][1] and dong["bbox"][3] <= sigungu[3]


def test_complex_by_name(client):
    first = data(client, "해운대아이파크")[0]
    assert first["type"] == "complex" and first["name"] == "해운대아이파크"
    assert set(first) == {"type", "complex_id", "name", "address", "lat", "lng"}
    UUID(first["complex_id"])
    assert first["address"].startswith("부산 해운대구")
    assert data(client, "해운대 아이파크")[0]["complex_id"] == first["complex_id"]


def test_complex_by_address_text(client):
    found = data(client, "해운대아이파크")[0]
    address = client.get(f"{API}/complexes/{found['complex_id']}").json()["data"]["address"]
    tail = " ".join(address.split()[-2:])
    assert found["complex_id"] in {r["complex_id"] for r in data(client, tail) if r["type"] == "complex"}


def test_exact_and_prefix_matches_come_before_partial_ones(client):
    results = data(client, "우동", limit=30)
    assert results[0]["type"] == "region"
    complexes = [r["name"] for r in results if r["type"] == "complex"]
    starts = [n.replace(" ", "").startswith("우동") for n in complexes]
    assert starts == sorted(starts, reverse=True)


def test_popular_complexes_come_first_among_equal_matches(client):
    results = [r for r in data(client, "해운대", limit=30) if r["type"] == "complex"]
    counts = [
        client.get(f"{API}/complexes/{r['complex_id']}/transactions", params={"page_size": 1}).json()["meta"]["total"]
        for r in results
        if r["name"].startswith("해운대")
    ]
    assert counts == sorted(counts, reverse=True)


def test_limit_default_and_bounds(client):
    assert len(data(client, "해운대", limit=3)) == 3
    assert len(data(client, "해운대")) <= 10
    assert len(data(client, "해운대", limit=30)) <= 30
    for bad in (0, 31, -1):
        r = search(client, "해운대", limit=bad)
        assert r.status_code == 400 and r.json()["error"]["field"] == "limit"


@pytest.mark.parametrize("q", ["", " ", "한", " 우 ", "a"])
def test_query_needs_two_characters(client, q):
    r = search(client, q)
    assert r.status_code == 400 and r.json()["error"]["field"] == "q"


def test_missing_query_is_400(client):
    r = client.get(f"{API}/search")
    assert r.status_code == 400 and r.json()["error"]["field"] == "q"


def test_no_match_is_an_empty_list(client):
    assert data(client, "zzzzqqqq") == []


def test_every_result_is_inside_busan(client):
    min_lng, min_lat, max_lng, max_lat = (float(v) for v in BUSAN.split(","))
    for q in ("해운대", "해운", "래미안", "푸르지오"):
        for r in data(client, q, limit=30):
            assert min_lng <= r["lng"] <= max_lng and min_lat <= r["lat"] <= max_lat


def test_search_response_is_cacheable(client):
    first = search(client, "우동")
    again = client.get(f"{API}/search", params={"q": "우동"}, headers={"If-None-Match": first.headers["etag"]})
    assert again.status_code == 304


def test_search_requires_a_device_id():
    with TestClient(create_app(clock=lambda: FIXED_NOW)) as c:
        assert c.get(f"{API}/search", params={"q": "우동"}).status_code == 400


# ---------- 주소 검색 ----------


def test_addresses_are_searched_only_when_the_query_has_a_number():
    fake = FakeAddressSearch()
    with app_with(fake) as c:
        assert not any(r["type"] == "address" for r in data(c, "우동"))
        assert fake.calls == []
        results = data(c, "우동 1407")
        assert fake.calls == [("우동 1407", 3)]
    address = next(r for r in results if r["type"] == "address")
    assert address == {
        "type": "address",
        "pnu": "2635010500114070000",
        "address": "부산 해운대구 우동 1407",
        "lat": 35.15,
        "lng": 129.14,
    }


def test_address_search_is_off_without_a_provider(client):
    assert not any(r["type"] == "address" for r in data(client, "우동 1407"))


@pytest.mark.parametrize("error", [QuotaExceeded("한도"), SourceUnavailable("장애")])
def test_address_failures_do_not_break_the_search(error):
    with app_with(FakeAddressSearch(error=error)) as c:
        results = data(c, "우동 1407")
        assert not any(r["type"] == "address" for r in results)


def test_an_address_that_is_already_a_complex_is_not_repeated(client):
    complex_hit = data(client, "해운대아이파크")[0]
    detail = client.get(f"{API}/complexes/{complex_hit['complex_id']}").json()["data"]
    tail = " ".join(detail["address"].split()[-2:])  # 지번이 들어 있어 주소 검색이 함께 호출되는 검색어
    fake = FakeAddressSearch([AddressHit(detail["address"], detail["lat"], detail["lng"], detail["pnu"])])
    with app_with(fake) as c:
        results = data(c, tail)
    assert fake.calls and any(r["type"] == "complex" and r["complex_id"] == detail["id"] for r in results)
    assert not any(r["type"] == "address" and r["pnu"] == detail["pnu"] for r in results)


def test_limit_applies_to_the_merged_list():
    fake = FakeAddressSearch([AddressHit(f"부산 해운대구 우동 {n}", 35.15, 129.14, None) for n in range(1, 4)])
    with app_with(fake) as c:
        assert len(data(c, "우동 1", limit=2)) == 2


# ---------- 카카오 어댑터 ----------


def kakao(handler):
    seen: list[httpx.Request] = []

    def wrapped(request):
        seen.append(request)
        return handler(request)

    async def no_sleep(_):
        return None

    http = httpx.AsyncClient(transport=httpx.MockTransport(wrapped))
    return KakaoGeocoder("k", http=http, base_url="https://kakao.test", sleep=no_sleep), seen


def doc(address_type, name, b_code, main="1407", sub="", mountain="N", x="129.14", y="35.15"):
    return {
        "address_type": address_type,
        "address_name": name,
        "x": x,
        "y": y,
        "address": {
            "address_name": name,
            "b_code": b_code,
            "main_address_no": main,
            "sub_address_no": sub,
            "mountain_yn": mountain,
        },
    }


@pytest.mark.anyio
async def test_kakao_search_keeps_only_busan_addresses_and_builds_pnu():
    documents = [
        doc("REGION_ADDR", "부산 해운대구 우동 1407", "2635010500"),
        doc("REGION_ADDR", "서울 종로구 청운동 1407", "1111010100"),
        doc("REGION", "부산 해운대구 우동", "2635010500", main=""),
        doc("ROAD_ADDR", "부산 해운대구 우동 939-2", "2635010500", main="939", sub="2"),
    ]
    geocoder, seen = kakao(lambda _: httpx.Response(200, json={"documents": documents}))
    hits = await geocoder.search_addresses("해운대구 우동 1407", 5)
    assert [h.address for h in hits] == ["부산 해운대구 우동 1407", "부산 해운대구 우동 939-2"]
    assert hits[0].pnu == "2635010500114070000" and hits[1].pnu == "2635010500109390002"
    assert seen[0].url.params["query"] == "부산 해운대구 우동 1407"


@pytest.mark.anyio
async def test_kakao_search_does_not_double_the_city_prefix_and_respects_the_limit():
    documents = [doc("REGION_ADDR", f"부산 해운대구 우동 {n}", "2635010500", main=str(n)) for n in range(1, 6)]
    geocoder, seen = kakao(lambda _: httpx.Response(200, json={"documents": documents}))
    assert len(await geocoder.search_addresses("부산 해운대구 우동 1", 2)) == 2
    assert seen[0].url.params["query"] == "부산 해운대구 우동 1"


# ---------- 저장소 ----------


@pytest.mark.anyio
async def test_region_boxes_enclose_their_complexes():
    repo = InMemoryMarketRepository(FIXED_NOW)
    dong = next(h for h in await repo.search_regions("우동", 5) if h.level == "dong")
    inside = [h.complex for h in await repo.search_complexes("우동", 50) if h.complex.region_code == dong.code]
    assert inside and all(
        dong.bbox[0] <= c.lng <= dong.bbox[2] and dong.bbox[1] <= c.lat <= dong.bbox[3] for c in inside
    )

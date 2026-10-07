import zlib
from dataclasses import replace
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.adapters.kakao import GeoResult, make_pnu
from app.adapters.molit.mock import FixtureTradeSource
from app.adapters.molit.types import QuotaExceeded, RawTrade, SourceAuthError, SourceKind
from app.core.busan import SIGUNGU
from app.core.config import Settings
from app.ingestion.build import BuildOptions, GeoIndex, build_dataset, needed_lookups
from app.ingestion.collect import collect, recent_months
from app.ingestion.geocode import GeocodeCache, geocode_missing
from app.ingestion.rawcache import RawCache
from app.ingestion.real import RealDataMissing, load_real_data
from app.ingestion.refresh import refresh
from app.main import create_app
from app.repositories.memory.market import InMemoryMarketRepository
from app.repositories.ports import SourceUnavailable
from app.repositories.types import DealType, LocationPrecision, PropertyType
from tests.conftest import API, FIXED_NOW

pytestmark = pytest.mark.anyio

DISTRICTS = ["26350", "26500"]
SGG_CODE = {s.name: s.code for s in SIGUNGU}
HEADERS = {"X-Device-Id": "2f8f1d52-7b0e-4c55-9c0e-3f6f9d1c2a11"}


class FakeGeocoder:
    """주소마다 결정적인 좌표·법정동 코드를 돌려주는 가짜 지오코더."""

    def __init__(self):
        self.calls = 0

    @staticmethod
    def _code(sgg_name: str, umd_nm: str) -> str:
        return SGG_CODE[sgg_name] + f"{zlib.crc32(umd_nm.encode()) % 90000 + 10000:05d}"

    @staticmethod
    def _xy(text: str) -> tuple[float, float]:
        h = zlib.crc32(text.encode())
        return 35.1 + (h % 1000) / 10000, 129.0 + ((h // 1000) % 1000) / 10000

    async def address(self, sgg_name, umd_nm, jibun):
        self.calls += 1
        lat, lng = self._xy(f"{sgg_name}{umd_nm}{jibun}")
        code = self._code(sgg_name, umd_nm)
        main, _, sub = jibun.partition("-")
        pnu = make_pnu(code, main, sub, False)
        return GeoResult(lat, lng, code, pnu, precise=pnu is not None)

    async def region(self, sgg_name, umd_nm):
        self.calls += 1
        lat, lng = self._xy(f"{sgg_name}{umd_nm}")
        return GeoResult(lat, lng, self._code(sgg_name, umd_nm) if umd_nm else None, None, precise=False)


async def fixture_trades() -> list[RawTrade]:
    source = FixtureTradeSource()
    return [t for k in SourceKind for g in DISTRICTS for t in await source.fetch_month(k, g, "202509")]


async def geo_for(trades, tmp_path) -> GeoIndex:
    cache = GeocodeCache(tmp_path / "geocode.json")
    addresses, regions = needed_lookups(trades)
    await geocode_missing(FakeGeocoder(), cache, addresses, regions)
    return GeoIndex(cache)


async def built(tmp_path, options=None):
    trades = await fixture_trades()
    return trades, *build_dataset(trades, await geo_for(trades, tmp_path), options)


# ---------- 수집 ----------


def test_recent_months_counts_back_across_years():
    assert recent_months(FIXED_NOW, 3) == ["202610", "202609", "202608"]
    assert recent_months(FIXED_NOW.replace(month=1, day=15), 3) == ["202601", "202512", "202511"]


async def test_raw_cache_round_trip(tmp_path):
    cache = RawCache(tmp_path)
    rows = await FixtureTradeSource().fetch_month(SourceKind.apt_trade, "26350", "202509")
    cache.save(SourceKind.apt_trade, "26350", "202509", rows, FIXED_NOW)

    assert cache.windows() == [(SourceKind.apt_trade, "26350", "202509")]
    assert cache.fetched_at(SourceKind.apt_trade, "26350", "202509") == FIXED_NOW
    assert cache.latest_fetch() == FIXED_NOW
    loaded = cache.load(SourceKind.apt_trade, "26350", "202509")
    assert [r.source_key for r in loaded] == [r.source_key for r in rows]
    assert loaded[0].is_cancelled == rows[0].is_cancelled and loaded[0].amount_man == rows[0].amount_man
    assert cache.fetched_at(SourceKind.apt_rent, "26350", "202509") is None
    assert RawCache(tmp_path / "none").windows() == [] and RawCache(tmp_path / "none").latest_fetch() is None


class CountingSource:
    def __init__(self, fail_at: int | None = None, error: Exception | None = None, always_fail: bool = False):
        self.calls: list[tuple] = []
        self.fail_at, self.error, self.always_fail = fail_at, error, always_fail
        self.calls_made = 0

    async def fetch_month(self, kind, lawd_cd, deal_ymd):
        self.calls.append((kind, lawd_cd, deal_ymd))
        if self.always_fail or (self.fail_at is not None and len(self.calls) == self.fail_at):
            raise self.error or SourceUnavailable("일시 오류")
        return await FixtureTradeSource().fetch_month(kind, lawd_cd, deal_ymd)


async def run_collect(source, cache, now=FIXED_NOW, months=("202509",), **kw):
    return await collect(source, cache, months=months, sgg_codes=DISTRICTS, kinds=list(SourceKind), now=now, **kw)


async def test_collect_saves_every_window_and_skips_fresh_ones(tmp_path):
    cache, source = RawCache(tmp_path), CountingSource()
    first = await run_collect(source, cache)
    assert first.fetched == 14 and first.skipped == 0 and first.rows > 100 and first.stopped_reason is None

    again = await run_collect(CountingSource(), cache, now=FIXED_NOW + timedelta(hours=1))
    assert again.fetched == 0 and again.skipped == 14


async def test_settled_old_months_are_never_refetched_but_recent_ones_are(tmp_path):
    cache = RawCache(tmp_path)
    await run_collect(CountingSource(), cache, months=("202509", "202610"))
    later = FIXED_NOW + timedelta(days=2)
    source = CountingSource()
    report = await run_collect(source, cache, now=later, months=("202509", "202610"))
    assert {c[2] for c in source.calls} == {"202610"} and report.fetched == 14 and report.skipped == 14


async def test_quota_stops_collection_and_lists_what_is_left(tmp_path):
    cache = RawCache(tmp_path)
    report = await run_collect(CountingSource(fail_at=3, error=QuotaExceeded("한도")), cache)
    assert report.fetched == 2 and len(report.remaining) == 12 and "한도" in report.stopped_reason
    resumed = await run_collect(CountingSource(), cache)
    assert resumed.fetched == 12 and resumed.skipped == 2


async def test_auth_errors_abort_immediately(tmp_path):
    source = CountingSource(fail_at=1, error=SourceAuthError("키"))
    with pytest.raises(SourceAuthError):
        await run_collect(source, RawCache(tmp_path))
    assert len(source.calls) == 1


async def test_isolated_failures_are_recorded_and_repeated_ones_stop_the_run(tmp_path):
    report = await run_collect(CountingSource(fail_at=2), RawCache(tmp_path))
    assert len(report.failed) == 1 and report.fetched == 13 and report.stopped_reason is None
    stopped = await run_collect(CountingSource(always_fail=True), RawCache(tmp_path / "x"))
    assert len(stopped.failed) == 5 and "연속" in stopped.stopped_reason and len(stopped.remaining) == 9


# ---------- 데이터셋 만들기 ----------


async def test_build_converts_units_and_keeps_rent_semantics(tmp_path):
    trades, dataset, _, report = await built(tmp_path)
    for tx in dataset.transactions:
        assert tx.region_code.startswith(("26350", "26500")) and len(tx.region_code) == 10
    sales = [tx for tx in dataset.transactions if tx.deal_type == DealType.sale]
    assert sales and all(tx.price % 10_000 == 0 and tx.deposit is None and tx.monthly_rent is None for tx in sales)
    jeonse = [tx for tx in dataset.transactions if tx.deal_type == DealType.jeonse]
    monthly = [tx for tx in dataset.transactions if tx.deal_type == DealType.monthly]
    assert jeonse and all(tx.price is None and tx.deposit and tx.monthly_rent is None for tx in jeonse)
    # 보증금 없이 월세만 내는 거래(보증금 0)도 실제로 있어 보증금은 None이 아니기만 하면 된다
    assert monthly and all(tx.deposit is not None and tx.monthly_rent for tx in monthly)
    rent_kinds = {SourceKind.apt_rent, SourceKind.offi_rent, SourceKind.rh_rent}
    assert all(tx.trade_method is None for tx in dataset.transactions if tx.deal_type != DealType.sale)
    first_apt = next(t for t in trades if t.kind == SourceKind.apt_trade)
    mine = next(tx for tx in dataset.transactions if tx.price == first_apt.amount_man * 10_000)
    assert mine.exclusive_area_m2 and mine.contract_date and mine.supply_area_pyeong is None
    assert report.rows_in == len(trades) and rent_kinds


async def test_build_excludes_share_deals_and_roads_from_land_only(tmp_path):
    trades, dataset, _, report = await built(tmp_path)
    land = [t for t in trades if t.kind == SourceKind.land_trade]
    shares = sum(t.is_share_deal for t in land)
    roads = sum((not t.is_share_deal) and t.jimok == "도로" for t in land)
    assert shares > 0 and report.excluded_share_deals == shares and report.excluded_road_land == roads
    kept = [tx for tx in dataset.transactions if tx.property_type == PropertyType.land]
    assert len(kept) == len(land) - shares - roads

    _, everything, _, none_excluded = await built(tmp_path, BuildOptions(False, False))
    assert none_excluded.excluded_share_deals == 0
    assert sum(tx.property_type == PropertyType.land for tx in everything.transactions) == len(land)


async def test_masked_land_sits_at_the_dong_centroid_without_a_parcel(tmp_path):
    _, dataset, regions, _ = await built(tmp_path)
    land = [tx for tx in dataset.transactions if tx.property_type == PropertyType.land]
    assert land
    for tx in land:
        assert tx.location_precision == LocationPrecision.dong and tx.pnu is None
        assert (tx.lat, tx.lng) == (regions[tx.region_code].lat, regions[tx.region_code].lng)
        assert tx.land_area_m2 and tx.exclusive_area_m2 is None and tx.complex_id is None


async def test_complexes_group_by_apartment_id_and_by_name_for_the_rest(tmp_path):
    trades, dataset, _, report = await built(tmp_path)
    apts = [c for c in dataset.complexes if c.property_type == PropertyType.apartment]
    seqs = {t.complex_seq for t in trades if t.kind in (SourceKind.apt_trade, SourceKind.apt_rent)}
    assert len(apts) == len(seqs)
    others = {
        (
            t.kind.property_type,
            t.sgg_cd,
            t.umd_nm,
            t.jibun,
            "".join(ch for ch in (t.name or "") if ch.isalnum()).lower(),
        )
        for t in trades
        if t.kind in (SourceKind.offi_trade, SourceKind.offi_rent, SourceKind.rh_trade, SourceKind.rh_rent)
    }
    assert len(dataset.complexes) == len(apts) + len(others) == report.complexes
    for c in dataset.complexes:
        assert c.household_count is None and c.area_types and all(a.supply_area_pyeong is None for a in c.area_types)
        assert c.pnu is None or len(c.pnu) == 19


async def test_area_types_merge_near_identical_areas(tmp_path):
    trades = await fixture_trades()
    base = next(t for t in trades if t.kind == SourceKind.apt_trade and t.exclusive_area_m2)
    near = replace(
        base, exclusive_area_m2=base.exclusive_area_m2 + 0.12, amount_man=(base.amount_man or 0) + 1, ordinal=7
    )
    far = replace(base, exclusive_area_m2=base.exclusive_area_m2 + 15, amount_man=(base.amount_man or 0) + 2, ordinal=8)
    dataset, _, _ = build_dataset([base, near, far], await geo_for([base], tmp_path))
    [complex_] = dataset.complexes
    assert len(complex_.area_types) == 2


async def test_ids_are_deterministic_and_unique(tmp_path):
    _, first, _, _ = await built(tmp_path)
    _, second, _, _ = await built(tmp_path)
    assert [t.id for t in first.transactions] == [t.id for t in second.transactions]
    assert len({t.id for t in first.transactions}) == len(first.transactions)
    assert [c.id for c in first.complexes] == [c.id for c in second.complexes]


async def test_cancelled_trades_carry_their_date(tmp_path):
    trades, dataset, _, _ = await built(tmp_path)
    cancelled = [tx for tx in dataset.transactions if tx.is_cancelled]
    assert len(cancelled) == sum(t.is_cancelled for t in trades if t.kind != SourceKind.land_trade)
    assert all(tx.cancelled_at and tx.cancelled_at.utcoffset() == timedelta(hours=9) for tx in cancelled)


async def test_without_geocoding_apartments_still_map_and_fall_back_to_the_dong_centre(tmp_path):
    trades = await fixture_trades()
    empty = GeoIndex(GeocodeCache(tmp_path / "empty.json"))
    dataset, regions, report = build_dataset(trades, empty)
    apt_rows = [t for t in trades if t.kind == SourceKind.apt_trade]
    mapped_apts = [tx for tx in dataset.transactions if tx.property_type == PropertyType.apartment]
    assert mapped_apts and report.unmapped_regions and report.unmapped_region_rows > 0
    assert report.complexes_without_exact_location == report.complexes
    assert all(tx.location_precision == LocationPrecision.dong for tx in dataset.transactions)
    assert all(tx.region_code in regions for tx in dataset.transactions) and apt_rows


# ---------- API로 내려보내기 ----------


async def served(tmp_path, **kwargs) -> TestClient:
    _, dataset, regions, _ = await built(tmp_path)
    repo = InMemoryMarketRepository(FIXED_NOW, dataset, regions)
    return TestClient(create_app(clock=lambda: FIXED_NOW, market_repo=repo, **kwargs), headers=HEADERS)


async def test_real_dataset_is_served_through_the_map_api(tmp_path):
    with await served(tmp_path) as client:
        region_markers = client.get(
            f"{API}/map/markers",
            params={"bbox": "128.75,34.85,129.35,35.40", "zoom": 11, "property_type": "apartment", "period_months": 24},
        ).json()["data"]["markers"]
        assert {m["name"] for m in region_markers} == {"해운대구", "수영구"}

        complex_markers = client.get(
            f"{API}/map/markers",
            params={"bbox": "128.75,34.85,129.35,35.40", "zoom": 15, "property_type": "apartment", "period_months": 24},
        ).json()["data"]["markers"]
        assert complex_markers and all(m["kind"] == "complex" for m in complex_markers)
        detail = client.get(f"{API}/complexes/{complex_markers[0]['complex_id']}").json()["data"]
        assert detail["household_count"] is None
        assert all(a["supply_area_pyeong"] is None for a in detail["area_types"])
        history = client.get(f"{API}/complexes/{detail['id']}/transactions", params={"deal_type": "jeonse"}).json()
        assert all(t["trade_method"] is None and t["monthly_rent"] is None for t in history["data"])


async def test_land_has_regional_aggregates_but_no_parcel_markers(tmp_path):
    with await served(tmp_path) as client:
        body = client.get(
            f"{API}/map/markers",
            params={"bbox": "128.75,34.85,129.35,35.40", "zoom": 15, "property_type": "land", "period_months": 24},
        ).json()["data"]
        assert body["markers"] and all(m["kind"] == "region" for m in body["markers"])


# ---------- 실데이터 모드 ----------


async def prepare_cache(tmp_path):
    cache = RawCache(tmp_path)
    await run_collect(CountingSource(), cache, months=("202509",))
    trades = [t for w in cache.windows() for t in cache.load(*w)]
    geo_cache = GeocodeCache(tmp_path / "geocode.json")
    addresses, regions = needed_lookups(trades)
    await geocode_missing(FakeGeocoder(), geo_cache, addresses, regions)


async def test_real_mode_loads_from_the_cache_and_hides_fake_parcels(tmp_path):
    await prepare_cache(tmp_path)
    settings = Settings(data_mode="real", data_dir=tmp_path)
    data = load_real_data(settings)
    assert data.report.rows_out > 100 and data.as_of == FIXED_NOW

    with TestClient(create_app(settings=settings, clock=lambda: FIXED_NOW), headers=HEADERS) as client:
        markers = client.get(
            f"{API}/map/markers",
            params={"bbox": "128.75,34.85,129.35,35.40", "zoom": 11, "property_type": "apartment", "period_months": 24},
        ).json()
        assert markers["meta"]["data_as_of"].startswith("2026-10-06T12:00") and markers["data"]["markers"]
        parcel = client.get(f"{API}/parcels/lookup", params={"lat": 35.164, "lng": 129.161})
        assert parcel.status_code == 200 and parcel.json()["data"] is None
        assert parcel.json()["meta"]["unavailable_reason"] == "source_unavailable"
        assert "no-store" in parcel.headers["cache-control"]


def test_real_mode_without_a_cache_explains_what_to_run(tmp_path):
    with pytest.raises(RealDataMissing, match="refresh"):
        create_app(settings=Settings(data_mode="real", data_dir=tmp_path), clock=lambda: FIXED_NOW)


def test_sample_mode_is_the_default_and_ignores_any_cache(tmp_path):
    assert Settings().data_mode == "sample"
    app = create_app(settings=Settings(data_dir=tmp_path), clock=lambda: FIXED_NOW)
    with TestClient(app, headers=HEADERS) as client:
        assert client.get(f"{API}/glossary").status_code == 200


async def test_refresh_collects_then_geocodes_only_what_is_missing(tmp_path):
    lines: list[str] = []
    geocoder = FakeGeocoder()
    report = await refresh(
        CountingSource(), geocoder, tmp_path, now=FIXED_NOW, months=1, sgg_codes=DISTRICTS, out=lines.append
    )
    # 2026-10에는 샘플 응답이 없어 빈 단위 14개만 저장되고, 시군구 중심점 16곳만 조회된다
    assert report.collect.fetched == 14 and report.collect.rows == 0 and report.geocoded == 16

    cache = RawCache(tmp_path)
    await run_collect(CountingSource(), cache, months=("202509",))
    first = await refresh(
        CountingSource(), geocoder, tmp_path, now=FIXED_NOW, months=1, sgg_codes=DISTRICTS, out=lines.append
    )
    assert first.geocoded > 0
    calls = geocoder.calls
    second = await refresh(
        CountingSource(), geocoder, tmp_path, now=FIXED_NOW, months=1, sgg_codes=DISTRICTS, out=lines.append
    )
    assert second.geocoded == 0 and geocoder.calls == calls


async def test_auto_mode_follows_whether_a_cache_exists(tmp_path):
    from app.ingestion.real import resolve_data_mode

    settings = Settings(data_mode="auto", data_dir=tmp_path)
    assert resolve_data_mode(settings) == "sample"
    with TestClient(create_app(settings=settings, clock=lambda: FIXED_NOW), headers=HEADERS) as client:
        assert client.get(f"{API}/parcels/lookup", params={"lat": 35.164, "lng": 129.161}).status_code == 200

    await prepare_cache(tmp_path)
    assert resolve_data_mode(settings) == "real"
    with TestClient(create_app(settings=settings, clock=lambda: FIXED_NOW), headers=HEADERS) as client:
        body = client.get(f"{API}/parcels/lookup", params={"lat": 35.164, "lng": 129.161}).json()
        assert body["meta"]["unavailable_reason"] == "source_unavailable"
    assert resolve_data_mode(Settings(data_mode="sample", data_dir=tmp_path)) == "sample"


async def test_every_response_says_which_data_mode_is_served(tmp_path):
    with TestClient(create_app(settings=Settings(), clock=lambda: FIXED_NOW), headers=HEADERS) as client:
        assert client.get(f"{API}/health").headers["x-data-mode"] == "sample"
        assert client.get(f"{API}/glossary").headers["x-data-mode"] == "sample"
        assert client.get(f"{API}/nope").headers["x-data-mode"] == "sample"

    await prepare_cache(tmp_path)
    app = create_app(settings=Settings(data_mode="real", data_dir=tmp_path), clock=lambda: FIXED_NOW)
    with TestClient(app, headers=HEADERS) as client:
        assert client.get(f"{API}/health").headers["x-data-mode"] == "real"
        first = client.get(f"{API}/glossary")
        again = client.get(f"{API}/glossary", headers={"If-None-Match": first.headers["etag"]})
        assert again.status_code == 304 and again.headers["x-data-mode"] == "real"

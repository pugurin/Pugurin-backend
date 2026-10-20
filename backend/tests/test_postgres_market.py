"""DB 저장소가 파일 캐시 저장소와 같은 답을 내는지 같은 데이터로 비교한다(#38 3단계). Docker가 없으면 건너뛴다."""

import itertools

import pytest

from app.adapters.molit.types import SourceKind
from app.core.busan import SIGUNGU
from app.core.geo import BBox
from app.db import migrate
from app.db.engine import make_engine
from app.db.legal_dong import load_legal_dongs
from app.ingestion.build import GeoIndex, build_dataset, needed_lookups
from app.ingestion.collect import collect
from app.ingestion.geocode import GeocodeCache, geocode_missing
from app.ingestion.load import load_into_db
from app.ingestion.rawcache import RawCache
from app.repositories.memory.market import InMemoryMarketRepository
from app.repositories.postgres.market import PostgresMarketRepository
from app.repositories.types import DealType, Level, PropertyType, Range, TransactionFilter
from tests.conftest import FIXED_NOW, create_database
from tests.fakes.molit import FixtureTradeSource
from tests.test_ingestion import DISTRICTS, FakeGeocoder

pytestmark = pytest.mark.anyio

BUSAN = BBox(128.75, 34.85, 129.35, 35.40)
HAEUNDAE = BBox(129.10, 35.14, 129.22, 35.22)
KINDS = [
    (PropertyType.apartment, DealType.sale),
    (PropertyType.apartment, DealType.jeonse),
    (PropertyType.apartment, DealType.monthly),
    (PropertyType.officetel, DealType.sale),
    (PropertyType.officetel, DealType.monthly),
    (PropertyType.villa, DealType.sale),
    (PropertyType.villa, DealType.jeonse),
    (PropertyType.land, DealType.sale),
]


class RealCodeGeocoder(FakeGeocoder):
    """좌표는 가짜지만 법정동 코드는 실제 마스터에서 찾는다. 두 저장소가 같은 지역 코드를 쓰게 하려는 것이다."""

    _SGG = {s.name: s.code for s in SIGUNGU}
    _CODES = {(d.parent_code, d.name): d.code for d in load_legal_dongs() if d.level == "dong"}

    @classmethod
    def _code(cls, sgg_name: str, umd_nm: str) -> str:
        return cls._CODES[(cls._SGG[sgg_name], umd_nm)]


async def real_geo(trades, tmp) -> GeoIndex:
    cache = GeocodeCache(tmp / "geocode.json")
    addresses, regions = needed_lookups(trades)
    await geocode_missing(RealCodeGeocoder(), cache, addresses, regions)
    return GeoIndex(cache)


@pytest.fixture(scope="module")
def market_db(postgis: str) -> str:
    url = create_database(postgis, "market")
    migrate.upgrade(url)
    return url


@pytest.fixture(scope="module")
async def repos(market_db, tmp_path_factory):
    tmp = tmp_path_factory.mktemp("cache")
    raw = RawCache(tmp)
    await collect(
        FixtureTradeSource(), raw, months=["202509"], sgg_codes=DISTRICTS, kinds=list(SourceKind), now=FIXED_NOW
    )
    trades = [t for w in raw.windows() for t in raw.load(*w)]
    geo = await real_geo(trades, tmp)
    engine = make_engine(market_db)
    await load_into_db(engine, raw, geo, out=lambda _: None)

    dataset, regions, _ = build_dataset(trades, geo)
    memory = InMemoryMarketRepository(FIXED_NOW, dataset, regions)
    postgres = PostgresMarketRepository(engine, fallback_as_of=FIXED_NOW)
    yield memory, postgres, dataset, regions
    await engine.dispose()


def filters():
    for (pt, dt), period, direct, renewals in itertools.product(KINDS, (24, 60), (False, True), (False, True)):
        yield TransactionFilter(pt, dt, period_months=period, exclude_direct=direct, include_renewals=renewals)
    yield TransactionFilter(PropertyType.apartment, DealType.sale, 24, price=Range(300_000_000, 900_000_000))
    yield TransactionFilter(PropertyType.apartment, DealType.sale, 24, exclusive_area_pyeong=Range(20, 30))
    yield TransactionFilter(PropertyType.apartment, DealType.jeonse, 24, deposit=Range(None, 300_000_000))
    yield TransactionFilter(PropertyType.officetel, DealType.monthly, 24, rent=Range(500_000, None))
    yield TransactionFilter(PropertyType.land, DealType.sale, 24, land_area_pyeong=Range(50, 500))


async def test_data_as_of_and_regions_match(repos):
    memory, postgres, _, regions = repos
    await postgres.refresh(force=True)
    assert postgres.data_as_of == memory.data_as_of == FIXED_NOW
    for code in regions:
        assert await postgres.get_region(code) == await memory.get_region(code), code
    # 거래가 없는 동은 둘 다 모른다(사직동은 동래구)
    assert await postgres.get_region("2626010800") is None
    assert postgres.has_data is True


async def test_complexes_match(repos):
    memory, postgres, dataset, _ = repos
    assert dataset.complexes
    for c in dataset.complexes:
        assert await postgres.get_complex(c.id) == await memory.get_complex(c.id), c.name


async def test_map_aggregates_and_markers_match(repos):
    memory, postgres, _, _ = repos
    for flt in filters():
        for level, box in itertools.product((Level.sigungu, Level.dong), (BUSAN, HAEUNDAE)):
            got = await postgres.aggregate_regions(level, box, flt)
            assert got == await memory.aggregate_regions(level, box, flt), (flt, level, box)
        for box in (BUSAN, HAEUNDAE):
            assert await postgres.complex_markers(box, flt) == await memory.complex_markers(box, flt), flt
            assert await postgres.parcel_markers(box, flt) == await memory.parcel_markers(box, flt), flt


async def test_transaction_lists_match(repos):
    memory, postgres, _, _ = repos
    for flt in filters():
        for sort, region, box, cancelled in itertools.product(
            ("latest", "price_asc", "price_desc"), (None, "26350", "2635010500"), (None, HAEUNDAE), (False, True)
        ):
            kw = dict(bbox=box, region_code=region, include_cancelled=cancelled, sort=sort)
            for offset, limit in ((0, 20), (15, 10), (10_000, 20)):
                got = await postgres.list_transactions(flt, offset=offset, limit=limit, **kw)
                assert got == await memory.list_transactions(flt, offset=offset, limit=limit, **kw), (flt, kw)


async def test_complex_transactions_and_single_lookups_match(repos):
    memory, postgres, dataset, _ = repos
    for c in dataset.complexes[:40]:
        for deal_type, direct, cancelled, sort, renewals in itertools.product(
            (None, *DealType), (False, True), (False, True), ("latest", "price_desc"), (False, True)
        ):
            kw = dict(
                deal_type=deal_type,
                exclude_direct=direct,
                include_cancelled=cancelled,
                sort=sort,
                include_renewals=renewals,
            )
            got = await postgres.complex_transactions(c.id, offset=0, limit=50, **kw)
            assert got == await memory.complex_transactions(c.id, offset=0, limit=50, **kw), (c.name, kw)
    for t in dataset.transactions[::97]:
        assert await postgres.get_transaction(t.id) == t


async def test_share_deals_and_roads_are_stored_but_never_served(repos):
    _, postgres, dataset, _ = repos
    served = {t.id for t in dataset.transactions}
    async with postgres._engine.connect() as conn:  # noqa: SLF001 - 숨겨진 행의 id를 직접 확인
        from sqlalchemy import text

        hidden = [r[0] for r in (await conn.execute(text("SELECT id FROM transactions WHERE is_share_deal"))).all()]
    assert hidden and not (set(hidden) & served)
    assert await postgres.get_transaction(hidden[0]) is None


async def test_stats_match(repos):
    memory, postgres, dataset, regions = repos
    codes = [c for c in regions if c.startswith("26350")][:4] + ["26500"]
    for code, (pt, dt), period, direct, renewals in itertools.product(
        codes, KINDS, (24, 60), (False, True), (False, True)
    ):
        args = (code, pt, dt, period, direct)
        got = await postgres.region_stats(*args, include_renewals=renewals)
        assert got == await memory.region_stats(*args, include_renewals=renewals), (args, renewals)
    for c, dt, direct, renewals in itertools.product(dataset.complexes[:60], DealType, (False, True), (False, True)):
        got = await postgres.complex_stats(c.id, dt, 24, direct, include_renewals=renewals)
        assert got == await memory.complex_stats(c.id, dt, 24, direct, include_renewals=renewals), (c.name, dt)
    assert await postgres.region_stats("99999", PropertyType.apartment, DealType.sale, 24, False) is None


async def test_search_matches(repos):
    memory, postgres, dataset, _ = repos
    queries = ["해운대", "우동", "해운대구 우동", "수영", "광안", "센텀", "1408", "자이", "아파트", "없는이름"]
    queries += [c.name[:3] for c in dataset.complexes[:20]]
    for q in queries:
        assert await postgres.search_regions(q, 10) == await memory.search_regions(q, 10), q
        got = [(h.complex.id, h.score, h.popularity) for h in await postgres.search_complexes(q, 10)]
        want = [(h.complex.id, h.score, h.popularity) for h in await memory.search_complexes(q, 10)]
        assert sorted(got) == sorted(want) and [g[1:] for g in got] == [w[1:] for w in want], q


# ── 서버에 붙였을 때 ─────────────────────────────────────────


def _app(url: str):
    from fastapi.testclient import TestClient

    from app.core.config import Settings
    from app.main import create_app

    settings = Settings(database_url=url)
    return TestClient(
        create_app(settings=settings, clock=lambda: FIXED_NOW),
        headers={"X-Device-Id": "2f8f1d52-7b0e-4c55-9c0e-3f6f9d1c2a11"},
        client=("127.0.0.1", 50000),
    )


async def test_the_api_serves_the_database_when_it_is_configured(repos, market_db):
    _, _, dataset, _ = repos
    with _app(market_db) as c:
        r = c.get(
            "/api/v1/map/markers",
            params={"bbox": "128.75,34.85,129.35,35.40", "zoom": 15, "property_type": "apartment", "period_months": 24},
        )
        assert r.status_code == 200 and r.headers["x-data-mode"] == "real"
        assert r.json()["meta"]["data_as_of"].startswith("2026-10-06T12:00")
        ids = {m["complex_id"] for m in r.json()["data"]["markers"]}
        assert ids and ids <= {str(c.id) for c in dataset.complexes}
        ready = c.get("/internal/health/ready").json()
        assert ready["status"] == "ready" and ready["checks"]["data"]["status"] == "ok"


def test_an_empty_database_says_there_is_no_data_yet(postgis):
    url = create_database(postgis, "empty")
    migrate.upgrade(url)
    with _app(url) as c:
        r = c.get(
            "/api/v1/map/markers", params={"bbox": "128.75,34.85,129.35,35.40", "zoom": 11, "property_type": "land"}
        )
        assert r.status_code == 503 and r.json()["error"]["code"] == "DATA_NOT_READY"
        assert r.headers["x-data-mode"] == "none"
        ready = c.get("/internal/health/ready")
        assert ready.status_code == 200
        assert ready.json()["checks"]["data"]["status"] == "warning"
        assert "app.ingestion load" in ready.json()["checks"]["data"]["detail"]

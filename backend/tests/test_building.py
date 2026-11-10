"""건축물대장 → 아파트 평형별 공급면적: 원천 해석, 표본 계산, 나눠 받기, API 반영."""

import asyncio
from datetime import date, timedelta
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.adapters.building import (
    BuildingRegisterClient,
    Parcel,
    is_residential_common,
    supply_pyeong,
    supply_samples,
)
from app.adapters.molit.types import QuotaExceeded, SourceAuthError
from app.core.config import Settings
from app.db import migrate
from app.db.engine import make_engine
from app.ingestion.load import LoadBusy
from app.ingestion.supply import SUPPLY_LOCK_KEY, sync_supply_areas
from app.main import create_app
from tests.conftest import API, FIXED_NOW, create_database

pytestmark = pytest.mark.anyio


def unit(ho: str, exclusive: float, commons: list[tuple[str, float]], *, dong: str = "101동", use: str = "아파트"):
    """한 호의 전유 1행 + 공용 행들. commons = [(용도, 면적)]"""
    base = {"mgmBldrgstPk": 1, "dongNm": dong, "hoNm": ho}
    rows = [
        {**base, "exposPubuseGbCdNm": "전유", "mainPurpsCdNm": use, "etcPurps": f"공동주택({use})", "area": exclusive}
    ]
    rows += [
        {**base, "exposPubuseGbCdNm": "공용", "mainPurpsCdNm": "아파트", "etcPurps": p, "area": a} for p, a in commons
    ]
    return rows


# 84.95㎡ + 벽체 7 + 코아 24 = 115.95㎡ = 35.1평. 지하주차장·관리사무소는 공급면적이 아니다
UNIT_85 = [("벽체", 7.0), ("코아", 24.0), ("지하주차장,휀룸(지2~지1)", 44.7), ("관리사무소,경비실", 0.3)]
# 59.9㎡ + 계단실,승강기,홀 20 = 79.9㎡ = 24.2평
UNIT_60 = [("계단실,승강기,홀", 20.0), ("경로당,어린이집", 1.0)]


# ── 원천 해석 ─────────────────────────────────────────────


def test_pnu_maps_to_building_register_parcel_codes():
    p = Parcel.from_pnu("2641010800107450000")
    assert (p.sigungu, p.bjdong, p.plat_gb, p.bun, p.ji) == ("26410", "10800", "0", "0745", "0000")
    assert Parcel.from_pnu("2671025022200650003").plat_gb == "1"  # 산
    with pytest.raises(ValueError):
        Parcel.from_pnu("26410108001")


@pytest.mark.parametrize(
    ("purpose", "main", "expected"),
    [
        ("벽체", "아파트", True),
        ("계단실(엘리베이터)", "아파트", True),
        ("복도,계단,EV", "부대시설", True),
        ("지하주차장(지4-1층)", "아파트", False),
        ("관리사무소, 노인정", "아파트", False),
        ("전기,발전기실,코아(지2~", "오피스텔", False),  # 기타공용이 섞이면 뺀다
        ("도시형생활주택", "아파트", True),  # 뭉뚱그린 용도는 주용도가 주거일 때만
        (" ", "부대시설", False),
        ("대피소", "부대시설", False),
    ],
)
def test_residential_common_rows_are_told_apart_by_purpose(purpose, main, expected):
    assert is_residential_common(purpose, main) is expected


def test_supply_samples_add_only_residential_common_area():
    pages = {1: unit("101", 84.95, UNIT_85) + unit("102", 59.9, UNIT_60) + unit("103", 84.95, UNIT_85)}
    samples = supply_samples(pages, last_page=1)
    assert samples == {85: [115.95, 115.95], 60: [79.9]}
    assert supply_pyeong(samples[85]) == 35.1 and supply_pyeong(samples[60]) == 24.2


def test_units_cut_at_an_unfetched_page_edge_are_skipped():
    whole = unit("101", 84.95, UNIT_85) + unit("102", 84.95, UNIT_85)
    first, rest = whole[:7], whole[7:]  # 102호의 마지막 행 하나가 2페이지로 넘어갔다
    assert supply_samples({1: first}, last_page=2) == {85: [115.95]}
    assert supply_samples({1: first, 2: rest}, last_page=2) == {85: [115.95, 115.95]}


def test_implausible_ratios_and_non_apartment_units_are_dropped():
    old = unit("201", 84.0, [("계단실", 6.9)])  # 1.08배: 벽체가 안 적힌 옛 대장
    shop = unit("1", 50.0, [("복도,계단", 10.0)], use="소매점")
    assert supply_samples({1: old + shop}, last_page=1) == {}
    assert supply_pyeong([]) is None


def test_the_most_common_supply_wins_within_an_area_type():
    assert supply_pyeong([115.95, 115.95, 117.4]) == 35.1


def http_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _no_sleep(_):
    return None


async def test_client_asks_for_100_rows_per_page_and_reports_the_page_count():
    def handler(request):
        p = request.url.params
        assert request.url.path.endswith("/getBrExposPubuseAreaInfo")
        assert (p["sigunguCd"], p["bjdongCd"], p["platGbCd"], p["bun"], p["ji"]) == (
            "26410",
            "10800",
            "0",
            "0745",
            "0000",
        )
        assert p["numOfRows"] == "100" and p["serviceKey"] == "k"
        item = unit("101", 84.95, [])[0]  # 1건이면 객체로 온다
        body = {"items": {"item": item}, "totalCount": 250}
        return httpx.Response(200, json={"response": {"header": {"resultCode": "00"}, "body": body}})

    client = BuildingRegisterClient("k", http=http_client(handler), sleep=_no_sleep)
    rows, pages = await client.unit_areas("2641010800107450000", 1)
    assert (len(rows), pages, client.calls_made) == (1, 3, 1)


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (httpx.Response(200, text="<returnAuthMsg>SERVICE_KEY_IS_NOT_REGISTERED_ERROR</returnAuthMsg>"), SourceAuthError),
        (httpx.Response(200, text="<returnAuthMsg>LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR</returnAuthMsg>"), QuotaExceeded),
        (httpx.Response(429), QuotaExceeded),
    ],
)  # fmt: skip
async def test_client_maps_key_and_quota_errors(response, error):
    client = BuildingRegisterClient("k", http=http_client(lambda r: response), sleep=_no_sleep)
    with pytest.raises(error):
        await client.unit_areas("2641010800107450000", 1)


# ── 동기화 (실제 PostGIS) ─────────────────────────────────────

BIG = UUID("6f1d2a8e-0000-4000-8000-0000000000b1")  # 거래 많음 → 먼저 본다
SMALL = UUID("6f1d2a8e-0000-4000-8000-0000000000b2")
NO_REGISTER = UUID("6f1d2a8e-0000-4000-8000-0000000000b3")  # 대장이 비어 있다
OFFICETEL = UUID("6f1d2a8e-0000-4000-8000-0000000000b4")  # 아파트만 본다
PNU = {BIG: "2635010500114070000", SMALL: "2635010500114080000", NO_REGISTER: "2635010500114090000",
       OFFICETEL: "2635010500114100000"}  # fmt: skip


class FakeRegister:
    """PNU별 페이지 목록. error를 넣으면 그 PNU에서 그 예외를 던진다."""

    def __init__(self, pages: dict[str, list[list[dict]]]):
        self.pages, self.calls_made, self.asked, self.errors = pages, 0, [], {}

    async def unit_areas(self, pnu: str, page: int):
        self.calls_made += 1
        self.asked.append((pnu, page))
        if pnu in self.errors:
            raise self.errors[pnu]
        pages = self.pages.get(pnu, [])
        return (pages[page - 1] if page <= len(pages) else []), max(1, len(pages))


def register() -> FakeRegister:
    return FakeRegister(
        {
            # 1쪽에 85형만, 2쪽에 60형. 쪽 끝의 호는 다음 쪽에 이어질 수 있어 안 센다. 3쪽은 받을 필요가 없다
            PNU[BIG]: [
                unit("101", 84.95, UNIT_85) + unit("103", 84.95, UNIT_85),
                unit("102", 59.9, UNIT_60) + unit("104", 59.9, UNIT_60),
                unit("105", 84.95, UNIT_85),
            ],
            # 115형은 대장에 없다(산출 못 함)
            PNU[SMALL]: [unit("201", 84.95, UNIT_85)],
            PNU[OFFICETEL]: [unit("301", 84.95, UNIT_85)],
        }
    )


@pytest.fixture(scope="module")
def supply_db(postgis: str) -> str:
    url = create_database(postgis, "supply")
    migrate.upgrade(url)
    return url


@pytest.fixture
async def engine(supply_db):
    engine = make_engine(supply_db)
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE complexes, etl_runs RESTART IDENTITY CASCADE"))
        for cid, kind in (
            (BIG, "apartment"),
            (SMALL, "apartment"),
            (NO_REGISTER, "apartment"),
            (OFFICETEL, "officetel"),
        ):
            await conn.execute(
                text(
                    """
                    INSERT INTO complexes (id, property_type, name, name_key, region_code, jibun, pnu, location,
                                           location_source)
                    VALUES (:id, :kind, :name, :name, '2635010500', '1407', :pnu,
                            ST_SetSRID(ST_MakePoint(129.166, 35.166), 4326), 'geocode')
                    """
                ),
                {"id": cid, "kind": kind, "name": f"가상{str(cid)[-2:]}", "pnu": PNU[cid]},
            )
        await conn.execute(
            text(
                "INSERT INTO complex_area_types (complex_id, area_bucket, exclusive_area_m2) VALUES "
                "(:big, 60, 59.9), (:big, 85, 84.95), (:small, 85, 84.95), (:small, 115, 114.9), "
                "(:none, 85, 84.9), (:o, 85, 84.95)"
            ),
            {"big": BIG, "small": SMALL, "none": NO_REGISTER, "o": OFFICETEL},
        )
        run = (
            await conn.execute(text("INSERT INTO etl_runs (job, status) VALUES ('collect', 'success') RETURNING id"))
        ).scalar_one()
        for n in range(3):
            await conn.execute(
                text(
                    """
                    INSERT INTO transactions (id, source_key, source_kind, property_type, deal_type, complex_id,
                        region_code, location, location_precision, price, exclusive_area_m2, contract_date,
                        first_seen_run_id, last_seen_run_id)
                    VALUES (gen_random_uuid(), :key, 'apt_trade', 'apartment', 'sale', :cid, '2635010500',
                        ST_SetSRID(ST_MakePoint(129.166, 35.166), 4326), 'parcel', 500000000, 84.95, :day, :run, :run)
                    """
                ),
                {"key": f"{n:040d}", "cid": BIG, "day": date(2026, 9, 1 + n), "run": run},
            )
    yield engine
    await engine.dispose()


async def supply(engine, source, *, days=0, max_calls=100):
    return await sync_supply_areas(
        engine, source, now=FIXED_NOW + timedelta(days=days), max_calls=max_calls, out=lambda _: None
    )


async def fetch(engine, sql: str):
    async with engine.connect() as conn:
        return [tuple(r) for r in (await conn.execute(text(sql))).all()]


async def test_fills_apartment_area_types_and_stops_paging_once_every_type_is_found(engine):
    source = register()
    report = await supply(engine, source)
    assert (report.status, report.complexes, report.area_types_filled, report.area_types_missing) == (
        "success",
        3,
        3,
        2,
    )
    assert await fetch(
        engine,
        "SELECT c.name, t.area_bucket, t.supply_area_pyeong::float8, t.supply_source FROM complex_area_types t "
        "JOIN complexes c ON c.id = t.complex_id ORDER BY 1, 2",
    ) == [
        ("가상b1", 60, 24.2, "building_register"),
        ("가상b1", 85, 35.1, "building_register"),
        ("가상b2", 85, 35.1, "building_register"),
        ("가상b2", 115, None, None),
        ("가상b3", 85, None, None),
        ("가상b4", 85, None, None),  # 오피스텔은 보지 않는다
    ]
    assert (PNU[BIG], 3) not in source.asked and (PNU[BIG], 1) == source.asked[0]  # 거래 많은 단지부터
    assert not any(pnu == PNU[OFFICETEL] for pnu, _ in source.asked)
    assert await fetch(engine, "SELECT count(*) FROM complexes WHERE supply_checked_at IS NOT NULL") == [(3,)]
    assert await fetch(engine, "SELECT job, status, calls_made FROM etl_runs WHERE job = 'building'") == [
        ("building", "success", report.calls)
    ]


async def test_checked_complexes_wait_a_month_before_the_missing_types_are_tried_again(engine):
    await supply(engine, register())
    soon = register()
    assert (await supply(engine, soon, days=1)).complexes == 0 and soon.asked == []
    later = register()
    again = await supply(engine, later, days=31)
    assert again.complexes == 2  # 빈 평형이 남은 SMALL·NO_REGISTER만. BIG은 다 채워졌다
    assert {pnu for pnu, _ in later.asked} == {PNU[SMALL], PNU[NO_REGISTER]}


async def test_the_daily_call_budget_leaves_the_rest_for_tomorrow(engine):
    first = await supply(engine, register(), max_calls=2)
    assert first.calls <= 2 + 3  # 동시에 4곳까지 돌다가 멈춘다
    unchecked = await fetch(engine, "SELECT count(*) FROM complexes WHERE supply_checked_at IS NULL")
    assert unchecked[0][0] >= 2  # 끝까지 못 본 단지는 확인 시각을 남기지 않는다(오피스텔 포함)
    rest = await supply(engine, register(), days=1)
    assert rest.complexes >= 1
    assert await fetch(
        engine,
        "SELECT count(*) FROM complexes WHERE property_type = 'apartment' AND supply_checked_at IS NULL",
    ) == [(0,)]


async def test_a_quota_error_stops_the_run_without_marking_anything_unseen(engine):
    source = register()
    source.errors[PNU[BIG]] = QuotaExceeded("건축물대장 호출 한도 초과")
    report = await supply(engine, source)
    assert report.status == "quota_exceeded" and "한도" in report.errors[0]
    assert await fetch(engine, f"SELECT supply_checked_at IS NULL FROM complexes WHERE id = '{BIG}'") == [(True,)]


async def test_a_second_sync_refuses_to_run_while_one_is_in_progress(engine):
    async with engine.connect() as holder:
        await holder.execute(text("SELECT pg_advisory_lock(:k)"), {"k": SUPPLY_LOCK_KEY})
        with pytest.raises(LoadBusy):
            await supply(engine, register())
        await holder.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": SUPPLY_LOCK_KEY})


# ── API ─────────────────────────────────────────────────────


async def test_the_api_serves_supply_areas_on_complexes_and_map_markers(engine, supply_db):
    await supply(engine, register())
    app = create_app(settings=Settings(database_url=supply_db), clock=lambda: FIXED_NOW)
    with TestClient(app, headers={"X-Device-Id": "2f8f1d52-7b0e-4c55-9c0e-3f6f9d1c2a11"}) as c:
        detail = await asyncio.to_thread(c.get, f"{API}/complexes/{BIG}")
        markers = await asyncio.to_thread(
            c.get,
            f"{API}/map/markers",
            params={"bbox": "129.10,35.14,129.22,35.22", "zoom": 16, "property_type": "apartment"},
        )
    assert detail.status_code == 200
    assert [(a["exclusive_area_m2"], a["supply_area_pyeong"]) for a in detail.json()["data"]["area_types"]] == [
        (59.9, 24.2),
        (84.95, 35.1),
    ]
    latest = next(m["latest"] for m in markers.json()["data"]["markers"] if m["complex_id"] == str(BIG))
    assert latest["supply_area_pyeong"] == 35.1

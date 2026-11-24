"""관심 단지 주간 리포트 계산·생성 (실제 PostGIS, 가짜 데이터)."""

import json
import logging
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from app.core.dates import KST
from app.db import migrate
from app.db.engine import make_engine
from app.services.interest_report import (
    ReportRun,
    ReportWeekNotOver,
    _area_label,
    _format_diff,
    format_price,
    generate_interest_reports,
    week_start,
)
from tests.conftest import create_database

pytestmark = pytest.mark.anyio

# 리포트 주: 2026-11-16(월) 00:00 ~ 11-23(월) 00:00 KST. 만드는 시각은 11-23 08:00
WEEK = date(2026, 11, 16)
NOW = datetime(2026, 11, 23, 8, 0, tzinfo=KST)
WEEK_START = datetime(2026, 11, 16, tzinfo=KST)
IN_WEEK = datetime(2026, 11, 18, 4, 0, tzinfo=KST)
LAST_WEEK = datetime(2026, 11, 11, 4, 0, tzinfo=KST)
LONG_AGO = datetime(2026, 6, 1, 4, 0, tzinfo=KST)

DAEYEON = "2629010600"  # 남구 대연동
UDONG = "2635010500"  # 해운대구 우동
PNU = "2629010600100010000"
OTHER_PNU = "2629010600100020000"

# 이름순: 가람 < 나래 < 다솜 < 라온
GARAM = UUID("7a000000-0000-4000-8000-000000000001")
NARAE = UUID("7a000000-0000-4000-8000-000000000002")
DASOM = UUID("7a000000-0000-4000-8000-000000000003")
RAON = UUID("7a000000-0000-4000-8000-000000000004")

# 평형: (area_bucket, 전용면적, 공급평형)
P34 = (85, 84.97, 34.0)
P25 = (60, 59.9, 25.0)
P41 = (102, 101.9, 41.0)
P15 = (40, 39.8, 15.0)

INSIDE = (129.09, 35.13)  # 정비구역 경계 안
OUTSIDE = (129.20, 35.20)
NARAE_AT = (129.15, 35.16)  # busy_week에서 나래단지만 덮는 경계 안


# ── 테스트 데이터 ──────────────────────────────────────────


@pytest.fixture(scope="module")
def report_db(postgis: str) -> str:
    url = create_database(postgis, "interest_report")
    migrate.upgrade(url)
    return url


@pytest.fixture
async def engine(report_db):
    engine = make_engine(report_db)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE notifications, user_interests, users, transactions, complexes, auctions, "
                "redevelopment_zones, redevelopments, etl_runs RESTART IDENTITY CASCADE"
            )
        )
    yield engine
    await engine.dispose()


async def run_(engine, finished_at: datetime | None, *, job: str = "collect", status: str = "success") -> int:
    async with engine.begin() as conn:
        return (
            await conn.execute(
                text(
                    "INSERT INTO etl_runs (job, status, started_at, finished_at) "
                    "VALUES (:job, :status, coalesce(:at, now()), :at) RETURNING id"
                ),
                {"job": job, "status": status, "at": finished_at},
            )
        ).scalar_one()


async def complex_(
    engine,
    complex_id: UUID,
    name: str,
    areas: list[tuple[int, float, float | None]],
    *,
    code: str = DAEYEON,
    pnu: str | None = None,
    at: tuple[float, float] = INSIDE,
    property_type: str = "apartment",
):
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
                INSERT INTO complexes (id, property_type, name, name_key, region_code, jibun, pnu, location,
                                       location_source)
                VALUES (:id, :ptype, :name, :name, :code, :jibun, :pnu, ST_SetSRID(ST_MakePoint(:lng, :lat), 4326),
                        'geocode')
                """
            ),
            {
                "id": complex_id,
                "ptype": property_type,
                "name": name,
                "code": code,
                "jibun": str(complex_id),
                "pnu": pnu,
                "lng": at[0],
                "lat": at[1],
            },
        )
        for bucket, area, supply in areas:
            await conn.execute(
                text(
                    "INSERT INTO complex_area_types (complex_id, area_bucket, exclusive_area_m2, supply_area_pyeong) "
                    "VALUES (:c, :b, :a, :s)"
                ),
                {"c": complex_id, "b": bucket, "a": area, "s": supply},
            )


async def sale(
    engine,
    run: int,
    price: int,
    *,
    complex_id: UUID = GARAM,
    area: float = P34[1],
    contract: date = date(2026, 11, 2),
    deal_type: str = "sale",
    cancelled: bool = False,
    deleted: bool = False,
):
    jeonse = deal_type == "jeonse"
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
                INSERT INTO transactions (id, source_key, source_kind, property_type, deal_type, complex_id,
                    region_code, location, location_precision, price, deposit, exclusive_area_m2, contract_date,
                    is_cancelled, deleted_at, first_seen_run_id, last_seen_run_id)
                VALUES (gen_random_uuid(), :key, 'apt_trade', 'apartment', :deal, :complex_id, :code,
                    ST_SetSRID(ST_MakePoint(129.09, 35.13), 4326), 'parcel', :price, :deposit, :area, :contract,
                    :cancelled, :deleted, :run, :run)
                """
            ),
            {
                "key": uuid4().hex,
                "deal": deal_type,
                "complex_id": complex_id,
                "code": DAEYEON,
                "price": None if jeonse else price,
                "deposit": price if jeonse else None,
                "area": area,
                "contract": contract,
                "cancelled": cancelled,
                "deleted": NOW if deleted else None,
                "run": run,
            },
        )


async def auction(
    engine,
    case: str,
    *,
    pnu: str | None = PNU,
    area: float | None = P25[1],
    min_price: int | None = 410_000_000,
    first_seen: datetime = IN_WEEK,
    source: str = "court",
    sale_date: date | None = date(2026, 12, 10),
    bid_end_at: datetime | None = None,
    closed_at: datetime | None = None,
    usage: str = "아파트",
):
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
                INSERT INTO auctions (id, source, case_no, usage, address, region_code, pnu, min_price, sale_date,
                                      bid_end_at, building_area_m2, list_hash, first_seen_at, last_seen_at, closed_at)
                VALUES (:id, :source, :case, :usage, '부산 남구 대연동 1', :code, :pnu, :price, :sale_date,
                        :bid_end_at, :area, 'h', :seen, :seen, :closed)
                """
            ),
            {
                "id": f"CA-{case}",
                "source": source,
                "case": case,
                "usage": usage,
                "code": DAEYEON,
                "pnu": pnu,
                "price": min_price,
                "sale_date": sale_date,
                "bid_end_at": bid_end_at,
                "area": area,
                "seen": first_seen,
                "closed": closed_at,
            },
        )


async def redevelopment(
    engine,
    name: str,
    stage: str,
    previous: str | None,
    changed_at: datetime | None,
    *,
    kind: str = "reconstruction",
) -> str:
    rid = f"Z-{name}"
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
                INSERT INTO redevelopments (id, area_name, kind, stage, previous_stage, stage_changed_at,
                                            first_seen_at, last_seen_at)
                VALUES (:id, :name, :kind, :stage, :prev, :changed, :seen, :seen)
                """
            ),
            {
                "id": rid,
                "name": name,
                "kind": kind,
                "stage": stage,
                "prev": previous,
                "changed": changed_at,
                "seen": LONG_AGO,
            },
        )
    return rid


async def zone_piece(engine, zone_id: str, redevelopment_id: str | None, bbox: tuple[float, float, float, float]):
    """경계 조각 하나(사각형). bbox = (min_lng, min_lat, max_lng, max_lat)."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
                INSERT INTO redevelopment_zones (zone_id, layer, sigungu_code, geom, label_point, area_m2,
                                                 redevelopment_id, loaded_at)
                SELECT :zid, 'zone', '26290', ST_Multi(g), ST_PointOnSurface(g), 1000, :rid, :now
                FROM (SELECT ST_MakeEnvelope(:x0, :y0, :x1, :y1, 4326) AS g) s
                """
            ),
            {
                "zid": zone_id,
                "rid": redevelopment_id,
                "now": NOW,
                "x0": bbox[0],
                "y0": bbox[1],
                "x1": bbox[2],
                "y1": bbox[3],
            },
        )


async def user(engine, *complexes: UUID, deleted: bool = False, settings: dict | None = None) -> UUID:
    user_id = uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, nickname, agreed_at, created_at, deleted_at, settings) "
                "VALUES (:id, 'n', :t, :t, :del, CAST(:settings AS jsonb))"
            ),
            {
                "id": user_id,
                "t": NOW - timedelta(days=200),
                "del": NOW if deleted else None,
                "settings": json.dumps(settings or {}),
            },
        )
        for complex_id in complexes:
            await conn.execute(
                text("INSERT INTO user_interests (user_id, kind, target_id) VALUES (:u, 'complex', :c)"),
                {"u": user_id, "c": str(complex_id)},
            )
    return user_id


async def reports(engine) -> list:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT user_id, kind, period_start, title, summary, payload FROM notifications "
                    "ORDER BY created_at, user_id"
                )
            )
        ).all()


async def generate(engine, week: date = WEEK) -> ReportRun:
    return await generate_interest_reports(engine, week=week, now=NOW)


def block(row, complex_id: UUID) -> dict:
    return next(b for b in row.payload["complexes"] if b["id"] == str(complex_id))


async def one_block(engine, complex_id: UUID = GARAM) -> dict:
    [row] = await reports(engine)
    return block(row, complex_id)


# ── 주 계산·표기 ───────────────────────────────────────────


def test_week_start_is_the_monday_of_the_last_finished_week_in_kst():
    assert week_start(NOW) == WEEK
    assert week_start(datetime(2026, 11, 22, 23, 59, tzinfo=KST)) == date(2026, 11, 9)
    # UTC 일요일 15:00 = KST 월요일 00:00 → 방금 끝난 주
    assert week_start(datetime(2026, 11, 22, 15, 0, tzinfo=UTC)) == WEEK
    assert week_start(datetime(2026, 11, 22, 14, 59, tzinfo=UTC)) == date(2026, 11, 9)


def test_prices_are_written_like_the_app():
    assert format_price(980_000_000) == "9.8억"
    assert format_price(1_000_000_000) == "10억"
    assert format_price(41_000_000) == "4,100만"
    # 앱 toFixed(1)은 float 값 그대로 반올림(동률이면 위로). 파이썬 :.1f는 은행가 반올림이라 다르다
    assert format_price(125_000_000) == "1.3억"
    assert format_price(525_000_000) == "5.3억"
    assert format_price(1_225_000_000) == "12.3억"
    assert format_price(115_000_000) == "1.1억"  # 1.15는 float로 1.1499…라 앱도 1.1
    assert _format_diff(-125_000_000) == "-1.3억"


def test_area_labels_are_written_like_the_app():
    # 앱 areaLabel(평 단위)과 같게: 공급평형을 알면 "N평형", 모르면 "전용 25.7평"(toFixed(1), 끝 .0 생략)
    assert _area_label(34.0, 84.97) == "34평형"
    assert _area_label(33.6, None) == "34평형"
    assert _area_label(None, 84.9) == "전용 25.7평"  # 84.9 ÷ 3.3058 = 25.68…
    assert _area_label(None, 84.97) == "전용 25.7평"
    assert _area_label(None, 82.645) == "전용 25평"  # 나누면 25.0 → 끝 .0 생략
    assert _area_label(None, 59.9) == "전용 18.1평"
    assert _area_label(None, None) is None


# ── 설계 문서 §3의 한 주 ───────────────────────────────────


async def busy_week(engine) -> UUID:
    """가람단지: 새 실거래 5건(34평형 2·25평형 2·41평형 1)과 새 경매 1건, 나래단지: 재건축 단계 변화,
    다솜·라온단지: 무소식."""
    this_week = await run_(engine, IN_WEEK)
    old = await run_(engine, LONG_AGO)
    await complex_(engine, GARAM, "가람단지", [P34, P25, P41], pnu=PNU)
    await complex_(engine, NARAE, "나래단지", [P34], code=UDONG, at=NARAE_AT)
    await complex_(engine, DASOM, "다솜단지", [P34], at=OUTSIDE)
    await complex_(engine, RAON, "라온단지", [P34], at=OUTSIDE)
    await sale(engine, this_week, 940_000_000, area=P34[1], contract=date(2026, 11, 2))
    await sale(engine, this_week, 900_000_000, area=P34[1], contract=date(2026, 10, 30))
    await sale(engine, old, 920_000_000, area=P34[1], contract=date(2026, 8, 1))  # 직전 같은 평형
    await sale(engine, this_week, 680_000_000, area=P25[1], contract=date(2026, 10, 28))
    await sale(engine, this_week, 650_000_000, area=P25[1], contract=date(2026, 10, 20))
    await sale(engine, this_week, 1_190_000_000, area=P41[1], contract=date(2026, 11, 5))
    await auction(engine, "2026타경1")
    rid = await redevelopment(engine, "삼익비치구역", "project_approved", "association", IN_WEEK)
    await zone_piece(engine, "zone-1", rid, (129.14, 35.15, 129.16, 35.17))
    return await user(engine, RAON, GARAM, DASOM, NARAE)


async def test_report_for_a_busy_week(engine):
    user_id = await busy_week(engine)

    result = await generate(engine)

    assert result == ReportRun(users=1, created=1, skipped_empty=0, skipped_existing=0, no_collect=False)
    [row] = await reports(engine)
    assert (row.user_id, row.kind, row.period_start) == (user_id, "interest_report", WEEK)
    assert row.title == "관심 단지 주간 리포트 · 11/16~11/22"
    assert row.summary == "가람단지 새 실거래 5건 외 1개 단지"
    payload = row.payload
    assert payload["period"] == {"start": "2026-11-16", "end": "2026-11-22"}
    assert payload["headline"] == "관심 단지 4곳 중 2곳에 새 소식이 있어요"
    assert (payload["total"], payload["with_news"]) == (4, 2)
    assert [b["name"] for b in payload["complexes"]] == ["가람단지", "나래단지"]
    assert payload["quiet"] == [{"id": str(DASOM), "name": "다솜단지"}, {"id": str(RAON), "name": "라온단지"}]
    garam = payload["complexes"][0]
    auction_ = garam.pop("auctions")
    assert garam == {
        "id": str(GARAM),
        "name": "가람단지",
        "sigungu_name": "남구",
        "dong_name": "대연동",
        "lat": pytest.approx(35.13),
        "lng": pytest.approx(129.09),
        "lines": [
            "새 실거래 5건",
            "34평형 2건: 최근 9.4억 (11/02) · 직전 같은 평형보다 +2,000만",
            "25평형 2건: 최근 6.8억 (10/28)",
            "41평형 1건: 11.9억 (11/05)",
            "새 경매 1건: 25평형 · 최저가 4.1억 · 매각기일 12/10",
        ],
        "sales": {
            "count": 5,
            "areas": [
                {
                    "area_bucket": 85,
                    "area_label": "34평형",
                    "count": 2,
                    "latest": {"price": 940_000_000, "contract_date": "2026-11-02"},
                    "prev_diff": 20_000_000,
                },
                {
                    "area_bucket": 60,
                    "area_label": "25평형",
                    "count": 2,
                    "latest": {"price": 680_000_000, "contract_date": "2026-10-28"},
                    "prev_diff": None,
                },
                {
                    "area_bucket": 102,
                    "area_label": "41평형",
                    "count": 1,
                    "latest": {"price": 1_190_000_000, "contract_date": "2026-11-05"},
                    "prev_diff": None,
                },
            ],
            "more_areas": 0,
        },
        "redevelopments": [],
    }
    assert auction_ == [
        {
            "id": "CA-2026타경1",
            "area_label": "25평형",
            "min_price": 410_000_000,
            "date": "2026-12-10",
            "source": "court",
        }
    ]
    narae = payload["complexes"][1]
    assert (narae["sigungu_name"], narae["dong_name"]) == ("해운대구", "우동")
    assert narae["lines"] == ["삼익비치구역: '조합설립인가' → '사업시행계획인가'로 단계가 바뀌었어요"]
    assert narae["sales"] == {"count": 0, "areas": [], "more_areas": 0}
    assert narae["auctions"] == []
    assert narae["redevelopments"] == [
        {
            "id": "Z-삼익비치구역",
            "area_name": "삼익비치구역",
            "kind": "reconstruction",
            "previous_stage": "association",
            "stage": "project_approved",
        }
    ]
    assert payload["sources"] == [
        "국토교통부 실거래가 공개시스템",
        "대법원 법원경매정보·온비드",
        "부산광역시 정비사업 정보",
    ]
    assert payload["lag_notice"] == "최근 30일 거래는 아직 신고되지 않았을 수 있어요"


# ── 새 실거래 ───────────────────────────────────────────────


async def test_week_boundary_uses_run_finished_at_kst(engine):
    before = await run_(engine, WEEK_START - timedelta(seconds=1))
    after = await run_(engine, WEEK_START + timedelta(seconds=1))
    next_week = await run_(engine, WEEK_START + timedelta(days=7))
    await complex_(engine, GARAM, "가람단지", [P34])
    await sale(engine, before, 500_000_000, contract=date(2026, 11, 1))
    await sale(engine, after, 600_000_000, contract=date(2026, 11, 3))
    await sale(engine, next_week, 700_000_000, contract=date(2026, 11, 5))
    await user(engine, GARAM)

    await generate(engine)

    garam = await one_block(engine)
    assert garam["sales"]["count"] == 1
    assert garam["sales"]["areas"][0]["latest"] == {"price": 600_000_000, "contract_date": "2026-11-03"}


async def test_first_collect_decides_newness_and_old_contracts_are_dropped(engine):
    collect = await run_(engine, IN_WEEK)
    backfill = await run_(engine, IN_WEEK, job="backfill")
    await complex_(engine, GARAM, "가람단지", [P34])
    await sale(engine, backfill, 900_000_000)
    await sale(engine, collect, 800_000_000, contract=WEEK - timedelta(days=61))
    # 계약일이 오래돼도 60일 안이고 이번 주 수집이 처음 봤으면 새 거래다
    await sale(engine, collect, 500_000_000, contract=WEEK - timedelta(days=60))
    await user(engine, GARAM)

    await generate(engine)

    garam = await one_block(engine)
    assert garam["sales"]["count"] == 1
    assert garam["sales"]["areas"][0]["latest"] == {"price": 500_000_000, "contract_date": "2026-09-17"}


async def test_cancelled_deleted_zero_and_rent_trades_are_excluded(engine):
    run = await run_(engine, IN_WEEK)
    await complex_(engine, GARAM, "가람단지", [P34])
    await sale(engine, run, 500_000_000)
    await sale(engine, run, 900_000_000, cancelled=True)
    await sale(engine, run, 800_000_000, deleted=True)
    await sale(engine, run, 0)
    await sale(engine, run, 600_000_000, deal_type="jeonse")
    await user(engine, GARAM)

    await generate(engine)

    garam = await one_block(engine)
    assert garam["sales"]["count"] == 1
    assert garam["lines"][:2] == ["새 실거래 1건", "34평형 1건: 5억 (11/02)"]


async def test_only_zero_price_trades_make_no_news(engine):
    run = await run_(engine, IN_WEEK)
    await complex_(engine, GARAM, "가람단지", [P34])
    await sale(engine, run, 0)
    await user(engine, GARAM)

    result = await generate(engine)

    assert result.skipped_empty == 1
    assert await reports(engine) == []


async def test_at_most_three_area_lines_by_count_then_larger(engine):
    run = await run_(engine, IN_WEEK)
    await complex_(engine, GARAM, "가람단지", [P15, P25, P34, P41])
    for area, n in [(P15[1], 1), (P25[1], 2), (P34[1], 1), (P41[1], 1)]:
        for _ in range(n):
            await sale(engine, run, 500_000_000, area=area)
    await user(engine, GARAM)

    await generate(engine)

    garam = await one_block(engine)
    assert [(a["area_bucket"], a["count"]) for a in garam["sales"]["areas"]] == [(60, 2), (102, 1), (85, 1)]
    assert garam["sales"]["more_areas"] == 1
    assert garam["lines"] == [
        "새 실거래 5건",
        "25평형 2건: 최근 5억 (11/02)",
        "41평형 1건: 5억 (11/02)",
        "34평형 1건: 5억 (11/02)",
        "외 1개 평형",
    ]


async def test_latest_trade_is_the_latest_contract_then_higher_price(engine):
    run = await run_(engine, IN_WEEK)
    await complex_(engine, GARAM, "가람단지", [P34])
    await sale(engine, run, 700_000_000, contract=date(2026, 11, 3))
    await sale(engine, run, 750_000_000, contract=date(2026, 11, 3))
    await sale(engine, run, 990_000_000, contract=date(2026, 11, 1))
    await user(engine, GARAM)

    await generate(engine)

    garam = await one_block(engine)
    assert garam["sales"]["areas"][0]["latest"] == {"price": 750_000_000, "contract_date": "2026-11-03"}


async def test_exclusive_pyeong_when_supply_is_unknown(engine):
    run = await run_(engine, IN_WEEK)
    await complex_(engine, GARAM, "가람단지", [(85, 84.97, None)])
    await sale(engine, run, 500_000_000, area=84.97)
    await user(engine, GARAM)

    await generate(engine)

    garam = await one_block(engine)
    # 84.97㎡ ÷ 3.3058 = 25.7 → 앱처럼 "전용 25.7평"
    assert garam["sales"]["areas"][0]["area_label"] == "전용 25.7평"
    assert garam["lines"][1] == "전용 25.7평 1건: 5억 (11/02)"


# ── 직전 같은 평형 대비 ──────────────────────────────────────


async def test_prev_diff_uses_the_last_older_trade_of_the_same_area(engine):
    run = await run_(engine, IN_WEEK)
    old = await run_(engine, LONG_AGO)
    await complex_(engine, GARAM, "가람단지", [P34, P25])
    await sale(engine, run, 800_000_000, contract=date(2026, 11, 2))
    await sale(engine, old, 900_000_000, contract=date(2026, 9, 1))  # 더 늦은 직전 거래
    await sale(engine, old, 500_000_000, contract=date(2026, 7, 1))
    await sale(engine, old, 100_000_000, contract=date(2026, 10, 1), area=P25[1])  # 다른 평형
    await sale(engine, old, 100_000_000, contract=date(2026, 10, 2), cancelled=True)
    await sale(engine, old, 0, contract=date(2026, 10, 3))
    await sale(engine, old, 100_000_000, contract=date(2026, 11, 10))  # 최근 거래보다 늦은 계약
    await user(engine, GARAM)

    await generate(engine)

    garam = await one_block(engine)
    assert garam["sales"]["areas"][0]["prev_diff"] == -100_000_000
    assert garam["lines"][1] == "34평형 1건: 8억 (11/02) · 직전 같은 평형보다 -1억"


async def test_prev_diff_zero_says_same(engine):
    run = await run_(engine, IN_WEEK)
    old = await run_(engine, LONG_AGO)
    await complex_(engine, GARAM, "가람단지", [P34])
    await sale(engine, run, 800_000_000)
    await sale(engine, old, 800_000_000, contract=date(2026, 9, 1))
    await user(engine, GARAM)

    await generate(engine)

    garam = await one_block(engine)
    assert garam["sales"]["areas"][0]["prev_diff"] == 0
    assert garam["lines"][1] == "34평형 1건: 8억 (11/02) · 직전 같은 평형과 같아요"


async def test_prev_diff_is_null_without_a_trade_within_a_year(engine):
    run = await run_(engine, IN_WEEK)
    old = await run_(engine, LONG_AGO)
    await complex_(engine, GARAM, "가람단지", [P34, P25])
    await sale(engine, run, 800_000_000, contract=date(2026, 11, 2))
    await sale(engine, old, 700_000_000, contract=date(2026, 11, 2) - timedelta(days=366))
    await sale(engine, run, 600_000_000, area=P25[1], contract=date(2026, 11, 2))
    await sale(engine, old, 500_000_000, area=P25[1], contract=date(2026, 11, 2) - timedelta(days=365))
    await user(engine, GARAM)

    await generate(engine)

    garam = await one_block(engine)
    by_bucket = {a["area_bucket"]: a["prev_diff"] for a in garam["sales"]["areas"]}
    assert by_bucket == {85: None, 60: 100_000_000}
    assert "34평형 1건: 8억 (11/02)" in garam["lines"]


async def test_prev_diff_ignores_this_weeks_new_trades(engine):
    run = await run_(engine, IN_WEEK)
    await complex_(engine, GARAM, "가람단지", [P34])
    await sale(engine, run, 800_000_000, contract=date(2026, 11, 2))
    await sale(engine, run, 700_000_000, contract=date(2026, 10, 2))
    await user(engine, GARAM)

    await generate(engine)

    garam = await one_block(engine)
    assert garam["sales"]["areas"][0]["prev_diff"] is None
    assert garam["lines"][1] == "34평형 2건: 최근 8억 (11/02)"


async def test_prev_diff_ignores_trades_first_collected_after_the_week(engine):
    run = await run_(engine, IN_WEEK)
    later = await run_(engine, WEEK_START + timedelta(days=7, hours=1))  # 주가 끝난 뒤 수집
    await complex_(engine, GARAM, "가람단지", [P34])
    await sale(engine, run, 800_000_000, contract=date(2026, 11, 2))
    await sale(engine, later, 700_000_000, contract=date(2026, 9, 1))  # 옛 계약이지만 주 뒤에 처음 봄
    await user(engine, GARAM)

    await generate(engine)

    garam = await one_block(engine)
    assert garam["sales"]["areas"][0]["prev_diff"] is None
    assert garam["lines"][1] == "34평형 1건: 8억 (11/02)"


async def test_prev_diff_rounds_symmetrically_to_10_thousand_won(engine):
    run = await run_(engine, IN_WEEK)
    old = await run_(engine, LONG_AGO)
    await complex_(engine, GARAM, "가람단지", [P34, P25])
    # -15,000원 → -2만(부호를 떼고 반올림), +15,000원 → +2만
    await sale(engine, run, 800_005_000, contract=date(2026, 11, 2))
    await sale(engine, old, 800_020_000, contract=date(2026, 9, 1))
    await sale(engine, run, 600_020_000, area=P25[1], contract=date(2026, 11, 2))
    await sale(engine, old, 600_005_000, area=P25[1], contract=date(2026, 9, 1))
    await user(engine, GARAM)

    await generate(engine)

    garam = await one_block(engine)
    by_bucket = {a["area_bucket"]: a["prev_diff"] for a in garam["sales"]["areas"]}
    assert by_bucket == {85: -20_000, 60: 20_000}
    assert "34평형 1건: 8억 (11/02) · 직전 같은 평형보다 -2만" in garam["lines"]


# ── 새 경매 ─────────────────────────────────────────────────


async def test_auction_links_to_one_complex_of_the_same_pnu(engine):
    await run_(engine, IN_WEEK)
    # 한 필지에 두 단지: 경매 전용 59.8㎡는 나래단지(25평형)에만 붙는다
    await complex_(engine, GARAM, "가람단지", [P34], pnu=PNU)
    await complex_(engine, NARAE, "나래단지", [P25], pnu=PNU)
    await auction(engine, "2026타경1", area=59.8)
    await auction(engine, "2026타경0", first_seen=LAST_WEEK)  # 지난주에 처음 봄
    await user(engine, GARAM, NARAE)

    await generate(engine)

    [row] = await reports(engine)
    assert [b["name"] for b in row.payload["complexes"]] == ["나래단지"]
    assert row.payload["quiet"] == [{"id": str(GARAM), "name": "가람단지"}]
    assert [a["id"] for a in block(row, NARAE)["auctions"]] == ["CA-2026타경1"]
    assert row.summary == "나래단지 새 경매 1건"


async def test_closed_public_and_unknown_auctions(engine):
    await run_(engine, IN_WEEK)
    await complex_(engine, GARAM, "가람단지", [P25], pnu=PNU)
    await complex_(engine, NARAE, "나래단지", [P34], pnu=OTHER_PNU)
    # 그 주 안에 닫힌 경매도 넣는다
    await auction(engine, "2026타경1", sale_date=date(2026, 12, 10), closed_at=IN_WEEK + timedelta(days=1))
    # 공매는 입찰 마감(bid_end_at, KST 날짜)
    await auction(
        engine,
        "2026-0001-001",
        source="public",
        min_price=520_000_000,
        sale_date=date(2026, 12, 3),
        bid_end_at=datetime(2026, 12, 3, 17, 0, tzinfo=KST),
    )
    # 면적·최저가·기일을 모르면 그 조각을 뺀다(평형이 안 맞아도 같은 필지 단지에 붙는다)
    await auction(engine, "2026타경9", pnu=OTHER_PNU, area=None, min_price=None, sale_date=None)
    await user(engine, GARAM, NARAE)

    await generate(engine)

    [row] = await reports(engine)
    garam = block(row, GARAM)
    assert garam["auctions"] == [
        {
            "id": "CA-2026-0001-001",
            "area_label": "25평형",
            "min_price": 520_000_000,
            "date": "2026-12-03",
            "source": "public",
        },
        {
            "id": "CA-2026타경1",
            "area_label": "25평형",
            "min_price": 410_000_000,
            "date": "2026-12-10",
            "source": "court",
        },
    ]
    # 2건 이상이면 머리줄 뒤에 건마다 한 줄
    assert garam["lines"] == [
        "새 경매 2건",
        "25평형 · 최저가 5.2억 · 입찰 마감 12/03",
        "25평형 · 최저가 4.1억 · 매각기일 12/10",
    ]
    narae = block(row, NARAE)
    assert narae["auctions"] == [
        {"id": "CA-2026타경9", "area_label": None, "min_price": None, "date": None, "source": "court"}
    ]
    assert narae["lines"] == ["새 경매 1건"]
    assert row.summary == "가람단지 새 경매 2건 외 1개 단지"


async def test_auction_area_is_exclusive_pyeong_when_supply_is_unknown(engine):
    await run_(engine, IN_WEEK)
    # 이어진 평형의 공급평형을 모르면 그 평형의 전용면적으로 앱처럼 "전용 18.1평"
    await complex_(engine, GARAM, "가람단지", [(60, 59.9, None)], pnu=PNU)
    await auction(engine, "2026타경1")
    await user(engine, GARAM)

    await generate(engine)

    garam = await one_block(engine)
    assert garam["auctions"][0]["area_label"] == "전용 18.1평"
    assert garam["lines"] == ["새 경매 1건: 전용 18.1평 · 최저가 4.1억 · 매각기일 12/10"]


# ── 정비사업 ────────────────────────────────────────────────


async def test_redevelopment_stage_change_once_per_zone_inside_the_boundary(engine):
    await run_(engine, IN_WEEK)
    await complex_(engine, GARAM, "가람단지", [P34], at=INSIDE)
    await complex_(engine, DASOM, "다솜단지", [P34], at=OUTSIDE)
    rid = await redevelopment(engine, "대연8구역", "completed", "construction", IN_WEEK, kind="redevelopment")
    # 한 구역이 경계 조각 둘로 나뉘어 둘 다 단지를 덮어도 한 번
    await zone_piece(engine, "piece-1", rid, (129.08, 35.12, 129.10, 35.14))
    await zone_piece(engine, "piece-2", rid, (129.085, 35.125, 129.095, 35.135))
    # 처음 본 구역(stage_changed_at 없음)과 지난주 변화는 넣지 않는다
    new_id = await redevelopment(engine, "새구역", "designated", None, None)
    await zone_piece(engine, "piece-3", new_id, (129.08, 35.12, 129.10, 35.14))
    last_id = await redevelopment(engine, "지난주구역", "association", "committee", LAST_WEEK)
    await zone_piece(engine, "piece-4", last_id, (129.08, 35.12, 129.10, 35.14))
    # 이어진 구역이 없는 경계 조각은 무시
    await zone_piece(engine, "piece-5", None, (129.08, 35.12, 129.10, 35.14))
    await user(engine, GARAM, DASOM)

    await generate(engine)

    [row] = await reports(engine)
    assert [b["name"] for b in row.payload["complexes"]] == ["가람단지"]
    garam = block(row, GARAM)
    assert [d["id"] for d in garam["redevelopments"]] == ["Z-대연8구역"]
    assert garam["lines"] == ["대연8구역: '이주·철거·착공' → '준공'으로 단계가 바뀌었어요"]
    assert row.summary == "가람단지 재개발 단계 변경"


async def test_redevelopment_without_previous_stage(engine):
    await run_(engine, IN_WEEK)
    await complex_(engine, GARAM, "가람단지", [P34], at=INSIDE)
    rid = await redevelopment(engine, "대연9구역", "review", None, IN_WEEK)
    await zone_piece(engine, "piece-1", rid, (129.08, 35.12, 129.10, 35.14))
    await user(engine, GARAM)

    await generate(engine)

    garam = await one_block(engine)
    assert garam["lines"] == ["대연9구역: '건축심의·통합심의' 단계가 됐어요"]
    assert garam["redevelopments"][0]["previous_stage"] is None


# ── 요약·대상·배치 ──────────────────────────────────────────


async def test_summary_prefers_sales_then_auctions_then_stage(engine):
    run = await run_(engine, IN_WEEK)
    await complex_(engine, GARAM, "가람단지", [P25], pnu=PNU)
    await complex_(engine, NARAE, "나래단지", [P34])
    await auction(engine, "2026타경1")
    rid = await redevelopment(engine, "대연8구역", "completed", "construction", IN_WEEK)
    await zone_piece(engine, "piece-1", rid, (129.08, 35.12, 129.10, 35.14))
    await sale(engine, run, 500_000_000, complex_id=NARAE)
    await user(engine, GARAM, NARAE)

    await generate(engine)

    [row] = await reports(engine)
    # 가람단지는 경매·정비사업이 있고 실거래가 없다 → 경매가 앞선다
    assert row.summary == "가람단지 새 경매 1건 외 1개 단지"
    assert row.payload["headline"] == "관심 단지 2곳 중 2곳에 새 소식이 있어요"
    garam = block(row, GARAM)
    assert garam["lines"] == [
        "새 경매 1건: 25평형 · 최저가 4.1억 · 매각기일 12/10",
        "대연8구역: '이주·철거·착공' → '준공'으로 단계가 바뀌었어요",
    ]


async def test_two_users_with_the_same_complex_get_the_same_block(engine):
    await busy_week(engine)
    other = await user(engine, GARAM)

    result = await generate(engine)

    assert (result.users, result.created) == (2, 2)
    rows = await reports(engine)
    mine = next(r for r in rows if r.user_id == other)
    theirs = next(r for r in rows if r.user_id != other)
    assert block(mine, GARAM) == block(theirs, GARAM)
    assert mine.payload["headline"] == "관심 단지 1곳 중 1곳에 새 소식이 있어요"
    assert mine.summary == "가람단지 새 실거래 5건"


async def test_report_setting_off_is_skipped_and_missing_key_is_on(engine):
    await busy_week(engine)
    off = await user(engine, GARAM, settings={"weekly_report": False})
    on = await user(engine, GARAM, settings={"weekly_report": True, "loading_tips": False})

    result = await generate(engine)

    assert (result.users, result.created) == (2, 2)
    receivers = {r.user_id for r in await reports(engine)}
    assert off not in receivers
    assert on in receivers


async def test_deleted_and_malformed_interests_are_left_out(engine):
    run = await run_(engine, IN_WEEK)
    await complex_(engine, GARAM, "가람단지", [P34])
    await sale(engine, run, 500_000_000)
    gone = uuid4()  # 지워진 단지(관심 행만 남음)
    mixed = await user(engine, GARAM)
    only_broken = await user(engine)
    async with engine.begin() as conn:
        for user_id, target in [
            (mixed, str(gone)),
            (mixed, "not-a-uuid"),
            (only_broken, str(gone)),
            (only_broken, "not-a-uuid"),
        ]:
            await conn.execute(
                text("INSERT INTO user_interests (user_id, kind, target_id) VALUES (:u, 'complex', :c)"),
                {"u": user_id, "c": target},
            )

    result = await generate(engine)

    assert result == ReportRun(users=2, created=1, skipped_empty=1, skipped_existing=0, no_collect=False)
    [row] = await reports(engine)
    assert row.user_id == mixed
    assert (row.payload["total"], row.payload["with_news"]) == (1, 1)
    assert row.payload["quiet"] == []


async def test_all_quiet_sends_nothing(engine):
    await run_(engine, IN_WEEK)
    await complex_(engine, GARAM, "가람단지", [P34], at=OUTSIDE)
    await complex_(engine, NARAE, "나래단지", [P25], pnu=PNU)
    await auction(engine, "2026타경9", pnu=OTHER_PNU)  # 관심 없는 필지
    await user(engine, GARAM, NARAE)

    result = await generate(engine)

    assert result == ReportRun(users=1, created=0, skipped_empty=1, skipped_existing=0, no_collect=False)
    assert await reports(engine) == []


async def test_rerun_keeps_one_report(engine):
    await busy_week(engine)

    first = await generate(engine)
    second = await generate(engine)

    assert (first.created, first.skipped_existing) == (1, 0)
    assert (second.created, second.skipped_existing) == (0, 1)
    assert len(await reports(engine)) == 1


async def test_no_collect_run_in_week_makes_nothing(engine, caplog):
    await run_(engine, IN_WEEK, status="failed")
    await run_(engine, IN_WEEK, job="backfill")
    await run_(engine, None, status="running")
    await run_(engine, LAST_WEEK)
    await complex_(engine, GARAM, "가람단지", [P25], pnu=PNU)
    await auction(engine, "2026타경1")
    await user(engine, GARAM)

    with caplog.at_level(logging.WARNING, logger="app.services.interest_report"):
        result = await generate(engine)

    assert result == ReportRun(users=1, created=0, skipped_empty=0, skipped_existing=0, no_collect=True)
    assert await reports(engine) == []
    assert "수집" in caplog.text


async def test_partial_collect_run_counts(engine):
    run = await run_(engine, IN_WEEK, status="partial")
    await complex_(engine, GARAM, "가람단지", [P34])
    await sale(engine, run, 500_000_000)
    await user(engine, GARAM)

    result = await generate(engine)

    assert (result.no_collect, result.created) == (False, 1)


async def test_withdrawn_users_get_nothing(engine):
    await busy_week(engine)
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE users SET deleted_at = :now"), {"now": NOW})

    result = await generate(engine)

    assert result == ReportRun(users=0, created=0, skipped_empty=0, skipped_existing=0, no_collect=False)
    assert await reports(engine) == []


async def test_report_is_a_snapshot(engine):
    user_id = await busy_week(engine)
    await generate(engine)
    [before] = await reports(engine)

    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE complexes SET name = '새이름', location = ST_SetSRID(ST_MakePoint(129.5, 35.5), 4326) "
                "WHERE id = :c"
            ),
            {"c": GARAM},
        )
        await conn.execute(text("DELETE FROM user_interests WHERE user_id = :u"), {"u": user_id})

    [after] = await reports(engine)
    assert after.payload == before.payload
    assert block(after, GARAM)["name"] == "가람단지"


async def test_old_notifications_are_cleaned(engine):
    await run_(engine, IN_WEEK)
    user_id = await user(engine)
    async with engine.begin() as conn:
        for days, period in [(91, date(2026, 8, 17)), (89, date(2026, 8, 24))]:
            await conn.execute(
                text(
                    "INSERT INTO notifications (id, user_id, kind, period_start, title, summary, payload, created_at) "
                    "VALUES (gen_random_uuid(), :u, 'interest_report', :p, 't', 's', '{}', :at)"
                ),
                {"u": user_id, "p": period, "at": NOW - timedelta(days=days)},
            )

    await generate(engine)

    assert [r.period_start for r in await reports(engine)] == [date(2026, 8, 24)]


async def test_old_notification_cleanup_has_a_created_at_index(engine):
    # 주 1회 오래된 알림 지우기(created_at < cutoff)가 알림 전체를 훑지 않게 created_at만의 인덱스를 둔다
    async with engine.connect() as conn:
        defs = (
            (await conn.execute(text("SELECT indexdef FROM pg_indexes WHERE tablename = 'notifications'")))
            .scalars()
            .all()
        )
    assert any(d.endswith("USING btree (created_at)") for d in defs), defs


async def test_week_must_be_a_monday(engine):
    with pytest.raises(ValueError) as e:
        await generate(engine, week=date(2026, 11, 17))
    assert not isinstance(e.value, ReportWeekNotOver)


async def test_unfinished_week_is_refused(engine):
    await busy_week(engine)
    # 이번 주(11/23~)는 아직 끝나지 않았다. 일부 데이터로 리포트가 굳지 않게 막는다
    with pytest.raises(ReportWeekNotOver):
        await generate(engine, week=date(2026, 11, 23))
    assert await reports(engine) == []

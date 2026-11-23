"""관심 지역 주간 리포트 계산·생성 (실제 PostGIS, 가짜 데이터)."""

import logging
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from app.core.dates import KST
from app.db import migrate
from app.db.engine import make_engine
from app.services.region_report import ReportRun, format_price, generate_region_reports, week_start
from tests.conftest import create_database

pytestmark = pytest.mark.anyio

# 리포트 주: 2026-11-16(월) 00:00 ~ 11-23(월) 00:00 KST. 만드는 시각은 11-23 08:00
WEEK = date(2026, 11, 16)
NOW = datetime(2026, 11, 23, 8, 0, tzinfo=KST)
WEEK_START = datetime(2026, 11, 16, tzinfo=KST)
IN_WEEK = datetime(2026, 11, 18, 4, 0, tzinfo=KST)
LAST_WEEK = datetime(2026, 11, 11, 4, 0, tzinfo=KST)

DAEYEON = "2629010600"  # 남구 대연동
YONGHO = "2629010700"  # 남구 용호동
MUNHYEON = "2629010900"  # 남구 문현동
HILLSTATE = UUID("7a000000-0000-4000-8000-000000000001")
# 30평(전용 99.174㎡)이라 평당가 = 가격 / 30
AREA_30 = 99.174


# ── 테스트 데이터 ──────────────────────────────────────────


@pytest.fixture(scope="module")
def report_db(postgis: str) -> str:
    url = create_database(postgis, "region_report")
    migrate.upgrade(url)
    return url


@pytest.fixture
async def engine(report_db):
    engine = make_engine(report_db)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE notifications, user_interests, users, transactions, complexes, auctions, redevelopments, "
                "etl_runs RESTART IDENTITY CASCADE"
            )
        )
        # 스냅샷 테스트가 바꾼 이름·대표점을 매번 되돌린다
        await conn.execute(
            text(
                "UPDATE regions SET name = '대연동', centroid = ST_SetSRID(ST_MakePoint(129.09, 35.13), 4326) "
                "WHERE code = :c"
            ),
            {"c": DAEYEON},
        )
        await conn.execute(
            text("UPDATE regions SET centroid = NULL WHERE code IN (:y, :m)"), {"y": YONGHO, "m": MUNHYEON}
        )
        await conn.execute(
            text("UPDATE regions SET centroid = ST_SetSRID(ST_MakePoint(129.08, 35.12), 4326) WHERE code = '26290'")
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


async def complex_(engine, complex_id: UUID, name: str, code: str, areas: list[tuple[int, float, float | None]]):
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
                INSERT INTO complexes (id, property_type, name, name_key, region_code, jibun, location, location_source)
                VALUES (:id, 'apartment', :name, :name, :code, '1', ST_SetSRID(ST_MakePoint(129.09, 35.13), 4326),
                        'geocode')
                """
            ),
            {"id": complex_id, "name": name, "code": code},
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
    code: str = DAEYEON,
    area: float = AREA_30,
    contract: date = date(2026, 11, 2),
    complex_id: UUID | None = None,
    property_type: str = "apartment",
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
                VALUES (gen_random_uuid(), :key, 'apt_trade', :ptype, :deal, :complex_id, :code,
                    ST_SetSRID(ST_MakePoint(129.09, 35.13), 4326), 'parcel', :price, :deposit, :area, :contract,
                    :cancelled, :deleted, :run, :run)
                """
            ),
            {
                "key": uuid4().hex,
                "ptype": property_type,
                "deal": deal_type,
                "complex_id": complex_id,
                "code": code,
                "price": None if jeonse else price,
                "deposit": price if jeonse else None,
                "area": area,
                "contract": contract,
                "cancelled": cancelled,
                "deleted": NOW if deleted else None,
                "run": run,
            },
        )


async def auction(engine, case: str, min_price: int | None, first_seen: datetime, *, code: str = DAEYEON):
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
                INSERT INTO auctions (id, source, case_no, address, region_code, min_price, list_hash,
                                      first_seen_at, last_seen_at)
                VALUES (:id, 'court', :case, '부산 남구 대연동 1', :code, :price, 'h', :seen, :seen)
                """
            ),
            {"id": f"CA-{case}", "case": case, "code": code, "price": min_price, "seen": first_seen},
        )


async def zone(
    engine,
    name: str,
    stage: str,
    previous: str | None,
    changed_at: datetime | None,
    *,
    code: str | None = DAEYEON,
):
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
                INSERT INTO redevelopments (id, area_name, kind, stage, previous_stage, stage_changed_at, region_code,
                                            first_seen_at, last_seen_at)
                VALUES (:id, :name, 'redevelopment', :stage, :prev, :changed, :code, :seen, :seen)
                """
            ),
            {
                "id": f"Z-{name}",
                "name": name,
                "stage": stage,
                "prev": previous,
                "changed": changed_at,
                "code": code,
                "seen": LAST_WEEK - timedelta(days=100),
            },
        )


async def user(engine, *regions: str, deleted: bool = False) -> UUID:
    user_id = uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO users (id, nickname, agreed_at, created_at, deleted_at) VALUES (:id, 'n', :t, :t, :del)"),
            {"id": user_id, "t": NOW - timedelta(days=200), "del": NOW if deleted else None},
        )
        for code in regions:
            await conn.execute(
                text("INSERT INTO user_interests (user_id, kind, target_id) VALUES (:u, 'region', :c)"),
                {"u": user_id, "c": code},
            )
    return user_id


async def reports(engine) -> list:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text("SELECT user_id, period_start, title, summary, payload FROM notifications ORDER BY created_at")
            )
        ).all()


async def generate(engine, week: date = WEEK) -> ReportRun:
    return await generate_region_reports(engine, week=week, now=NOW)


def region(row, code: str) -> dict:
    return next(r for r in row.payload["regions"] if r["code"] == code)


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
    assert format_price(100_000_000) == "1억"
    assert format_price(41_000_000) == "4,100만"
    assert format_price(99_995_000) == "10,000만"


# ── 리포트 ─────────────────────────────────────────────────


async def busy_week(engine) -> UUID:
    """예시 그대로의 한 주: 대연동 새 거래 12건·경매 2건·재개발 단계 변화 1곳, 나머지 두 동네는 무소식."""
    this_week = await run_(engine, IN_WEEK)
    last_week = await run_(engine, LAST_WEEK)
    await complex_(engine, HILLSTATE, "대연힐스테이트", DAEYEON, [(85, 84.97, 33.5)])
    # 평당 2,000만~2,250만 11건(중위 2,150만) + 가장 비싼 거래 1건(34평형 9.8억)
    for per_pyeong in [2000, 2050, 2100, 2120, 2140, 2150, 2150, 2160, 2180, 2200, 2250]:
        await sale(engine, this_week, per_pyeong * 10_000 * 30, contract=date(2026, 11, 10))
    await sale(engine, this_week, 980_000_000, area=84.97, complex_id=HILLSTATE, contract=date(2026, 11, 2))
    # 지난주 3건(중위 2,125만) → +1.2%
    for per_pyeong in [2100, 2125, 2160]:
        await sale(engine, last_week, per_pyeong * 10_000 * 30, contract=date(2026, 11, 1))
    await auction(engine, "2026타경1", 410_000_000, IN_WEEK)
    await auction(engine, "2026타경2", 520_000_000, WEEK_START)
    await auction(engine, "2026타경3", 100_000_000, LAST_WEEK)  # 지난주에 처음 봄
    await zone(engine, "대연8구역", "disposal_approved", "project_approved", IN_WEEK)
    return await user(engine, YONGHO, DAEYEON, MUNHYEON)


async def test_report_lines_for_a_busy_week(engine):
    user_id = await busy_week(engine)

    result = await generate(engine)

    assert result == ReportRun(users=1, created=1, skipped_empty=0, skipped_existing=0, no_collect=False)
    [row] = await reports(engine)
    assert (row.user_id, row.period_start) == (user_id, WEEK)
    assert row.title == "관심 지역 주간 리포트 · 11/16~11/22"
    assert row.summary == "대연동 거래 12건 · 새 경매 2건 외 2개 동네"
    payload = row.payload
    assert payload["period"] == {"start": "2026-11-16", "end": "2026-11-22"}
    assert [r["name"] for r in payload["regions"]] == ["대연동", "문현동", "용호동"]  # 이름순
    assert payload["regions"][0] == {
        "code": DAEYEON,
        "name": "대연동",
        "sigungu_name": "남구",
        "lat": pytest.approx(35.13),
        "lng": pytest.approx(129.09),
        "empty": False,
        "lines": [
            "아파트 매매 12건 새로 신고 · 평당 중위 2,150만 (지난주보다 +1.2%)",
            "이번 주 가장 비싼 거래: 대연힐스테이트 34평형 9.8억 (계약 11/02)",
            "새 경매 2건 · 최저가 4.1억부터",
            "재개발: 대연8구역이 '관리처분계획인가'로 단계가 바뀌었어요",
        ],
        "sales": {"count": 12, "median_per_pyeong": 21_500_000, "change_pct": 1.2},
        "top_sale": {
            "complex_name": "대연힐스테이트",
            "area_label": "34평형",
            "price": 980_000_000,
            "contract_date": "2026-11-02",
        },
        "auctions": {"count": 2, "min_price": 410_000_000},
        "redevelopments": [{"area_name": "대연8구역", "stage": "disposal_approved"}],
    }
    quiet = region(row, YONGHO)
    assert (quiet["empty"], quiet["lines"], quiet["sigungu_name"]) == (True, ["이번 주 새 소식이 없어요"], "남구")
    # 대표점이 없는 동은 시군구 대표점을 쓴다
    assert (quiet["lat"], quiet["lng"]) == (pytest.approx(35.12), pytest.approx(129.08))
    assert quiet["sales"] == {"count": 0, "median_per_pyeong": None, "change_pct": None}
    assert (quiet["top_sale"], quiet["auctions"], quiet["redevelopments"]) == (
        None,
        {"count": 0, "min_price": None},
        [],
    )
    assert payload["sources"] == [
        "국토교통부 실거래가 공개시스템",
        "대법원 법원경매정보·온비드",
        "부산광역시 정비사업 정보",
    ]
    assert payload["lag_notice"] == "최근 30일 거래는 아직 신고되지 않았을 수 있어요"


async def test_change_is_omitted_under_three_trades(engine):
    this_week = await run_(engine, IN_WEEK)
    last_week = await run_(engine, LAST_WEEK)
    for per_pyeong in [2000, 2100]:
        await sale(engine, this_week, per_pyeong * 10_000 * 30)
    for per_pyeong in [1900, 2000, 2100, 2200]:
        await sale(engine, last_week, per_pyeong * 10_000 * 30)
    await user(engine, DAEYEON)

    await generate(engine)

    [row] = await reports(engine)
    daeyeon = region(row, DAEYEON)
    assert daeyeon["lines"][0] == "아파트 매매 2건 새로 신고 · 평당 중위 2,050만"
    assert daeyeon["sales"] == {"count": 2, "median_per_pyeong": 20_500_000, "change_pct": None}
    assert row.summary == "대연동 거래 2건"


async def test_change_is_omitted_when_last_week_had_under_three_trades(engine):
    this_week = await run_(engine, IN_WEEK)
    last_week = await run_(engine, LAST_WEEK)
    for per_pyeong in [2000, 2100, 2200]:
        await sale(engine, this_week, per_pyeong * 10_000 * 30)
    for per_pyeong in [1000, 1100]:
        await sale(engine, last_week, per_pyeong * 10_000 * 30)
    await user(engine, DAEYEON)

    await generate(engine)

    [row] = await reports(engine)
    assert region(row, DAEYEON)["lines"][0] == "아파트 매매 3건 새로 신고 · 평당 중위 2,100만"
    assert region(row, DAEYEON)["sales"]["change_pct"] is None


async def test_week_boundary_uses_run_finished_at_kst(engine):
    before = await run_(engine, WEEK_START - timedelta(seconds=1))
    after = await run_(engine, WEEK_START + timedelta(seconds=1))
    next_week = await run_(engine, WEEK_START + timedelta(days=7))
    await sale(engine, before, 500_000_000)
    await sale(engine, after, 600_000_000)
    await sale(engine, next_week, 700_000_000)
    await user(engine, DAEYEON)

    await generate(engine)

    [row] = await reports(engine)
    daeyeon = region(row, DAEYEON)
    assert daeyeon["sales"]["count"] == 1
    assert daeyeon["top_sale"]["price"] == 600_000_000


async def test_backfill_and_old_contracts_are_not_new(engine):
    collect = await run_(engine, IN_WEEK)
    backfill = await run_(engine, IN_WEEK, job="backfill")
    await sale(engine, backfill, 900_000_000)
    await sale(engine, collect, 800_000_000, contract=WEEK - timedelta(days=61))
    # 계약일이 오래돼도 60일 안이고 이번 주 수집이 처음 봤으면 새 거래다
    await sale(engine, collect, 500_000_000, contract=WEEK - timedelta(days=60))
    await user(engine, DAEYEON)

    await generate(engine)

    [row] = await reports(engine)
    daeyeon = region(row, DAEYEON)
    assert daeyeon["sales"]["count"] == 1
    assert daeyeon["top_sale"]["price"] == 500_000_000
    assert daeyeon["top_sale"]["contract_date"] == "2026-09-17"


async def test_cancelled_and_deleted_trades_are_excluded(engine):
    run = await run_(engine, IN_WEEK)
    await sale(engine, run, 500_000_000)
    await sale(engine, run, 900_000_000, cancelled=True)
    await sale(engine, run, 800_000_000, deleted=True)
    await sale(engine, run, 700_000_000, property_type="officetel")
    await sale(engine, run, 600_000_000, deal_type="jeonse")
    await user(engine, DAEYEON)

    await generate(engine)

    [row] = await reports(engine)
    daeyeon = region(row, DAEYEON)
    assert daeyeon["sales"]["count"] == 1
    assert daeyeon["top_sale"]["price"] == 500_000_000
    # 단지를 모르는 거래는 단지명 없이, 전용평으로 표기한다(99.174㎡ = 30평)
    assert daeyeon["top_sale"]["complex_name"] is None
    assert daeyeon["lines"][1] == "이번 주 가장 비싼 거래: 30평형 5억 (계약 11/02)"


async def test_new_redevelopment_is_not_a_stage_change(engine):
    await run_(engine, IN_WEEK)
    await zone(engine, "새구역", "designated", None, None)  # 이번 주 처음 본 구역
    await zone(engine, "지난주구역", "association", "committee", LAST_WEEK)
    await zone(engine, "동모름구역", "review", "association", IN_WEEK, code=None)
    await zone(engine, "문현1구역", "completed", "construction", IN_WEEK, code=MUNHYEON)
    await user(engine, DAEYEON, MUNHYEON)

    await generate(engine)

    [row] = await reports(engine)
    assert region(row, DAEYEON)["empty"] is True
    munhyeon = region(row, MUNHYEON)
    assert munhyeon["redevelopments"] == [{"area_name": "문현1구역", "stage": "completed"}]
    assert munhyeon["lines"] == ["재개발: 문현1구역이 '준공'으로 단계가 바뀌었어요"]
    assert row.summary == "문현동 재개발 1곳 단계 변화 외 1개 동네"


async def test_all_quiet_sends_nothing(engine):
    await run_(engine, IN_WEEK)
    await auction(engine, "2026타경9", 100_000_000, IN_WEEK, code=YONGHO)  # 관심 없는 동네
    await user(engine, DAEYEON, MUNHYEON)

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
    await auction(engine, "2026타경1", 410_000_000, IN_WEEK)
    await user(engine, DAEYEON)

    with caplog.at_level(logging.WARNING, logger="app.services.region_report"):
        result = await generate(engine)

    assert result.no_collect is True
    assert result.created == 0
    assert await reports(engine) == []
    assert "수집" in caplog.text


async def test_partial_collect_run_counts(engine):
    run = await run_(engine, IN_WEEK, status="partial")
    await sale(engine, run, 500_000_000)
    await user(engine, DAEYEON)

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
                "UPDATE regions SET name = '새이름동', centroid = ST_SetSRID(ST_MakePoint(129.5, 35.5), 4326) WHERE code = :c"
            ),
            {"c": DAEYEON},
        )
        await conn.execute(text("DELETE FROM user_interests WHERE user_id = :u"), {"u": user_id})

    [after] = await reports(engine)
    assert after.payload == before.payload
    assert region(after, DAEYEON)["name"] == "대연동"
    assert region(after, DAEYEON)["lat"] == pytest.approx(35.13)


async def test_old_notifications_are_cleaned(engine):
    await run_(engine, IN_WEEK)
    user_id = await user(engine, DAEYEON)
    async with engine.begin() as conn:
        for days, period in [(91, date(2026, 8, 17)), (89, date(2026, 8, 24))]:
            await conn.execute(
                text(
                    "INSERT INTO notifications (id, user_id, kind, period_start, title, summary, payload, created_at) "
                    "VALUES (gen_random_uuid(), :u, 'region_report', :p, 't', 's', '{}', :at)"
                ),
                {"u": user_id, "p": period, "at": NOW - timedelta(days=days)},
            )

    await generate(engine)

    assert [r.period_start for r in await reports(engine)] == [date(2026, 8, 24)]


async def test_week_must_be_a_monday(engine):
    with pytest.raises(ValueError):
        await generate(engine, week=date(2026, 11, 17))


async def test_unfinished_week_is_refused(engine):
    await run_(engine, IN_WEEK)
    # 이번 주(11/23~)는 아직 끝나지 않았다. 일부 데이터로 리포트가 굳지 않게 막는다
    with pytest.raises(ValueError):
        await generate(engine, week=date(2026, 11, 23))
    assert await reports(engine) == []


async def test_zero_price_trades_are_not_counted(engine):
    run = await run_(engine, IN_WEEK)
    await sale(engine, run, 0)
    await sale(engine, run, 500_000_000)
    await user(engine, DAEYEON)

    await generate(engine)

    [row] = await reports(engine)
    daeyeon = region(row, DAEYEON)
    assert daeyeon["sales"]["count"] == 1
    assert daeyeon["sales"]["median_per_pyeong"] == 16_670_000


async def test_only_zero_price_trades_make_no_news(engine):
    run = await run_(engine, IN_WEEK)
    await sale(engine, run, 0)
    await user(engine, DAEYEON)

    result = await generate(engine)

    assert result.skipped_empty == 1
    assert await reports(engine) == []

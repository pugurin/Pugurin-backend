"""관심 지역 주간 리포트: 지난주(KST 월~일) 동네별 새 소식을 모아 회원마다 알림 한 통을 만든다.

정의는 docs/INTEREST_REGION_REPORT.md §4. 만든 리포트는 값 그대로 저장하므로(동 이름·대표점 포함)
나중에 지역 정보가 바뀌거나 관심 지역을 해제해도 그대로 남는다.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.dates import KST
from app.core.geo import SQM_PER_PYEONG
from app.repositories.postgres.market import _PYEONG, _supply_pyeong
from app.repositories.postgres.notifications import PostgresNotificationRepository
from app.repositories.types import AreaType, Complex, PropertyType, RedevelopmentStage

logger = logging.getLogger(__name__)

RETENTION = timedelta(days=90)  # 알림 보관 기간
NEW_CONTRACT_DAYS = 60  # 계약일이 주 시작보다 이만큼 넘게 이전이면 '새 거래'가 아니다(백필·재적재)
MIN_COMPARE_TRADES = 3  # 지난주 대비는 두 주 모두 이 건수 이상일 때만
SOURCES = ["국토교통부 실거래가 공개시스템", "대법원 법원경매정보·온비드", "부산광역시 정비사업 정보"]
LAG_NOTICE = "최근 30일 거래는 아직 신고되지 않았을 수 있어요"
QUIET_LINE = "이번 주 새 소식이 없어요"

# 프론트 REDEVELOPMENT_STEPS의 label·진행 표시 밖 단계 이름과 같은 말(src/state/redevelopment.ts)
STAGE_LABEL: dict[RedevelopmentStage, str] = {
    RedevelopmentStage.planned: "정비예정구역·기본계획",
    RedevelopmentStage.designated: "정비계획 수립·구역 지정",
    RedevelopmentStage.committee: "추진위원회 구성",
    RedevelopmentStage.association: "조합설립인가",
    RedevelopmentStage.review: "건축심의·통합심의",
    RedevelopmentStage.project_approved: "사업시행계획인가",
    RedevelopmentStage.disposal_approved: "관리처분계획인가",
    RedevelopmentStage.construction: "이주·철거·착공",
    RedevelopmentStage.completed: "준공",
    RedevelopmentStage.cancelled: "해제",
    RedevelopmentStage.transferred: "이전고시",
    RedevelopmentStage.dissolved: "조합해산",
}


@dataclass(frozen=True)
class ReportRun:
    users: int  # 관심 지역이 있는 탈퇴하지 않은 회원 수
    created: int
    skipped_empty: int  # 모든 관심 지역이 무소식이라 안 만든 회원 수
    skipped_existing: int  # 그 주 리포트가 이미 있던 회원 수
    no_collect: bool  # 그 주에 끝난 실거래 수집이 없어 아무것도 만들지 않음


# ── 주 계산 ───────────────────────────────────────────────


def week_start(now: datetime) -> date:
    """직전에 끝난 주(KST 월 00:00 ~ 다음 월 00:00)의 월요일."""
    today = now.astimezone(KST).date()
    return today - timedelta(days=today.weekday() + 7)


def _bounds(week: date) -> tuple[datetime, datetime]:
    start = datetime.combine(week, time(0), tzinfo=KST)
    return start, start + timedelta(days=7)


# ── 표기(프론트 state/market.ts와 같은 규칙) ─────────────────


def _round_half_up(value: float) -> int:
    return math.floor(value + 0.5)


def format_price(won: int) -> str:
    """억 이상은 소수 1자리(.0 생략) '9.8억', 그 아래는 '4,100만'."""
    if won >= 100_000_000:
        return f"{won / 100_000_000:.1f}".removesuffix(".0") + "억"
    return f"{_round_half_up(won / 10_000):,}만"


def _format_man(won: int) -> str:
    return f"{_round_half_up(won / 10_000):,}만"


def _format_change(pct: float) -> str:
    return "0.0%" if pct == 0 else f"{pct:+.1f}%"


def _has_final(word: str) -> bool:
    """마지막 글자에 받침이 있는지. 한글이 아니면 있다고 본다."""
    ch = word[-1:] or "가"
    if not "가" <= ch <= "힣":
        return True
    return (ord(ch) - 0xAC00) % 28 != 0


def _subject(word: str) -> str:
    return word + ("이" if _has_final(word) else "가")


def _toward(quoted: str, word: str) -> str:
    """'단계'로 / '준공'으로. ㄹ 받침 뒤는 '로'."""
    ch = word[-1:]
    final = (ord(ch) - 0xAC00) % 28 if "가" <= ch <= "힣" else 0
    return quoted + ("으로" if final not in (0, 8) else "로")


def _stage_label(stage: str, step_raw: str | None) -> str:
    try:
        label = STAGE_LABEL.get(RedevelopmentStage(stage))
    except ValueError:
        label = None
    return label or step_raw or "단계 미상"


# ── SQL ────────────────────────────────────────────────────

# 그 주에 처음 수집된(정기 수집이 끝난) 새 아파트 매매. :start·:end는 실행 종료 시각, :min_contract는 계약일 하한
_NEW_SALE_FROM = "FROM transactions t JOIN etl_runs r ON r.id = t.first_seen_run_id"
_NEW_SALE_WHERE = """
    WHERE t.property_type = 'apartment' AND t.deal_type = 'sale' AND NOT t.is_cancelled AND t.deleted_at IS NULL
      AND t.region_code = ANY(:codes)
      AND r.job = 'collect' AND r.status IN ('success', 'partial')
      AND r.finished_at >= :start AND r.finished_at < :end
      AND t.contract_date >= :min_contract
"""
_SALES = text(
    f"""
    SELECT t.region_code AS code, count(*) AS n,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY t.price::float8 / {_PYEONG})
               FILTER (WHERE t.basis_area_m2 > 0) AS median
    {_NEW_SALE_FROM}
    {_NEW_SALE_WHERE}
    GROUP BY 1
    """
)
_TOP_SALES = text(
    f"""
    SELECT DISTINCT ON (t.region_code) t.region_code AS code, t.price, t.contract_date,
           t.exclusive_area_m2::float8 AS area, t.complex_id,
           c.name AS complex_name, c.property_type, c.region_code AS complex_region, c.jibun, c.pnu,
           ST_Y(c.location) AS lat, ST_X(c.location) AS lng, c.build_year, c.household_count
    {_NEW_SALE_FROM} LEFT JOIN complexes c ON c.id = t.complex_id
    {_NEW_SALE_WHERE}
    ORDER BY t.region_code, t.price DESC, t.contract_date DESC, t.id
    """
)
_AREA_TYPES = text(
    """
    SELECT complex_id, exclusive_area_m2::float8 AS area, supply_area_pyeong::float8 AS supply
    FROM complex_area_types WHERE complex_id = ANY(:ids) ORDER BY complex_id, area_bucket
    """
)
# 그 주에 처음 본 경매(그 주 안에 닫힌 것도 센다)
_AUCTIONS = text(
    """
    SELECT region_code AS code, count(*) AS n, min(min_price) AS min_price
    FROM auctions
    WHERE region_code = ANY(:codes) AND first_seen_at >= :start AND first_seen_at < :end
    GROUP BY 1
    """
)
# 그 주에 단계가 바뀐 구역. previous_stage가 없으면 처음 본 구역이라 넣지 않는다
_REDEVELOPMENTS = text(
    """
    SELECT region_code AS code, area_name, stage, step_raw
    FROM redevelopments
    WHERE region_code = ANY(:codes) AND previous_stage IS NOT NULL
      AND stage_changed_at >= :start AND stage_changed_at < :end
    ORDER BY area_name, id
    """
)
_HAS_COLLECT = text(
    """
    SELECT EXISTS (
        SELECT 1 FROM etl_runs
        WHERE job = 'collect' AND status IN ('success', 'partial') AND finished_at >= :start AND finished_at < :end
    )
    """
)
# 관심 지역이 있는 탈퇴하지 않은 회원과 그 동네(대표점이 없으면 시군구 대표점)
_SUBSCRIBERS = text(
    """
    SELECT i.user_id, g.code, g.name, p.name AS sigungu_name,
           ST_Y(coalesce(g.centroid, p.centroid)) AS lat, ST_X(coalesce(g.centroid, p.centroid)) AS lng
    FROM user_interests i
    JOIN users u ON u.id = i.user_id AND u.deleted_at IS NULL
    JOIN regions g ON g.code = i.target_id AND g.level = 'dong'
    LEFT JOIN regions p ON p.code = g.parent_code
    WHERE i.kind = 'region'
    """
)


@dataclass(frozen=True)
class _Region:
    code: str
    name: str
    sigungu_name: str | None
    lat: float | None
    lng: float | None


@dataclass
class _Facts:
    """한 주 동안 동네별로 모은 원값."""

    sales: dict[str, tuple[int, float | None]]  # code → (건수, 평당 중위)
    previous_sales: dict[str, tuple[int, float | None]]
    top_sales: dict[str, dict]
    auctions: dict[str, tuple[int, int | None]]
    redevelopments: dict[str, list[dict]]


async def _sales(conn, codes: list[str], week: date) -> dict[str, tuple[int, float | None]]:
    start, end = _bounds(week)
    params = {"codes": codes, "start": start, "end": end, "min_contract": week - timedelta(days=NEW_CONTRACT_DAYS)}
    return {r.code: (r.n, r.median) for r in await conn.execute(_SALES, params)}


async def _top_sales(conn, codes: list[str], week: date) -> dict[str, dict]:
    start, end = _bounds(week)
    params = {"codes": codes, "start": start, "end": end, "min_contract": week - timedelta(days=NEW_CONTRACT_DAYS)}
    rows = (await conn.execute(_TOP_SALES, params)).all()
    complex_ids = [r.complex_id for r in rows if r.complex_id is not None]
    areas: dict[UUID, list[AreaType]] = defaultdict(list)
    if complex_ids:
        for a in await conn.execute(_AREA_TYPES, {"ids": complex_ids}):
            areas[a.complex_id].append(AreaType(a.area, a.supply))
    top = {}
    for r in rows:
        complex_ = None
        if r.complex_id is not None:
            # 평형 표기(_supply_pyeong)에만 쓰는 단지 값
            complex_ = Complex(
                id=r.complex_id,
                property_type=PropertyType(r.property_type),
                name=r.complex_name,
                address=r.jibun,
                region_code=r.complex_region,
                pnu=r.pnu,
                lat=r.lat,
                lng=r.lng,
                build_year=r.build_year,
                household_count=r.household_count,
                area_types=tuple(areas.get(r.complex_id, ())),
            )
        pyeong = _supply_pyeong(complex_, r.area)
        if pyeong is None and r.area is not None:
            pyeong = r.area / SQM_PER_PYEONG
        top[r.code] = {
            "complex_name": r.complex_name,
            "area_label": None if pyeong is None else f"{_round_half_up(pyeong)}평형",
            "price": r.price,
            "contract_date": r.contract_date.isoformat(),
        }
    return top


async def _collect(engine: AsyncEngine, codes: list[str], week: date) -> _Facts:
    start, end = _bounds(week)
    async with engine.connect() as conn:
        sales = await _sales(conn, codes, week)
        previous = await _sales(conn, codes, week - timedelta(days=7))
        top = await _top_sales(conn, codes, week)
        auctions = {
            r.code: (r.n, r.min_price)
            for r in await conn.execute(_AUCTIONS, {"codes": codes, "start": start, "end": end})
        }
        zones: dict[str, list[dict]] = defaultdict(list)
        for r in await conn.execute(_REDEVELOPMENTS, {"codes": codes, "start": start, "end": end}):
            zones[r.code].append({"area_name": r.area_name, "stage": r.stage, "step_raw": r.step_raw})
    return _Facts(sales, previous, top, auctions, zones)


# ── 리포트 조립 ─────────────────────────────────────────────


def _change_pct(now: tuple[int, float | None], before: tuple[int, float | None] | None) -> float | None:
    if before is None or now[0] < MIN_COMPARE_TRADES or before[0] < MIN_COMPARE_TRADES:
        return None
    if not now[1] or not before[1]:
        return None
    pct = round((now[1] / before[1] - 1) * 100, 1)
    return pct or 0.0  # -0.0을 0.0으로


def _region_block(region: _Region, facts: _Facts) -> dict:
    count, median = facts.sales.get(region.code, (0, None))
    change = _change_pct((count, median), facts.previous_sales.get(region.code)) if count else None
    median_won = None if median is None else _round_half_up(median / 10_000) * 10_000
    top = facts.top_sales.get(region.code) if count else None
    auction_count, auction_min = facts.auctions.get(region.code, (0, None))
    zones = facts.redevelopments.get(region.code, [])

    lines = []
    if count:
        line = f"아파트 매매 {count}건 새로 신고"
        if median_won is not None:
            line += f" · 평당 중위 {_format_man(median_won)}"
        if change is not None:
            line += f" (지난주보다 {_format_change(change)})"
        lines.append(line)
    if top:
        what = " ".join(x for x in (top["complex_name"], top["area_label"], format_price(top["price"])) if x)
        contract = date.fromisoformat(top["contract_date"])
        lines.append(f"이번 주 가장 비싼 거래: {what} (계약 {contract:%m/%d})")
    if auction_count:
        line = f"새 경매 {auction_count}건"
        if auction_min is not None:
            line += f" · 최저가 {format_price(auction_min)}부터"
        lines.append(line)
    for z in zones:
        label = _stage_label(z["stage"], z["step_raw"])
        stage = _toward("'" + label + "'", label)
        lines.append(f"재개발: {_subject(z['area_name'])} {stage} 단계가 바뀌었어요")

    empty = not lines
    return {
        "code": region.code,
        "name": region.name,
        "sigungu_name": region.sigungu_name,
        "lat": region.lat,
        "lng": region.lng,
        "empty": empty,
        "lines": [QUIET_LINE] if empty else lines,
        "sales": {"count": count, "median_per_pyeong": median_won, "change_pct": change},
        "top_sale": top,
        "auctions": {"count": auction_count, "min_price": auction_min},
        "redevelopments": [{"area_name": z["area_name"], "stage": z["stage"]} for z in zones],
    }


def _summary(blocks: list[dict]) -> str:
    """소식 있는 첫 동네의 요점 + 나머지 동네 수."""
    first = next(b for b in blocks if not b["empty"])
    parts = []
    if first["sales"]["count"]:
        parts.append(f"거래 {first['sales']['count']}건")
    if first["auctions"]["count"]:
        parts.append(f"새 경매 {first['auctions']['count']}건")
    if not parts:
        parts.append(f"재개발 {len(first['redevelopments'])}곳 단계 변화")
    summary = f"{first['name']} " + " · ".join(parts)
    others = len(blocks) - 1
    return summary + (f" 외 {others}개 동네" if others else "")


def _build_report(week: date, regions: list[_Region], facts: _Facts) -> tuple[str, str, dict] | None:
    """(제목, 요약, payload). 모든 동네가 무소식이면 None."""
    ordered = sorted(regions, key=lambda r: (r.name, r.sigungu_name or "", r.code))
    blocks = [_region_block(r, facts) for r in ordered]
    if all(b["empty"] for b in blocks):
        return None
    end = week + timedelta(days=6)
    title = f"관심 지역 주간 리포트 · {week:%m/%d}~{end:%m/%d}"
    payload = {
        "period": {"start": week.isoformat(), "end": end.isoformat()},
        "regions": blocks,
        "sources": SOURCES,
        "lag_notice": LAG_NOTICE,
    }
    return title, _summary(blocks), payload


# ── 배치 ───────────────────────────────────────────────────


async def generate_region_reports(engine: AsyncEngine, *, week: date, now: datetime) -> ReportRun:
    """그 주(week = 월요일) 리포트를 회원마다 한 통씩 만든다. 이미 있으면 덮지 않는다."""
    if week.weekday() != 0:
        raise ValueError(f"리포트 주는 월요일이어야 합니다: {week}")
    repo = PostgresNotificationRepository(engine)
    await repo.delete_older_than(now - RETENTION)

    start, end = _bounds(week)
    async with engine.connect() as conn:
        subscribers = (await conn.execute(_SUBSCRIBERS)).all()
        has_collect = (await conn.execute(_HAS_COLLECT, {"start": start, "end": end})).scalar_one()
    by_user: dict[UUID, list[_Region]] = defaultdict(list)
    for r in subscribers:
        by_user[r.user_id].append(_Region(r.code, r.name, r.sigungu_name, r.lat, r.lng))

    if not has_collect:
        # 수집이 없던 주를 "새 소식 없음"으로 잘못 보내지 않는다
        logger.warning("%s 주에 끝난 실거래 수집(collect)이 없어 관심 지역 리포트를 만들지 않습니다", week)
        return ReportRun(users=len(by_user), created=0, skipped_empty=0, skipped_existing=0, no_collect=True)

    codes = sorted({region.code for regions in by_user.values() for region in regions})
    facts = await _collect(engine, codes, week) if codes else _Facts({}, {}, {}, {}, {})
    created = skipped_empty = skipped_existing = 0
    for user_id, regions in by_user.items():
        report = _build_report(week, regions, facts)
        if report is None:
            skipped_empty += 1
            continue
        title, summary, payload = report
        if await repo.insert_report(user_id, week, title, summary, payload, now):
            created += 1
        else:
            skipped_existing += 1
    return ReportRun(len(by_user), created, skipped_empty, skipped_existing, no_collect=False)

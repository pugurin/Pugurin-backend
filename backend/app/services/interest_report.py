"""관심 단지 주간 리포트: 지난주(KST 월~일) 관심 단지별 새 소식을 모아 회원마다 알림 한 통을 만든다.

정의는 docs/INTEREST_COMPLEX_REPORT.md §4. 만든 리포트는 값 그대로 저장하므로(단지 이름·동 이름·대표점 포함)
나중에 단지 정보가 바뀌거나 관심 단지를 해제해도 그대로 남는다.
계산은 모든 회원의 관심 단지 id에 대해 한 번 하고 회원마다 나눠 쓴다(같은 단지는 누구에게나 같은 숫자).
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.dates import KST
from app.core.geo import SQM_PER_PYEONG
from app.repositories.postgres.auctions import AREA_TOLERANCE_M2, AUCTION_COMPLEX_LINK
from app.repositories.postgres.notifications import PostgresNotificationRepository
from app.repositories.types import AuctionSource, RedevelopmentKind, RedevelopmentStage

logger = logging.getLogger(__name__)

RETENTION = timedelta(days=90)  # 알림 보관 기간
NEW_CONTRACT_DAYS = 60  # 계약일이 주 시작보다 이만큼 넘게 이전이면 '새 거래'가 아니다(백필·재적재)
MAX_AREA_LINES = 3  # 단지마다 평형 줄 상한. 넘치면 '외 N개 평형'
PREV_TRADE_MAX_DAYS = 365  # 직전 같은 평형 거래가 최근 거래보다 이만큼 넘게 오래됐으면 비교하지 않는다
SOURCES = ["국토교통부 실거래가 공개시스템", "대법원 법원경매정보·온비드", "부산광역시 정비사업 정보"]
LAG_NOTICE = "최근 30일 거래는 아직 신고되지 않았을 수 있어요"

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
# 프론트 정비사업 종류 이름과 같은 말(src/state/redevelopment.ts). 요약의 '{종류} 단계 변경'에 쓴다
KIND_LABEL: dict[RedevelopmentKind, str] = {
    RedevelopmentKind.redevelopment: "재개발",
    RedevelopmentKind.reconstruction: "재건축",
    RedevelopmentKind.small_reconstruction: "소규모재건축",
    RedevelopmentKind.small_redevelopment: "소규모재개발",
    RedevelopmentKind.street_housing: "가로주택정비",
    RedevelopmentKind.residential_improvement: "주거환경개선",
    RedevelopmentKind.urban_environment: "도시환경정비",
    RedevelopmentKind.other: "정비사업",
}


class ReportWeekNotOver(ValueError):
    """아직 끝나지 않은 주. 워커는 이것만 '다음에 다시'로 넘긴다."""


@dataclass(frozen=True)
class ReportRun:
    users: int  # 관심 단지가 있고 리포트를 켠 탈퇴하지 않은 회원 수
    created: int
    skipped_empty: int  # 모든 관심 단지가 무소식이라 안 만든 회원 수
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
        # 앱 toFixed(1)처럼 float 값 그대로(이진 표현 그대로) 반올림하고 동률이면 위로. :.1f는 은행가 반올림이라 다르다
        eok = Decimal(won / 100_000_000).quantize(Decimal("0.1"), ROUND_HALF_UP)
        return f"{eok:f}".removesuffix(".0") + "억"
    return f"{_round_half_up(won / 10_000):,}만"


def _format_diff(won: int) -> str:
    """'+2,000만', '-1.2억'. 크기는 format_price 규칙."""
    return ("+" if won > 0 else "-") + format_price(abs(won))


def _area_label(supply_pyeong: float | None, exclusive_m2: float | None) -> str | None:
    """'N평형': 공급평형을 알면 공급평형, 모르면 전용평(market.py _supply_pyeong과 같은 규칙)."""
    pyeong = supply_pyeong
    if pyeong is None and exclusive_m2 is not None:
        pyeong = exclusive_m2 / SQM_PER_PYEONG
    return None if pyeong is None else f"{_round_half_up(pyeong)}평형"


def _mmdd(day: date) -> str:
    return f"{day:%m/%d}"


def _toward(quoted: str, word: str) -> str:
    """'단계'로 / '준공'으로. ㄹ 받침 뒤는 '로'."""
    ch = word[-1:]
    final = (ord(ch) - 0xAC00) % 28 if "가" <= ch <= "힣" else 0
    return quoted + ("으로" if final not in (0, 8) else "로")


def _stage_label(stage: str | None, step_raw: str | None = None) -> str:
    try:
        label = STAGE_LABEL.get(RedevelopmentStage(stage))
    except ValueError:
        label = None
    return label or step_raw or "단계 미상"


def _kind_label(kind: str) -> str:
    try:
        return KIND_LABEL[RedevelopmentKind(kind)]
    except (ValueError, KeyError):
        return KIND_LABEL[RedevelopmentKind.other]


# ── SQL ────────────────────────────────────────────────────

# 관심 단지가 있고 리포트를 켠(키가 없으면 켠 것) 탈퇴하지 않은 회원
_SUBSCRIBERS = text(
    """
    SELECT i.user_id, i.target_id
    FROM user_interests i
    JOIN users u ON u.id = i.user_id AND u.deleted_at IS NULL
    WHERE i.kind = 'complex' AND coalesce((u.settings->>'weekly_report')::boolean, true)
    """
)
# 단지 이름·시군구·동·대표점(보낼 때 값으로 저장한다)
_COMPLEXES = text(
    """
    SELECT c.id, c.name, ST_Y(c.location) AS lat, ST_X(c.location) AS lng,
           CASE WHEN g.level = 'dong' THEN g.name END AS dong_name,
           CASE WHEN g.level = 'dong' THEN p.name ELSE g.name END AS sigungu_name
    FROM complexes c
    LEFT JOIN regions g ON g.code = c.region_code
    LEFT JOIN regions p ON p.code = g.parent_code
    WHERE c.id = ANY(:ids)
    """
)
# 그 주에 처음 수집된(정기 수집이 끝난) 새 매매. 0원 거래는 지도 마커(_METRICS)처럼 뺀다.
# :start·:end는 실행 종료 시각, :min_contract는 계약일 하한
_NEW_SALES_CTE = """
    new AS (
        SELECT t.id, t.complex_id, floor(t.exclusive_area_m2 + 0.5)::int AS bucket, t.price, t.contract_date,
               t.exclusive_area_m2::float8 AS area
        FROM transactions t JOIN etl_runs r ON r.id = t.first_seen_run_id
        WHERE t.complex_id = ANY(:ids)
          AND t.deal_type = 'sale' AND NOT t.is_cancelled AND t.deleted_at IS NULL AND t.price <> 0
          AND r.job = 'collect' AND r.status IN ('success', 'partial')
          AND r.finished_at >= :start AND r.finished_at < :end
          AND t.contract_date >= :min_contract
    )
"""
# 평형 묶음마다 건수, 최근 거래(계약일 늦은 것 → 가격 높은 것), 공급평형, 직전 같은 평형 거래 가격.
# 직전 거래: 주가 끝나기 전에 처음 수집됐고(언제 돌려도 같은 숫자), 이번 주 새 거래가 아니고,
# 최근 거래 계약일보다 늦지 않고 PREV_TRADE_MAX_DAYS 안인 것 중 가장 늦은 것
_SALES = text(
    f"""
    WITH {_NEW_SALES_CTE},
    buckets AS (SELECT complex_id, bucket, count(*) AS n FROM new GROUP BY 1, 2),
    latest AS (
        SELECT DISTINCT ON (complex_id, bucket) complex_id, bucket, price, contract_date, area
        FROM new ORDER BY complex_id, bucket, contract_date DESC, price DESC, id
    )
    SELECT b.complex_id, b.bucket, b.n, l.price, l.contract_date, l.area,
           cat.supply_area_pyeong::float8 AS supply, p.price AS prev_price
    FROM buckets b
    JOIN latest l ON l.complex_id = b.complex_id AND l.bucket = b.bucket
    LEFT JOIN complex_area_types cat ON cat.complex_id = b.complex_id AND cat.area_bucket = b.bucket
    LEFT JOIN LATERAL (
        SELECT t.price FROM transactions t JOIN etl_runs pr ON pr.id = t.first_seen_run_id
        WHERE t.complex_id = b.complex_id AND pr.finished_at < :end AND floor(t.exclusive_area_m2 + 0.5) = b.bucket
          AND t.deal_type = 'sale' AND NOT t.is_cancelled AND t.deleted_at IS NULL AND t.price <> 0
          AND NOT EXISTS (SELECT 1 FROM new WHERE new.id = t.id)
          AND t.contract_date <= l.contract_date AND t.contract_date >= l.contract_date - CAST(:prev_days AS int)
        ORDER BY t.contract_date DESC, t.id LIMIT 1
    ) p ON true
    """
)
# 그 주에 처음 본 경매(그 주 안에 닫힌 것도 넣는다). 단지 잇기는 지도 ⚖ 배지와 같은 SQL이라 한 경매는 한 단지에만 붙는다
_AUCTIONS = text(
    f"""
    SELECT a.id, a.source, a.min_price, a.sale_date, a.bid_end_at,
           cx.id AS complex_id, cx.exclusive_area_m2, cx.supply_area_pyeong
    FROM auctions a
    {AUCTION_COMPLEX_LINK}
    WHERE a.first_seen_at >= :start AND a.first_seen_at < :end
      AND a.pnu IN (SELECT pnu FROM complexes WHERE id = ANY(:ids) AND pnu IS NOT NULL)
      AND cx.id = ANY(:ids)
    """
)
# 단지 대표점이 경계 안에 있는(경계 종류는 가리지 않는다) 구역 중 그 주에 단계가 바뀐 것.
# 처음 본 구역은 stage_changed_at이 비어 있어 빠진다. 조각이 여럿이어도 (단지, 구역)마다 한 번
_REDEVELOPMENTS = text(
    """
    SELECT DISTINCT ON (c.id, d.id) c.id AS complex_id, d.id, d.area_name, d.kind, d.previous_stage, d.stage,
           d.step_raw
    FROM complexes c
    JOIN redevelopment_zones z ON ST_Covers(z.geom, c.location)
    JOIN redevelopments d ON d.id = z.redevelopment_id
    WHERE c.id = ANY(:ids) AND d.stage_changed_at >= :start AND d.stage_changed_at < :end
    ORDER BY c.id, d.id
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


@dataclass(frozen=True)
class _Complex:
    id: UUID
    name: str
    sigungu_name: str | None
    dong_name: str | None
    lat: float | None
    lng: float | None


@dataclass
class _Facts:
    """한 주 동안 단지별로 모은 원값."""

    areas: dict[UUID, list[dict]]  # 평형 묶음(정렬 전 전체)
    auctions: dict[UUID, list[dict]]
    redevelopments: dict[UUID, list[dict]]


def _parse_ids(target_ids: list[str]) -> dict[str, UUID]:
    ids = {}
    for target in target_ids:
        try:
            ids[target] = UUID(target)
        except ValueError:
            continue  # 단지 id 모양이 아니면 단지와 이을 수 없다
    return ids


async def _collect(conn, ids: list[UUID], week: date) -> _Facts:
    start, end = _bounds(week)
    window = {"ids": ids, "start": start, "end": end}

    areas: dict[UUID, list[dict]] = defaultdict(list)
    params = {**window, "min_contract": week - timedelta(days=NEW_CONTRACT_DAYS), "prev_days": PREV_TRADE_MAX_DAYS}
    for r in await conn.execute(_SALES, params):
        prev_diff = None
        if r.prev_price is not None:
            diff = r.price - r.prev_price
            # 만 원 단위, 부호를 떼고 반올림해 +·- 대칭으로
            prev_diff = int(math.copysign(_round_half_up(abs(diff) / 10_000) * 10_000, diff))
        areas[r.complex_id].append(
            {
                "area_bucket": r.bucket,
                "area_label": _area_label(r.supply, r.area),
                "count": r.n,
                "latest": {"price": r.price, "contract_date": r.contract_date.isoformat()},
                "prev_diff": prev_diff,
            }
        )

    auctions: dict[UUID, list[dict]] = defaultdict(list)
    for r in await conn.execute(_AUCTIONS, {**window, "area_tolerance": AREA_TOLERANCE_M2}):
        # 경매는 매각기일, 공매는 입찰 마감(KST 날짜)
        if r.source == AuctionSource.public:
            day = r.bid_end_at.astimezone(KST).date() if r.bid_end_at else None
        else:
            day = r.sale_date
        auctions[r.complex_id].append(
            {
                "id": r.id,
                "area_label": _area_label(r.supply_area_pyeong, r.exclusive_area_m2),
                "min_price": r.min_price,
                "date": None if day is None else day.isoformat(),
                "source": r.source,
            }
        )
    for items in auctions.values():
        items.sort(key=lambda a: (a["date"] is None, a["date"] or "", a["id"]))

    redevelopments: dict[UUID, list[dict]] = defaultdict(list)
    for r in await conn.execute(_REDEVELOPMENTS, window):
        redevelopments[r.complex_id].append(
            {
                "id": r.id,
                "area_name": r.area_name,
                "kind": r.kind,
                "previous_stage": r.previous_stage,
                "stage": r.stage,
                "_step_raw": r.step_raw,
            }
        )
    for items in redevelopments.values():
        items.sort(key=lambda d: (d["area_name"], d["id"]))
    return _Facts(areas, auctions, redevelopments)


# ── 단지 블록(회원 사이에 공유) ──────────────────────────────


def _area_line(area: dict) -> str:
    latest = area["latest"]
    price = format_price(latest["price"])
    day = _mmdd(date.fromisoformat(latest["contract_date"]))
    label = area["area_label"]
    line = f"{label} {area['count']}건: " + (f"최근 {price}" if area["count"] > 1 else price) + f" ({day})"
    diff = area["prev_diff"]
    if diff == 0:
        line += " · 직전 같은 평형과 같아요"
    elif diff is not None:
        line += f" · 직전 같은 평형보다 {_format_diff(diff)}"
    return line


def _auction_detail(a: dict) -> str:
    """'{평형} · 최저가 … · 매각기일|입찰 마감 MM/DD'. 모르는 조각은 뺀다."""
    parts = []
    if a["area_label"]:
        parts.append(a["area_label"])
    if a["min_price"] is not None:
        parts.append(f"최저가 {format_price(a['min_price'])}")
    if a["date"]:
        what = "입찰 마감" if a["source"] == AuctionSource.public else "매각기일"
        parts.append(f"{what} {_mmdd(date.fromisoformat(a['date']))}")
    return " · ".join(parts)


def _auction_lines(auctions: list[dict]) -> list[str]:
    """1건이면 '새 경매 1건: {내용}', 2건 이상이면 머리줄 '새 경매 N건' 뒤에 건마다 내용 한 줄.
    내용을 하나도 모르는 건은 머리줄의 건수로만 남는다."""
    if not auctions:
        return []
    if len(auctions) == 1:
        detail = _auction_detail(auctions[0])
        return ["새 경매 1건" + (f": {detail}" if detail else "")]
    details = [_auction_detail(a) for a in auctions]
    return [f"새 경매 {len(auctions)}건", *(d for d in details if d)]


def _redevelopment_line(d: dict) -> str:
    now = _stage_label(d["stage"], d["_step_raw"])
    if d["previous_stage"] is None:
        return f"{d['area_name']}: '{now}' 단계가 됐어요"
    before = _stage_label(d["previous_stage"])
    return f"{d['area_name']}: '{before}' → {_toward(f"'{now}'", now)} 단계가 바뀌었어요"


def _complex_block(c: _Complex, facts: _Facts) -> dict | None:
    """소식이 있으면 블록, 없으면 None."""
    all_areas = sorted(facts.areas.get(c.id, []), key=lambda a: (-a["count"], -a["area_bucket"]))
    auctions = facts.auctions.get(c.id, [])
    redevelopments = facts.redevelopments.get(c.id, [])
    if not (all_areas or auctions or redevelopments):
        return None
    count = sum(a["count"] for a in all_areas)
    areas = all_areas[:MAX_AREA_LINES]
    more = len(all_areas) - len(areas)

    lines = []
    if count:
        lines.append(f"새 실거래 {count}건")
        lines.extend(_area_line(a) for a in areas)
        if more:
            lines.append(f"외 {more}개 평형")
    lines.extend(_auction_lines(auctions))
    lines.extend(_redevelopment_line(d) for d in redevelopments)
    return {
        "id": str(c.id),
        "name": c.name,
        "sigungu_name": c.sigungu_name,
        "dong_name": c.dong_name,
        "lat": c.lat,
        "lng": c.lng,
        "lines": lines,
        "sales": {"count": count, "areas": areas, "more_areas": more},
        "auctions": auctions,
        "redevelopments": [{k: v for k, v in d.items() if not k.startswith("_")} for d in redevelopments],
    }


def _first_news(b: dict) -> str:
    """새 실거래 > 새 경매 > 정비사업 단계 순으로 가장 앞 소식 하나."""
    if b["sales"]["count"]:
        return f"새 실거래 {b['sales']['count']}건"
    if b["auctions"]:
        return f"새 경매 {len(b['auctions'])}건"
    return f"{_kind_label(b['redevelopments'][0]['kind'])} 단계 변경"


def _build_report(
    week: date, complexes: list[_Complex], blocks: dict[UUID, dict | None]
) -> tuple[str, str, dict] | None:
    """(제목, 요약, payload). 모든 단지가 무소식이면 None."""
    ordered = sorted(complexes, key=lambda c: (c.name, str(c.id)))
    news = [blocks[c.id] for c in ordered if blocks[c.id] is not None]
    if not news:
        return None
    quiet = [{"id": str(c.id), "name": c.name} for c in ordered if blocks[c.id] is None]
    end = week + timedelta(days=6)
    title = f"관심 단지 주간 리포트 · {week.month}/{week.day}~{end.month}/{end.day}"
    summary = f"{news[0]['name']} {_first_news(news[0])}" + (f" 외 {len(news) - 1}개 단지" if len(news) > 1 else "")
    payload = {
        "period": {"start": week.isoformat(), "end": end.isoformat()},
        "headline": f"관심 단지 {len(ordered)}곳 중 {len(news)}곳에 새 소식이 있어요",
        "total": len(ordered),
        "with_news": len(news),
        "complexes": news,
        "quiet": quiet,
        "sources": SOURCES,
        "lag_notice": LAG_NOTICE,
    }
    return title, summary, payload


# ── 배치 ───────────────────────────────────────────────────


async def generate_interest_reports(engine: AsyncEngine, *, week: date, now: datetime) -> ReportRun:
    """그 주(week = 월요일) 리포트를 회원마다 한 통씩 만든다. 이미 있으면 덮지 않는다."""
    if week.weekday() != 0:
        raise ValueError(f"리포트 주는 월요일이어야 합니다: {week}")
    if _bounds(week)[1] > now:
        # 끝나지 않은 주는 일부 데이터로 만든 리포트가 그대로 굳으므로(다시 돌려도 덮지 않는다) 막는다
        raise ReportWeekNotOver(f"아직 끝나지 않은 주입니다: {week}")
    repo = PostgresNotificationRepository(engine)
    await repo.delete_older_than(now - RETENTION)

    start, end = _bounds(week)
    async with engine.connect() as conn:
        subscribers = (await conn.execute(_SUBSCRIBERS)).all()
        has_collect = (await conn.execute(_HAS_COLLECT, {"start": start, "end": end})).scalar_one()
    targets: dict[UUID, list[str]] = defaultdict(list)
    for r in subscribers:
        targets[r.user_id].append(r.target_id)

    if not has_collect:
        # 수집이 없던 주를 "새 소식 없음"으로 잘못 보내지 않는다
        logger.warning("%s 주에 끝난 실거래 수집(collect)이 없어 관심 단지 리포트를 만들지 않습니다", week)
        return ReportRun(users=len(targets), created=0, skipped_empty=0, skipped_existing=0, no_collect=True)

    parsed = _parse_ids(sorted({t for ts in targets.values() for t in ts}))
    ids = sorted(set(parsed.values()))
    infos: dict[UUID, _Complex] = {}
    blocks: dict[UUID, dict | None] = {}
    if ids:
        async with engine.connect() as conn:
            for r in await conn.execute(_COMPLEXES, {"ids": ids}):
                infos[r.id] = _Complex(r.id, r.name, r.sigungu_name, r.dong_name, r.lat, r.lng)
            facts = await _collect(conn, ids, week)
        # 단지마다 한 번 계산해 회원 사이에 같은 블록을 쓴다
        blocks = {cid: _complex_block(c, facts) for cid, c in infos.items()}

    created = skipped_empty = skipped_existing = 0
    for user_id, target_ids in targets.items():
        # 지워진 단지(관심 행은 남는다)는 이름을 몰라 리포트에서 뺀다
        mine = {parsed[t] for t in target_ids if t in parsed and parsed[t] in infos}
        report = _build_report(week, [infos[cid] for cid in mine], blocks)
        if report is None:
            skipped_empty += 1
            continue
        title, summary, payload = report
        if await repo.insert_report(user_id, week, title, summary, payload, now):
            created += 1
        else:
            skipped_existing += 1
    return ReportRun(len(targets), created, skipped_empty, skipped_existing, no_collect=False)

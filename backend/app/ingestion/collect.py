import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.adapters.molit.types import QuotaExceeded, SourceAuthError, SourceKind, TradeSource
from app.core.dates import months_ago
from app.ingestion.rawcache import RawCache
from app.repositories.ports import SourceUnavailable

logger = logging.getLogger(__name__)

Window = tuple[SourceKind, str, str]
MAX_CONSECUTIVE_FAILURES = 5


def recent_months(now: datetime, count: int) -> list[str]:
    """이번 달부터 거슬러 올라간 계약월(YYYYMM) 목록. 최신 달이 먼저다."""
    today = now.date()
    return [months_ago(today, n).strftime("%Y%m") for n in range(count)]


@dataclass
class CollectReport:
    fetched: int = 0
    skipped: int = 0
    rows: int = 0
    failed: list[Window] = field(default_factory=list)
    remaining: list[Window] = field(default_factory=list)
    stopped_reason: str | None = None


def _is_fresh(cache: RawCache, window: Window, now: datetime, max_age: timedelta, settled_months: int) -> bool:
    fetched = cache.fetched_at(*window)
    if fetched is None:
        return False
    # 신고 기한이 지난 오래된 달은 한 번 받으면 다시 받지 않고, 최근 달만 일정 시간이 지나면 다시 받는다
    settled_before = months_ago(now.date(), settled_months).strftime("%Y%m")
    if window[2] < settled_before and fetched.strftime("%Y%m") > window[2]:
        return True
    return now - fetched < max_age


async def collect(
    source: TradeSource,
    cache: RawCache,
    *,
    months: Sequence[str],
    sgg_codes: Sequence[str],
    kinds: Sequence[SourceKind],
    now: datetime,
    max_age: timedelta = timedelta(hours=20),
    settled_months: int = 3,
    on_window: Callable[[Window, int], None] | None = None,
) -> CollectReport:
    """최신 달부터 (종류, 시군구, 계약월) 단위로 수집해 저장한다. 한도에 닿으면 멈추고 남은 단위를 알려준다."""
    report = CollectReport()
    windows = [(kind, sgg, ym) for ym in months for kind in kinds for sgg in sgg_codes]
    consecutive = 0
    for index, window in enumerate(windows):
        if _is_fresh(cache, window, now, max_age, settled_months):
            report.skipped += 1
            continue
        try:
            rows = await source.fetch_month(*window)
        except QuotaExceeded as e:
            report.stopped_reason = f"호출 한도: {e}"
            report.remaining = windows[index:]
            return report
        except SourceAuthError:
            raise
        except SourceUnavailable as e:
            logger.warning("수집 실패 %s: %s", window, e)
            report.failed.append(window)
            consecutive += 1
            if consecutive >= MAX_CONSECUTIVE_FAILURES:
                report.stopped_reason = f"연속 {consecutive}회 실패: {e}"
                report.remaining = windows[index + 1 :]
                return report
            continue
        consecutive = 0
        cache.save(*window, rows, fetched_at=now)
        report.fetched += 1
        report.rows += len(rows)
        if on_window:
            on_window(window, len(rows))
    return report

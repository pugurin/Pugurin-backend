from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from app.adapters.kakao import Geocoder
from app.adapters.molit.types import SourceKind, TradeSource
from app.core.busan import SIGUNGU
from app.ingestion.build import needed_lookups
from app.ingestion.collect import CollectReport, collect, recent_months
from app.ingestion.geocode import GeocodeCache, geocode_missing
from app.ingestion.rawcache import RawCache


@dataclass
class RefreshReport:
    collect: CollectReport
    geocoded: int
    geocode_stopped: str | None = None


async def refresh(
    source: TradeSource,
    geocoder: Geocoder | None,
    data_dir,
    *,
    now: datetime,
    months: int,
    kinds: Sequence[SourceKind] = tuple(SourceKind),
    sgg_codes: Sequence[str] | None = None,
    out: Callable[[str], None] = print,
) -> RefreshReport:
    """수집(실거래가 API) → 지오코딩(카카오)을 이어서 하고 캐시에 저장한다."""
    raw = RawCache(data_dir)
    codes = list(sgg_codes or [s.code for s in SIGUNGU])
    report = await collect(
        source,
        raw,
        months=recent_months(now, months),
        sgg_codes=codes,
        kinds=kinds,
        now=now,
        on_window=lambda w, n: out(f"  수집 {w[0].value} {w[1]} {w[2]}: {n}건"),
    )
    out(f"수집 끝: 새로 받음 {report.fetched}, 건너뜀 {report.skipped}, 행 {report.rows}, 실패 {len(report.failed)}")
    if report.stopped_reason:
        out(f"  중단: {report.stopped_reason} (남은 단위 {len(report.remaining)}개, 다시 실행하면 이어서 받습니다)")

    geocoded = 0
    stopped = None
    if geocoder is not None:
        trades = (t for w in raw.windows() if w[1] in codes for t in raw.load(*w))
        addresses, regions = needed_lookups(trades)
        cache = GeocodeCache(data_dir / "geocode.json")
        out(f"지오코딩: 지번 {len(addresses)}곳, 법정동 {len(regions)}곳 중 캐시에 없는 것만 조회")
        try:
            geocoded = await geocode_missing(
                geocoder, cache, addresses, regions, on_progress=lambda d, t: out(f"  지오코딩 {d}/{t}")
            )
        except Exception as e:  # noqa: BLE001 - 한도·인증 오류 모두 저장된 진행분을 남기고 알린다
            stopped = f"{type(e).__name__}: {e}"
            out(f"  지오코딩 중단: {stopped}")
        out(f"지오코딩 끝: 새로 조회 {geocoded}건 (캐시 총 {len(cache)}건)")
    return RefreshReport(report, geocoded, stopped)

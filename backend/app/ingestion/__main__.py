import argparse
import asyncio
import sys
from datetime import datetime

from app.adapters.kakao import KakaoGeocoder
from app.adapters.molit.client import MolitClient
from app.adapters.molit.types import SourceKind
from app.core.config import Settings
from app.core.dates import KST
from app.db.engine import make_engine
from app.ingestion.build import GeoIndex
from app.ingestion.geocode import GeocodeCache
from app.ingestion.load import LoadReport, load_into_db
from app.ingestion.rawcache import RawCache
from app.ingestion.real import RealDataMissing, load_real_data
from app.ingestion.refresh import refresh
from app.ingestion.worker import run_daily


def _status(settings: Settings) -> int:
    try:
        data = load_real_data(settings)
    except RealDataMissing as e:
        print(e)
        return 1
    r = data.report
    print(f"기준 시각 {data.as_of.isoformat()}")
    print(f"거래 {r.rows_out:,}건 (원천 {r.rows_in:,}건)")
    print(f"토지에서 제외: 지분거래 {r.excluded_share_deals}건, 도로 {r.excluded_road_land}건")
    print(f"단지 {r.complexes:,}곳 (정확한 위치 없음 {r.complexes_without_exact_location}), 지역 {r.regions}곳")
    print(f"법정동을 못 찾은 이름 {len(r.unmapped_regions)}개, 그 거래 {r.unmapped_region_rows}건")
    for sgg, umd in sorted(r.unmapped_regions)[:10]:
        print(f"  - {sgg} {umd}")
    return 0


def _print_load(r: LoadReport) -> None:
    failed = len(r.windows_failed)
    print(f"DB 적재 {r.status}: 단위 {r.windows_loaded}개 적재, {r.windows_skipped}개 변경 없음, {failed}개 실패")
    print(f"  거래 새로 {r.rows_inserted:,} · 바뀜 {r.rows_updated:,} · 해제 {r.rows_cancelled:,}", end="")
    print(f" · 사라짐 {r.rows_deleted:,}")
    if r.unmapped_regions:
        names = ", ".join(f"{sgg} {umd}" for sgg, umd in sorted(r.unmapped_regions)[:10])
        print(f"  법정동을 못 찾은 이름 {len(r.unmapped_regions)}개 ({r.unmapped_rows}건 제외): {names}")
        print("  → 법정동 CSV(app/db/seed)를 갱신하고 `python -m app.db seed-regions`를 실행하세요")


async def _load(settings: Settings, *, force: bool = False, calls_made: int = 0, params: dict | None = None) -> int:
    raw = RawCache(settings.data_dir)
    if raw.latest_fetch() is None:
        print(f"적재할 수집 캐시가 없습니다({settings.data_dir}). 먼저 `python -m app.ingestion refresh`를 실행하세요")
        return 1
    engine = make_engine(settings.database_url)
    try:
        geo = GeoIndex(GeocodeCache(settings.data_dir / "geocode.json"))
        report = await load_into_db(engine, raw, geo, force=force, calls_made=calls_made, params=params)
    finally:
        await engine.dispose()
    _print_load(report)
    return 0 if report.status == "success" else 3


async def _refresh(settings: Settings, args: argparse.Namespace) -> int:
    if not (settings.data_go_kr_trade_key and settings.data_go_kr_rent_key):
        print("PUGURIN_DATA_GO_KR_TRADE_KEY, PUGURIN_DATA_GO_KR_RENT_KEY가 필요합니다 (backend/.env)")
        return 2
    if not args.skip_geocode and not settings.kakao_rest_key:
        print("PUGURIN_KAKAO_REST_KEY가 없습니다. 지오코딩 없이 수집만 하려면 --skip-geocode를 쓰세요")
        return 2
    source = MolitClient(
        settings.data_go_kr_trade_key,
        settings.data_go_kr_rent_key,
        base_url=settings.molit_base_url,
        daily_limit=settings.molit_daily_call_limit,
    )
    geocoder = None if args.skip_geocode else KakaoGeocoder(settings.kakao_rest_key)
    kinds = [SourceKind(k) for k in args.kinds.split(",")] if args.kinds else list(SourceKind)
    months = args.months or settings.real_data_months
    try:
        result = await refresh(
            source,
            geocoder,
            settings.data_dir,
            now=datetime.now(KST),
            months=months,
            kinds=kinds,
            sgg_codes=args.districts.split(",") if args.districts else None,
        )
        print(f"API 호출 {source.calls_made}회" + (f", 카카오 {geocoder.calls_made}회" if geocoder else ""))
    finally:
        await source.aclose()
        if geocoder:
            await geocoder.aclose()
    code = 0 if not (result.collect.stopped_reason or result.geocode_stopped) else 3
    if settings.database_url and not args.no_db:
        params = {"months": months, "kinds": [k.value for k in kinds], "districts": args.districts}
        code = max(code, await _load(settings, calls_made=source.calls_made, params=params))
    return code


def _worker(settings: Settings, args: argparse.Namespace) -> int:
    if not (settings.data_go_kr_trade_key and settings.data_go_kr_rent_key and settings.kakao_rest_key):
        print("worker에는 공공데이터포털 키 2개(TRADE·RENT)와 PUGURIN_KAKAO_REST_KEY가 필요합니다")
        return 2
    if not settings.database_url:
        print("worker에는 PUGURIN_DATABASE_URL이 필요합니다")
        return 2
    asyncio.run(run_daily(lambda: _refresh(settings, args), hour=settings.worker_run_hour))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m app.ingestion", description="실거래가 수집·지오코딩 캐시 관리")
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("refresh", help="실거래가를 받아 오고 주소에 좌표·법정동 코드를 붙여 캐시에 저장")
    r.add_argument("--months", type=int, help="이번 달부터 거슬러 올라갈 개월 수 (기본: 설정값)")
    r.add_argument("--kinds", help="예: apt_trade,apt_rent (기본: 7종 전부)")
    r.add_argument("--districts", help="시군구 코드, 쉼표 구분 (기본: 부산 16개 구·군)")
    r.add_argument("--skip-geocode", action="store_true")
    r.add_argument("--no-db", action="store_true", help="PUGURIN_DATABASE_URL이 있어도 DB 적재를 건너뛴다")
    sub.add_parser("status", help="캐시로 만든 데이터셋 요약 (네트워크 사용 안 함)")
    load = sub.add_parser("load", help="수집 캐시를 DB에 적재 (API 호출 없음)")
    load.add_argument("--force", action="store_true", help="이미 적재한 단위도 다시 적재")
    sub.add_parser("worker", help="refresh(수집 → 지오코딩 → DB 적재)를 지금 한 번, 이후 매일 정해진 시각에 실행")
    args = parser.parse_args()
    settings = Settings()
    if args.command == "status":
        return _status(settings)
    if args.command == "load":
        if not settings.database_url:
            print("PUGURIN_DATABASE_URL이 필요합니다 (예: postgresql+asyncpg://pugurin:pugurin@localhost:5432/pugurin)")
            return 2
        return asyncio.run(_load(settings, force=args.force))
    if args.command == "worker":
        args.months = args.kinds = args.districts = None
        args.skip_geocode = args.no_db = False
        return _worker(settings, args)
    return asyncio.run(_refresh(settings, args))


if __name__ == "__main__":
    sys.exit(main())

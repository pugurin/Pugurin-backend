import argparse
import asyncio
import sys
from datetime import datetime

from app.adapters.molit.client import MolitClient
from app.adapters.molit.types import SourceKind
from app.core.config import Settings
from app.core.dates import KST
from app.ingestion.geocode import KakaoGeocoder
from app.ingestion.real import RealDataMissing, load_real_data
from app.ingestion.refresh import refresh


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
    try:
        result = await refresh(
            source,
            geocoder,
            settings.data_dir,
            now=datetime.now(KST),
            months=args.months or settings.real_data_months,
            kinds=kinds,
            sgg_codes=args.districts.split(",") if args.districts else None,
        )
        print(f"API 호출 {source.calls_made}회" + (f", 카카오 {geocoder.calls_made}회" if geocoder else ""))
    finally:
        await source.aclose()
        if geocoder:
            await geocoder.aclose()
    return 0 if not (result.collect.stopped_reason or result.geocode_stopped) else 3


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m app.ingestion", description="실거래가 수집·지오코딩 캐시 관리")
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("refresh", help="실거래가를 받아 오고 주소에 좌표·법정동 코드를 붙여 캐시에 저장")
    r.add_argument("--months", type=int, help="이번 달부터 거슬러 올라갈 개월 수 (기본: 설정값)")
    r.add_argument("--kinds", help="예: apt_trade,apt_rent (기본: 7종 전부)")
    r.add_argument("--districts", help="시군구 코드, 쉼표 구분 (기본: 부산 16개 구·군)")
    r.add_argument("--skip-geocode", action="store_true")
    sub.add_parser("status", help="캐시로 만든 데이터셋 요약 (네트워크 사용 안 함)")
    args = parser.parse_args()
    settings = Settings()
    return _status(settings) if args.command == "status" else asyncio.run(_refresh(settings, args))


if __name__ == "__main__":
    sys.exit(main())

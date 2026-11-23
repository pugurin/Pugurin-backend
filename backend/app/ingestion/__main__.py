import argparse
import asyncio
import sys
from datetime import date, datetime
from pathlib import Path

from app.adapters.building import BuildingRegisterClient
from app.adapters.facilities import HiraHospitalClient, NeisSchoolClient
from app.adapters.kakao import KakaoGeocoder
from app.adapters.madangs import MadangsClient
from app.adapters.molit.client import MolitClient
from app.adapters.molit.types import SourceKind
from app.adapters.redevelopment import BusanRedevelopmentClient
from app.core.config import Settings
from app.core.dates import KST
from app.db.engine import make_engine
from app.ingestion import redevelopment_zones
from app.ingestion.auctions import sync_auctions
from app.ingestion.build import GeoIndex
from app.ingestion.facilities import sync_facilities
from app.ingestion.geocode import GeocodeCache
from app.ingestion.load import LoadBusy, LoadReport, load_into_db
from app.ingestion.rawcache import RawCache
from app.ingestion.real import RealDataMissing, load_real_data
from app.ingestion.redevelopment import sync_redevelopments
from app.ingestion.refresh import refresh
from app.ingestion.school_zones import NoElementarySchools, default_zone_dir, find_zone_files, load_school_zones
from app.ingestion.supply import sync_supply_areas
from app.ingestion.worker import run_daily, run_weekly
from app.services.region_report import ReportRun, generate_region_reports, week_start


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
    except LoadBusy as e:
        print(e)
        return 4
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


async def _auctions(settings: Settings) -> int:
    if not settings.auction_api_key:
        print("PUGURIN_AUCTION_API_KEY가 없어 경매 동기화를 건너뜁니다 (backend/.env)")
        return 2
    if not settings.database_url:
        print("경매 동기화에는 PUGURIN_DATABASE_URL이 필요합니다")
        return 2
    source = MadangsClient(settings.auction_api_key, base_url=settings.auction_base_url)
    geocoder = KakaoGeocoder(settings.kakao_rest_key) if settings.kakao_rest_key else None
    engine = make_engine(settings.database_url)
    try:
        cache = GeocodeCache(settings.data_dir / "geocode.json")
        r = await sync_auctions(engine, source, geocoder, cache, now=datetime.now(KST))
    except LoadBusy as e:
        print(e)
        return 4
    finally:
        await source.aclose()
        if geocoder:
            await geocoder.aclose()
        await engine.dispose()
    print(f"경매 동기화 {r.status}: 경매 {r.court:,}건 · 공매 {r.public:,}건, 상세 {r.details_fetched:,}건 받음")
    print(f"  새로 {r.inserted:,} · 갱신 {r.updated:,} · 종료 {r.closed:,} · 위치 없음 {r.without_location:,}", end="")
    print(f" · 상세 실패 {r.details_failed} · 다음으로 미룸 {r.details_pending}")
    for e in r.errors[:5]:
        print(f"  - {e}")
    return 0 if r.status == "success" else 3


async def _facilities(settings: Settings) -> int:
    academy_key = settings.academy_api_key or settings.neis_api_key
    if not (settings.neis_api_key or settings.hospital_api_key or academy_key):
        print("PUGURIN_NEIS_API_KEY, PUGURIN_HOSPITAL_API_KEY가 없어 학교·병원·학원 동기화를 건너뜁니다 (backend/.env)")
        return 2
    if not settings.database_url:
        print("학교·병원·학원 동기화에는 PUGURIN_DATABASE_URL이 필요합니다")
        return 2
    schools = NeisSchoolClient(settings.neis_api_key) if settings.neis_api_key else None
    hospitals = HiraHospitalClient(settings.hospital_api_key) if settings.hospital_api_key else None
    academies = NeisSchoolClient(academy_key) if academy_key else None
    geocoder = KakaoGeocoder(settings.kakao_rest_key) if settings.kakao_rest_key else None
    engine = make_engine(settings.database_url)
    try:
        cache = GeocodeCache(settings.data_dir / "geocode.json")
        r = await sync_facilities(
            engine, schools, hospitals, geocoder, cache, academies=academies, now=datetime.now(KST)
        )
    except LoadBusy as e:
        print(e)
        return 4
    finally:
        for client in (schools, hospitals, academies, geocoder):
            if client is not None:
                await client.aclose()
        await engine.dispose()
    print(f"학교·병원·학원 동기화 {r.status}: 학교 {r.schools:,}곳 · 병원 {r.hospitals:,}곳 · 학원 {r.academies:,}곳")
    print(f"  새로 {r.inserted:,} · 갱신 {r.updated:,} · 폐교·폐업 {r.closed:,} · 위치 없음 {r.without_location:,}")
    for e in r.errors[:5]:
        print(f"  - {e}")
    return 0 if r.status == "success" else 3


async def _redevelopments(settings: Settings) -> int:
    if not settings.redevelopment_api_key:
        print("PUGURIN_REDEVELOPMENT_API_KEY가 없어 정비사업 동기화를 건너뜁니다 (backend/.env)")
        return 2
    if not settings.database_url:
        print("정비사업 동기화에는 PUGURIN_DATABASE_URL이 필요합니다")
        return 2
    source = BusanRedevelopmentClient(settings.redevelopment_api_key)
    geocoder = KakaoGeocoder(settings.kakao_rest_key) if settings.kakao_rest_key else None
    engine = make_engine(settings.database_url)
    try:
        cache = GeocodeCache(settings.data_dir / "geocode.json")
        r = await sync_redevelopments(engine, source, geocoder, cache, now=datetime.now(KST))
    except LoadBusy as e:
        print(e)
        return 4
    finally:
        await source.aclose()
        if geocoder:
            await geocoder.aclose()
        await engine.dispose()
    print(f"정비사업 동기화 {r.status}: 구역 {r.fetched:,}곳 (시험 행 {r.skipped_test}건 제외)")
    print(f"  새로 {r.inserted:,} · 갱신 {r.updated:,} · 사라짐 {r.closed:,}", end="")
    print(f" · 위치 없음 {r.without_location:,}", end="")
    print(f" (지번 {r.lot} · 사무실 주소 {r.office} · 없음 {r.none})")
    if r.duplicates:
        print(f"  같은 ID가 두 번 나와 뒤의 것을 버림: {', '.join(r.duplicates[:10])}")
    if r.unknown_steps:
        steps = ", ".join(f"{step}({n})" for step, n in list(r.unknown_steps.items())[:10])
        print(f"  단계 표에 없는 원문: {steps} → app/adapters/redevelopment.py의 표를 보강하세요")
    for e in r.errors[:5]:
        print(f"  - {e}")
    return 0 if r.status == "success" else 3


async def _school_zones(settings: Settings, directory: Path | None) -> int:
    """위치를 명시했는데(--dir, PUGURIN_SCHOOL_ZONE_DIR) 파일이 없으면 2, 기본 위치에 없으면 안내만 하고 0."""
    explicit = directory or settings.school_zone_dir
    directory = explicit or default_zone_dir()
    if find_zone_files(directory) is None:
        print(f"학구도 파일이 없습니다({directory}). 한국교육시설안전원 학구도를 내려받아 두세요:")
        print("  zones/초등학교통학구역.shp(.shx·.dbf), links/…학교학구도연계정보….csv (다른 곳이면 --dir)")
        return 2 if explicit else 0
    if not settings.database_url:
        print("학구도 적재에는 PUGURIN_DATABASE_URL이 필요합니다")
        return 2
    engine = make_engine(settings.database_url)
    try:
        r = await load_school_zones(engine, directory, now=datetime.now(KST))
    except NoElementarySchools as e:
        print(e)
        return 3
    finally:
        await engine.dispose()
    base = r.base_date.isoformat() if r.base_date else "모름"
    print(f"학구도 적재: 부산 통학구역 {r.zones:,}곳, 학교 연결 {r.links:,}개 (기준일 {base})")
    if r.without_geometry:
        print(f"  도형이 없어 뺀 구역 {len(r.without_geometry)}곳: {', '.join(r.without_geometry[:10])}")
    if r.unmatched:
        print(f"  학교 목록에 없는 이름이라 연결하지 않음 {len(r.unmatched)}곳: {', '.join(r.unmatched[:20])}")
    if r.by_location:
        picked = ", ".join(f"{name} {zone_id} → {school_id}" for name, zone_id, school_id in r.by_location[:20])
        print(f"  같은 이름 학교를 구역 위치로 골라 연결 {len(r.by_location)}개: {picked}")
    if r.ambiguous:
        names = ", ".join(r.ambiguous[:20])
        print(f"  같은 이름 학교를 위치로 고르지 못해 연결하지 않음 {len(r.ambiguous)}곳: {names}")
        print("    (후보 좌표 없음, 2km 안에 후보 없음, 또는 우리 학교 하나가 학구도 학교 둘에 걸림)")
    if r.linked_without_location:
        names = ", ".join(r.linked_without_location[:20])
        print(f"  연결됐지만 좌표가 없는 학교 {len(r.linked_without_location)}곳(거리 없이 배정): {names}")
    return 0


async def _supply(settings: Settings, max_calls: int | None = None) -> int:
    if not settings.building_api_key:
        print("PUGURIN_BUILDING_API_KEY가 없어 공급면적 동기화를 건너뜁니다 (backend/.env)")
        return 2
    if not settings.database_url:
        print("공급면적 동기화에는 PUGURIN_DATABASE_URL이 필요합니다")
        return 2
    client = BuildingRegisterClient(settings.building_api_key)
    engine = make_engine(settings.database_url)
    try:
        r = await sync_supply_areas(
            engine, client, now=datetime.now(KST), max_calls=max_calls or settings.building_daily_calls
        )
    except LoadBusy as e:
        print(e)
        return 4
    finally:
        await client.aclose()
        await engine.dispose()
    print(f"공급면적 동기화 {r.status}: 단지 {r.complexes:,}곳 확인, 호출 {r.calls:,}회")
    print(f"  평형 채움 {r.area_types_filled:,} · 산출 못 함 {r.area_types_missing:,} · 실패 {r.failed:,}")
    for e in r.errors[:5]:
        print(f"  - {e}")
    return 0 if r.status == "success" else 3


async def _daily(settings: Settings, args: argparse.Namespace) -> int:
    """worker 하루치: 실거래가 수집·적재, 그다음 경매·학교·병원·학원·공급면적·정비사업 동기화(키가 있을 때)."""
    code = await _refresh(settings, args)
    if settings.auction_api_key:
        code = max(code, await _auctions(settings))
    if settings.neis_api_key or settings.hospital_api_key or settings.academy_api_key:
        code = max(code, await _facilities(settings))
    if settings.building_api_key:
        code = max(code, await _supply(settings))
    if settings.redevelopment_api_key:
        code = max(code, await _redevelopments(settings))
    return code


def _worker(settings: Settings, args: argparse.Namespace) -> int:
    if not (settings.data_go_kr_trade_key and settings.data_go_kr_rent_key and settings.kakao_rest_key):
        print("worker에는 공공데이터포털 키 2개(TRADE·RENT)와 PUGURIN_KAKAO_REST_KEY가 필요합니다")
        return 2
    if not settings.database_url:
        print("worker에는 PUGURIN_DATABASE_URL이 필요합니다")
        return 2
    asyncio.run(_worker_loops(settings, args))
    return 0


async def _worker_loops(settings: Settings, args: argparse.Namespace) -> None:
    """일일 수집 루프와 주간 리포트 루프를 함께 돌린다. 각 루프가 자기 예외를 잡아 서로를 멈추지 않는다."""
    await asyncio.gather(
        run_daily(lambda: _daily(settings, args), hour=settings.worker_run_hour),
        run_weekly(lambda: _weekly_job(settings), weekday=0, hour=8),
    )


async def _weekly_job(settings: Settings) -> str:
    try:
        return await _weekly_reports(settings)
    except ValueError as e:  # 아직 끝나지 않은 주: 기록만 하고 다음 주를 기다린다
        return f"리포트를 만들지 않음: {e}"


def _format_report(week: date, r: ReportRun) -> str:
    if r.no_collect:
        return f"{week} 주 리포트: 그 주에 끝난 실거래 수집이 없어 만들지 않음 (회원 {r.users}명)"
    return (
        f"{week} 주 리포트: 회원 {r.users}명 · 새로 {r.created} · 무소식 {r.skipped_empty}"
        f" · 이미 있음 {r.skipped_existing}"
    )


async def _weekly_reports(settings: Settings, week: date | None = None) -> str:
    """직전에 끝난 주(또는 --week)의 관심 지역 리포트를 만든다. 결과는 한 줄 요약.
    월요일이 아니거나 아직 끝나지 않은 주면 ValueError."""
    engine = make_engine(settings.database_url)
    try:
        now = datetime.now(KST)
        week = week or week_start(now)
        r = await generate_region_reports(engine, week=week, now=now)
    finally:
        await engine.dispose()
    return _format_report(week, r)


async def _region_reports(settings: Settings, week: date | None) -> int:
    if not settings.database_url:
        print("관심 지역 리포트에는 PUGURIN_DATABASE_URL이 필요합니다")
        return 2
    try:
        print(await _weekly_reports(settings, week))
    except ValueError as e:
        print(f"리포트를 만들 수 없는 주입니다: {e}")
        return 2
    except Exception as e:  # noqa: BLE001
        print(f"관심 지역 리포트 실패: {type(e).__name__}: {e}")
        return 3
    return 0


async def _redevelopment_zones(settings: Settings, directory: Path | None) -> int:
    """--dir을 줬는데 파일이 없으면 2, 기본 위치에 없으면 안내만 하고 0."""
    target = directory or redevelopment_zones.default_zone_dir()
    if redevelopment_zones.find_zone_files(target) is None:
        print(f"정비구역 경계 파일이 없습니다({target}). 브이월드에서 내려받아 두세요(로그인 필요):")
        print("  공간정보 다운로드 › 용도지역지구정보 › 도시및주거환경정비/정비구역 전국 전체데이터 SHP")
        print("  zones/AL_D029_00_YYYYMMDD.shp(.shx·.dbf) (다른 곳이면 --dir)")
        return 2 if directory else 0
    if not settings.database_url:
        print("정비구역 경계 적재에는 PUGURIN_DATABASE_URL이 필요합니다")
        return 2
    engine = make_engine(settings.database_url)
    try:
        r = await redevelopment_zones.load_redevelopment_zones(engine, target, now=datetime.now(KST))
    finally:
        await engine.dispose()
    base = r.base_date.isoformat() if r.base_date else "모름"
    skipped = ", ".join(f"{why} {n}" for why, n in r.skipped.items() if n)
    print(f"정비구역 경계 적재: 부산 {r.zones:,}개 (기준일 {base}){f' · 뺀 것: {skipped}' if skipped else ''}")
    links = f"좌표로 {r.linked_by_point}개, 이름으로 {r.linked_by_name}개, 사무실로 {r.linked_by_office}개"
    print(f"  우리 정비사업 구역과 잇기: {links}")
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
    sub.add_parser("auctions", help="부산 경매·공매 물건을 경매마당에서 받아 DB에 맞춘다")
    sub.add_parser("facilities", help="부산 학교·학원(나이스)·병원(심평원) 목록을 받아 DB에 맞춘다")
    sub.add_parser("redevelopments", help="부산 정비사업(재개발·재건축) 구역과 단계를 받아 DB에 맞춘다")
    zones = sub.add_parser(
        "school-zones", help="내려받아 둔 학구도(초등학교 통학구역) 파일에서 부산 구역을 DB에 통째로 갈아 끼운다"
    )
    zones.add_argument(
        "--dir", type=Path, help="학구도 파일 위치 (기본: PUGURIN_SCHOOL_ZONE_DIR, 없으면 data/schoolzone을 찾음)"
    )
    redev_zones = sub.add_parser(
        "redevelopment-zones",
        help="내려받아 둔 브이월드 정비구역 경계 파일에서 부산 구역을 DB에 통째로 갈아 끼우고 정비사업 구역과 잇는다",
    )
    redev_zones.add_argument("--dir", type=Path, help="경계 파일 위치 (기본: data/redevelopment을 찾음)")
    supply = sub.add_parser(
        "supply", help="건축물대장으로 아파트 평형별 공급면적을 채운다 (하루 호출 한도 안에서 나눠 받음)"
    )
    supply.add_argument("--max-calls", type=int, help="이번에 쓸 최대 호출 수 (기본: PUGURIN_BUILDING_DAILY_CALLS)")
    rr = sub.add_parser(
        "region-reports", help="관심 지역 주간 리포트를 만들어 알림함에 넣는다 (이미 있는 회원·주는 건너뜀)"
    )
    rr.add_argument("--week", type=date.fromisoformat, help="리포트 주의 월요일 YYYY-MM-DD (기본: 직전에 끝난 주)")
    sub.add_parser(
        "worker",
        help="refresh(수집 → 지오코딩 → DB 적재)와 경매·시설·공급면적·정비사업 동기화를 지금 한 번, 이후 매일 실행."
        " 매주 월요일 08:00 KST에는 관심 지역 리포트도 만든다",
    )
    args = parser.parse_args()
    settings = Settings()
    if args.command == "status":
        return _status(settings)
    if args.command == "load":
        if not settings.database_url:
            print("PUGURIN_DATABASE_URL이 필요합니다 (예: postgresql+asyncpg://pugurin:pugurin@localhost:5432/pugurin)")
            return 2
        return asyncio.run(_load(settings, force=args.force))
    if args.command == "auctions":
        return asyncio.run(_auctions(settings))
    if args.command == "facilities":
        return asyncio.run(_facilities(settings))
    if args.command == "redevelopments":
        return asyncio.run(_redevelopments(settings))
    if args.command == "school-zones":
        return asyncio.run(_school_zones(settings, args.dir))
    if args.command == "redevelopment-zones":
        return asyncio.run(_redevelopment_zones(settings, args.dir))
    if args.command == "region-reports":
        return asyncio.run(_region_reports(settings, args.week))
    if args.command == "supply":
        return asyncio.run(_supply(settings, args.max_calls))
    if args.command == "worker":
        args.months = args.kinds = args.districts = None
        args.skip_geocode = args.no_db = False
        return _worker(settings, args)
    return asyncio.run(_refresh(settings, args))


if __name__ == "__main__":
    sys.exit(main())

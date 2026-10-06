import re
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, time
from uuid import NAMESPACE_URL, uuid5

from app.adapters.molit.types import RawTrade, SourceKind
from app.core.busan import SIGUNGU, SIGUNGU_BY_CODE
from app.core.dates import KST
from app.ingestion.geocode import GeocodeCache, GeoResult, address_key, region_key
from app.repositories.memory.dataset import MarketDataset, RegionDirectory
from app.repositories.types import (
    AreaType,
    Complex,
    DealType,
    LocationPrecision,
    PropertyType,
    RegionRef,
    Transaction,
)

NAME_KEY_STRIP = re.compile(r"[\s\W_]+")


@dataclass(frozen=True)
class BuildOptions:
    exclude_share_deals: bool = True
    exclude_road_land: bool = True


@dataclass
class BuildReport:
    rows_in: int = 0
    rows_out: int = 0
    excluded_share_deals: int = 0
    excluded_road_land: int = 0
    unmapped_region_rows: int = 0
    complexes: int = 0
    complexes_without_exact_location: int = 0
    regions: int = 0
    unmapped_regions: set[tuple[str, str]] = field(default_factory=set)


class GeoIndex:
    """지오코딩 캐시를 읽기만 하는 조회기. 캐시에 없으면 None이다(네트워크를 쓰지 않는다)."""

    def __init__(self, cache: GeocodeCache):
        self._cache = cache

    def address(self, sgg_cd: str, umd_nm: str, jibun: str) -> GeoResult | None:
        return self._cache.get(address_key(SIGUNGU_BY_CODE[sgg_cd].name, umd_nm, jibun))

    def region(self, sgg_cd: str, umd_nm: str) -> GeoResult | None:
        return self._cache.get(region_key(SIGUNGU_BY_CODE[sgg_cd].name, umd_nm))


def needed_lookups(trades: Iterable[RawTrade]) -> tuple[set[tuple[str, str, str]], set[tuple[str, str]]]:
    """지오코딩이 필요한 지번 주소와 법정동 이름. 구 이름은 코드표에서 찾는다."""
    addresses: set[tuple[str, str, str]] = set()
    regions: set[tuple[str, str]] = {(s.name, "") for s in SIGUNGU}
    for t in trades:
        if t.sgg_cd not in SIGUNGU_BY_CODE:
            continue
        name = SIGUNGU_BY_CODE[t.sgg_cd].name
        regions.add((name, t.umd_nm))
        if "*" not in t.jibun and t.jibun:
            addresses.add((name, t.umd_nm, t.jibun))
    return addresses, regions


def _name_key(name: str) -> str:
    return NAME_KEY_STRIP.sub("", name).lower()


def _kst_midnight(d) -> datetime | None:
    return datetime.combine(d, time(0, 0), tzinfo=KST) if d else None


def build_dataset(
    trades: Iterable[RawTrade], geo: GeoIndex, options: BuildOptions | None = None
) -> tuple[MarketDataset, RegionDirectory, BuildReport]:
    options = options or BuildOptions()
    report = BuildReport()
    kept: list[RawTrade] = []
    for t in trades:
        report.rows_in += 1
        if t.sgg_cd not in SIGUNGU_BY_CODE:
            continue
        if t.kind == SourceKind.land_trade:
            if options.exclude_share_deals and t.is_share_deal:
                report.excluded_share_deals += 1
                continue
            if options.exclude_road_land and t.jimok == "도로":
                report.excluded_road_land += 1
                continue
        kept.append(t)

    # 법정동 코드와 중심점: 아파트 매매는 응답의 umdCd를 쓰고, 나머지는 지오코딩의 법정동 코드를 쓴다
    apt_codes = {(t.sgg_cd, t.umd_nm): t.sgg_cd + t.umd_cd for t in kept if t.umd_cd}
    regions: RegionDirectory = {}
    for s in SIGUNGU:
        found = geo.region(s.code, "")
        regions[s.code] = RegionRef(s.code, s.name, found.lat if found else s.lat, found.lng if found else s.lng)
    dong_code: dict[tuple[str, str], str] = {}
    for sgg_cd, umd_nm in {(t.sgg_cd, t.umd_nm) for t in kept}:
        found = geo.region(sgg_cd, umd_nm)
        code = apt_codes.get((sgg_cd, umd_nm)) or (found.b_code if found else None)
        if not code or len(code) != 10 or not code.startswith(sgg_cd):
            report.unmapped_regions.add((sgg_cd, umd_nm))
            continue
        dong_code[(sgg_cd, umd_nm)] = code
        sgg = regions[sgg_cd]
        regions.setdefault(
            code, RegionRef(code, umd_nm, found.lat if found else sgg.lat, found.lng if found else sgg.lng)
        )
    report.regions = len(regions)

    complexes: dict[tuple, Complex] = {}
    areas: dict[tuple, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    builds: dict[tuple, Counter] = defaultdict(Counter)
    location_of: dict[tuple, tuple[float, float, str | None, bool]] = {}
    transactions: list[Transaction] = []

    for t in kept:
        region_code = dong_code.get((t.sgg_cd, t.umd_nm))
        if region_code is None:
            report.unmapped_region_rows += 1
            continue
        region = regions[region_code]
        sgg_name = SIGUNGU_BY_CODE[t.sgg_cd].name
        address = f"부산 {sgg_name} {t.umd_nm} {t.jibun}".strip()

        complex_id = None
        lat, lng, pnu, precise = region.lat, region.lng, None, False
        if t.property_type != PropertyType.land:
            key = (
                ("seq", t.complex_seq)
                if t.complex_seq
                else (t.property_type.value, t.sgg_cd, t.umd_nm, t.jibun, _name_key(t.name or ""))
            )
            if key not in complexes:
                hit = geo.address(t.sgg_cd, t.umd_nm, t.jibun) if t.jibun else None
                if hit and hit.precise:
                    location_of[key] = (hit.lat, hit.lng, hit.pnu, True)
                else:
                    location_of[key] = (region.lat, region.lng, None, False)
                    report.complexes_without_exact_location += 1
                clat, clng, cpnu, _ = location_of[key]
                complexes[key] = Complex(
                    id=uuid5(NAMESPACE_URL, f"pugurin:complex:{key}"),
                    property_type=t.property_type,
                    name=t.name or address,
                    address=address,
                    region_code=region_code,
                    pnu=cpnu,
                    lat=clat,
                    lng=clng,
                    build_year=None,
                    household_count=None,
                    area_types=(),
                )
            complex_id = complexes[key].id
            lat, lng, pnu, precise = location_of[key]
            if t.exclusive_area_m2:
                areas[key][round(t.exclusive_area_m2)].append(t.exclusive_area_m2)
            if t.build_year:
                builds[key][t.build_year] += 1
        elif "*" not in t.jibun and t.jibun:
            hit = geo.address(t.sgg_cd, t.umd_nm, t.jibun)
            if hit and hit.precise:
                lat, lng, pnu, precise = hit.lat, hit.lng, hit.pnu, True

        sale = t.deal_type == DealType.sale
        jeonse = t.deal_type == DealType.jeonse
        transactions.append(
            Transaction(
                id=uuid5(NAMESPACE_URL, f"pugurin:tx:{t.source_key}"),
                property_type=t.property_type,
                deal_type=t.deal_type,
                complex_id=complex_id,
                address=address,
                region_code=region_code,
                jibun=t.jibun or None,
                pnu=pnu,
                location_precision=LocationPrecision.parcel if precise else LocationPrecision.dong,
                lat=lat,
                lng=lng,
                price=(t.amount_man or 0) * 10_000 if sale else None,
                deposit=None if sale else (t.deposit_man or 0) * 10_000,
                monthly_rent=None if sale or jeonse else (t.monthly_rent_man or 0) * 10_000,
                exclusive_area_m2=None if t.property_type == PropertyType.land else t.exclusive_area_m2,
                land_area_m2=t.land_area_m2 if t.property_type == PropertyType.land else None,
                supply_area_pyeong=None,
                floor=t.floor,
                contract_date=t.contract_date,
                build_year=t.build_year,
                trade_method=t.trade_method,
                is_cancelled=t.is_cancelled,
                cancelled_at=_kst_midnight(t.cancelled_date),
            )
        )

    finished: list[Complex] = []
    for key, c in complexes.items():
        types = tuple(AreaType(round(statistics.median(vals), 2), None) for _, vals in sorted(areas[key].items()))
        year = builds[key].most_common(1)[0][0] if builds[key] else None
        finished.append(Complex(**{**c.__dict__, "area_types": types, "build_year": year}))
    report.complexes = len(finished)
    report.rows_out = len(transactions)
    return MarketDataset(tuple(finished), tuple(transactions)), regions, report

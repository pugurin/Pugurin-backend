"""개발·테스트용 단지·거래 샘플 생성기. 모든 값은 가짜이며 같은 기준일이면 항상 같은 결과가 나온다."""

import random
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from uuid import NAMESPACE_URL, UUID, uuid5

from app.core.dates import KST
from app.core.geo import SQM_PER_PYEONG
from app.repositories.types import (
    AreaType,
    Complex,
    DealType,
    LocationPrecision,
    PropertyType,
    TradeMethod,
    Transaction,
)
from app.sample_data.regions import (
    CELL_DLAT,
    CELL_DLNG,
    DONGS,
    LAT0,
    LNG0,
    DongSample,
    grid_cell,
    is_road_cell,
    make_pnu,
)

HISTORY_DAYS = 365 * 5
DIRECT_RATE = 0.08
LAND_DIRECT_RATE = 0.10
CANCEL_RATE = 0.03
LAND_PARCEL_PRECISION_RATE = 0.30

SUPPLY_TO_EXCLUSIVE_M2 = {
    24: 59.97,
    30: 74.98,
    32: 79.34,
    33: 82.0,
    34: 84.97,
    35: 87.0,
    41: 101.97,
    45: 114.92,
    65: 164.0,
}
BRANDS = (
    "푸르지오",
    "래미안",
    "e편한세상",
    "롯데캐슬",
    "자이",
    "더샵",
    "힐스테이트",
    "SK뷰",
    "삼익",
    "현대",
    "대우",
    "협성",
)
OFFICETEL_NAMES = ("센텀오피스텔", "시티타워", "스카이뷰", "더테라스")
VILLA_NAMES = ("그린빌라", "하이츠", "빌리지", "맨션", "하우스")

# 동 이름 → (단지명, 공급 평형, 준공연도, 세대수, 해당 평형 매매가(억), 최고층, 연간 거래 규모)
NAMED_APARTMENTS: dict[str, tuple[tuple[str, int, int, int, float, int, int], ...]] = {
    "우동": (
        ("해운대아이파크", 34, 2011, 1631, 14.5, 45, 24),
        ("해운대두산위브더제니스", 41, 2011, 2700, 12.8, 80, 30),
        ("센텀파크1차", 35, 2005, 1192, 7.4, 35, 20),
    ),
    "중동": (
        ("해운대엘시티더샵", 65, 2019, 882, 32.0, 101, 12),
        ("해운대롯데캐슬스타", 34, 2018, 744, 9.9, 60, 18),
    ),
    "좌동": (("해운대신도시대우", 32, 1997, 1088, 5.4, 20, 22),),
    "남천동": (
        ("남천삼익비치", 33, 1979, 3060, 11.2, 13, 20),
        ("남천자이", 34, 2022, 440, 13.1, 29, 14),
    ),
    "광안동": (("광안자이", 34, 2020, 1100, 9.4, 35, 22),),
    "전포동": (("서면아이파크", 34, 2019, 1360, 8.6, 38, 24),),
    "연산동": (("연산롯데캐슬", 34, 2017, 1510, 7.3, 40, 26),),
    "대연동": (
        ("대연롯데캐슬레전드", 34, 2017, 3149, 8.4, 40, 30),
        ("대연힐스테이트푸르지오", 34, 2013, 2366, 7.6, 35, 24),
    ),
    "명지동": (("명지더샵퍼스트월드", 34, 2012, 2900, 5.8, 30, 28),),
}


@dataclass(frozen=True)
class SampleMarket:
    complexes: tuple[Complex, ...]
    transactions: tuple[Transaction, ...]


def _excl_m2(supply: int) -> float:
    return SUPPLY_TO_EXCLUSIVE_M2.get(supply, round(supply * 0.756 * SQM_PER_PYEONG, 2))


def _place(dong: DongSample, rng: random.Random, spread_lat: float, spread_lng: float) -> tuple[int, int]:
    lat = dong.lat + rng.uniform(-spread_lat, spread_lat)
    lng = dong.lng + rng.uniform(-spread_lng, spread_lng)
    cell = grid_cell(lat, lng)
    assert cell is not None
    i, j = cell
    while is_road_cell(i, j):
        j += 1
    return i, j


def _cell_center(i: int, j: int) -> tuple[float, float]:
    return round(LAT0 + (i + 0.5) * CELL_DLAT, 6), round(LNG0 + (j + 0.5) * CELL_DLNG, 6)


def _jibun(i: int, j: int) -> str:
    return f"{i}-{j}" if j else f"{i}"


def _round_to(value: float, unit: int) -> int:
    return int(round(value / unit)) * unit


def _make_complex(
    dong: DongSample,
    ptype: PropertyType,
    name: str,
    area_types: tuple[AreaType, ...],
    build_year: int,
    households: int,
    rng: random.Random,
) -> Complex:
    i, j = _place(dong, rng, 0.0035, 0.0045)
    lat, lng = _cell_center(i, j)
    return Complex(
        id=uuid5(NAMESPACE_URL, f"pugurin:complex:{dong.code}:{name}"),
        property_type=ptype,
        name=name,
        address=f"부산 {dong.sigungu.name} {dong.name} {_jibun(i, j)}",
        region_code=dong.code,
        pnu=make_pnu(dong.code, i, j),
        lat=lat,
        lng=lng,
        build_year=build_year,
        household_count=households,
        area_types=area_types,
    )


def _apartment_specs(dong: DongSample):
    """(Complex, 전용 1평당 기준가(만원), 최고층, 연간 거래 규모)"""
    specs = []
    for name, supply, year, households, sale_eok, max_floor, c12 in NAMED_APARTMENTS.get(dong.name, ()):
        rng = random.Random(f"complex:{dong.code}:{name}")
        excl = _excl_m2(supply)
        types = (AreaType(excl, float(supply)),)
        if supply != 24:
            types += (AreaType(_excl_m2(24), 24.0),)
        ppp = sale_eok * 1e8 / 1e4 / (excl / SQM_PER_PYEONG)
        specs.append(
            (_make_complex(dong, PropertyType.apartment, name, types, year, households, rng), ppp, max_floor, c12)
        )
    n_auto = max(1, min(5, round(dong.activity / 90)))
    for k in range(n_auto):
        name = f"{dong.stem}{BRANDS[(k * 5 + len(dong.name) * 3 + int(dong.suffix) // 100) % len(BRANDS)]}"
        rng = random.Random(f"complex:{dong.code}:{name}:{k}")
        supply = rng.choice((24, 30, 34, 34, 41))
        types = (AreaType(_excl_m2(supply), float(supply)),)
        if supply != 34:
            types += (AreaType(_excl_m2(34), 34.0),)
        c12 = max(4, round(dong.activity / n_auto * rng.uniform(0.7, 1.3)))
        specs.append(
            (
                _make_complex(
                    dong, PropertyType.apartment, name, types, rng.randint(1988, 2023), rng.randint(200, 1800), rng
                ),
                dong.apt_ppp * rng.uniform(0.85, 1.15),
                rng.randint(10, 35),
                c12,
            )
        )
    return specs


def _other_specs(dong: DongSample):
    specs = []
    if dong.apt_ppp >= 2300:
        for k in range(1 + dong.activity % 2):
            name = f"{dong.stem} {OFFICETEL_NAMES[k % len(OFFICETEL_NAMES)]}"
            rng = random.Random(f"complex:{dong.code}:{name}")
            types = (AreaType(23.1, None), AreaType(29.8, None))
            specs.append(
                (
                    _make_complex(
                        dong, PropertyType.officetel, name, types, rng.randint(2008, 2024), rng.randint(120, 620), rng
                    ),
                    dong.apt_ppp * 0.62 * rng.uniform(0.9, 1.1),
                    rng.randint(8, 35),
                    rng.randint(3, 14),
                )
            )
    for k in range(1 + dong.activity % 3):
        name = f"{dong.stem}{VILLA_NAMES[k % len(VILLA_NAMES)]}"
        rng = random.Random(f"complex:{dong.code}:{name}")
        types = (AreaType(36.4, None), AreaType(59.5, None))
        specs.append(
            (
                _make_complex(dong, PropertyType.villa, name, types, rng.randint(1995, 2022), rng.randint(8, 40), rng),
                dong.apt_ppp * 0.45 * rng.uniform(0.85, 1.15),
                rng.randint(3, 5),
                rng.randint(1, 8),
            )
        )
    return specs


def _complex_transactions(c: Complex, dong: DongSample, ppp_man: float, max_floor: int, c12: int, as_of: date):
    rng = random.Random(f"tx:{c.id}")
    out: list[Transaction] = []
    for deal, factor in ((DealType.sale, 1.0), (DealType.jeonse, 0.7), (DealType.monthly, 0.45)):
        count = max(1, round(c12 * 5 * factor * rng.uniform(0.85, 1.15)))
        for n in range(count):
            days_back = rng.randint(1, HISTORY_DAYS)
            contract = as_of - timedelta(days=days_back)
            area = rng.choice(c.area_types)
            pyeong = area.exclusive_area_m2 / SQM_PER_PYEONG
            sale_value = ppp_man * 1e4 * pyeong * (1 - 0.025 * days_back / 365) * rng.uniform(0.94, 1.06)
            price = deposit = rent = None
            if deal == DealType.sale:
                price = _round_to(sale_value, 1_000_000)
            elif deal == DealType.jeonse:
                deposit = _round_to(sale_value * rng.uniform(0.5, 0.65), 5_000_000)
            else:
                options = {
                    PropertyType.apartment: (30_000_000, 50_000_000, 100_000_000),
                    PropertyType.officetel: (5_000_000, 10_000_000, 20_000_000),
                    PropertyType.villa: (10_000_000, 20_000_000, 30_000_000),
                }[c.property_type]
                low, high = {
                    PropertyType.apartment: (70, 140),
                    PropertyType.officetel: (50, 80),
                    PropertyType.villa: (40, 65),
                }[c.property_type]
                deposit = rng.choice(options)
                rent = _round_to((low + rng.random() * (high - low)) * dong.apt_ppp / 2600 * 10_000, 100_000)
            cancelled = rng.random() < CANCEL_RATE
            cancelled_at = None
            if cancelled:
                cancelled_date = min(contract + timedelta(days=20), as_of)
                cancelled_at = datetime.combine(cancelled_date, time(10, 0), tzinfo=KST)
            out.append(
                Transaction(
                    id=uuid5(c.id, f"{deal}:{n}"),
                    property_type=c.property_type,
                    deal_type=deal,
                    complex_id=c.id,
                    address=c.address,
                    region_code=c.region_code,
                    jibun=c.address.rsplit(" ", 1)[-1],
                    pnu=c.pnu,
                    location_precision=LocationPrecision.parcel,
                    lat=c.lat,
                    lng=c.lng,
                    price=price,
                    deposit=deposit,
                    monthly_rent=rent,
                    exclusive_area_m2=area.exclusive_area_m2,
                    land_area_m2=None,
                    supply_area_pyeong=area.supply_area_pyeong,
                    floor=rng.randint(1, max_floor),
                    contract_date=contract,
                    build_year=c.build_year,
                    trade_method=TradeMethod.direct if rng.random() < DIRECT_RATE else TradeMethod.broker,
                    is_cancelled=cancelled,
                    cancelled_at=cancelled_at,
                )
            )
    return out


def _land_transactions(dong: DongSample, as_of: date) -> list[Transaction]:
    rng = random.Random(f"land:{dong.code}")
    out: list[Transaction] = []
    for n in range(max(1, round(dong.land_n * 5 * rng.uniform(0.8, 1.2)))):
        days_back = rng.randint(1, HISTORY_DAYS)
        contract = as_of - timedelta(days=days_back)
        pyeong = rng.uniform(60, 420)
        price = _round_to(
            dong.land_ppp * 1e4 * pyeong * (1 - 0.025 * days_back / 365) * rng.uniform(0.8, 1.25), 1_000_000
        )
        precise = rng.random() < LAND_PARCEL_PRECISION_RATE
        if precise:
            i, j = _place(dong, rng, 0.004, 0.005)
            lat, lng = _cell_center(i, j)
            pnu, jibun = make_pnu(dong.code, i, j), _jibun(i, j)
            address = f"부산 {dong.sigungu.name} {dong.name} {jibun}"
        else:
            lat, lng, pnu, jibun = dong.lat, dong.lng, None, None
            address = f"부산 {dong.sigungu.name} {dong.name}"
        cancelled = rng.random() < CANCEL_RATE
        cancelled_at = (
            datetime.combine(min(contract + timedelta(days=20), as_of), time(10, 0), tzinfo=KST) if cancelled else None
        )
        out.append(
            Transaction(
                id=uuid5(NAMESPACE_URL, f"pugurin:land:{dong.code}:{n}"),
                property_type=PropertyType.land,
                deal_type=DealType.sale,
                complex_id=None,
                address=address,
                region_code=dong.code,
                jibun=jibun,
                pnu=pnu,
                location_precision=LocationPrecision.parcel if precise else LocationPrecision.dong,
                lat=lat,
                lng=lng,
                price=price,
                deposit=None,
                monthly_rent=None,
                exclusive_area_m2=None,
                land_area_m2=round(pyeong * SQM_PER_PYEONG, 2),
                supply_area_pyeong=None,
                floor=None,
                contract_date=contract,
                build_year=None,
                trade_method=TradeMethod.direct if rng.random() < LAND_DIRECT_RATE else TradeMethod.broker,
                is_cancelled=cancelled,
                cancelled_at=cancelled_at,
            )
        )
    return out


@lru_cache(maxsize=4)
def build_sample_market(as_of: date) -> SampleMarket:
    complexes: list[Complex] = []
    transactions: list[Transaction] = []
    for dong in DONGS:
        for c, ppp_man, max_floor, c12 in [*_apartment_specs(dong), *_other_specs(dong)]:
            complexes.append(c)
            transactions.extend(_complex_transactions(c, dong, ppp_man, max_floor, c12, as_of))
        transactions.extend(_land_transactions(dong, as_of))
    return SampleMarket(tuple(complexes), tuple(transactions))


def complex_uuid_by_name(market: SampleMarket, name: str) -> UUID:
    return next(c.id for c in market.complexes if c.name == name)

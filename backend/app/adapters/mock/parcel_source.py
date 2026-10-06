import math
import random

from app.repositories.types import ParcelData, RestrictionInfo, ZoningPart
from app.sample_data.regions import (
    CELL_DLAT,
    CELL_DLNG,
    DONG_BY_CODE,
    cell_origin,
    grid_cell,
    is_road_cell,
    make_pnu,
    nearest_dong,
    parse_pnu,
)

ROAD_SIDES = ("중로한면", "소로한면", "세로(가)", "광대로한면", "맹지")
SHAPES = ("가로장방형", "세로장방형", "정방형", "사다리형", "부정형")
GENERAL_RESTRICTIONS = (
    RestrictionInfo("가축사육제한구역", "소·돼지 등 가축을 키우는 시설을 지을 수 없어요."),
    RestrictionInfo("교육환경보호구역", "학교 근처라 유흥업소 등 일부 시설은 지을 수 없어요."),
    RestrictionInfo("대공방어협조구역", "높은 건물을 지을 때 군부대와 협의가 필요해요."),
    RestrictionInfo("비행안전구역", "비행기 안전을 위해 건물 높이가 제한될 수 있어요."),
)
GREEN_BELT = RestrictionInfo("개발제한구역", "건물을 새로 짓거나 땅의 모양을 바꾸는 일이 엄격하게 제한돼요.")
NATURAL_GREEN = "자연녹지지역"


class MockParcelSource:
    """좌표를 ~22m 격자로 나눠 필지를 흉내 낸다. 격자 번호가 PNU에 들어 있어 PNU만으로 같은 필지를 복원한다."""

    def __init__(self, land_price_year: int):
        self._year = land_price_year

    async def resolve_pnu(self, lat: float, lng: float) -> str | None:
        cell = grid_cell(lat, lng)
        if cell is None or is_road_cell(*cell):
            return None
        return make_pnu(nearest_dong(lat, lng).code, *cell)

    async def fetch_parcel(self, pnu: str) -> ParcelData | None:
        parsed = parse_pnu(pnu)
        if parsed is None:
            return None
        dong_code, i, j = parsed
        dong = DONG_BY_CODE.get(dong_code)
        if dong is None or is_road_cell(i, j):
            return None

        rng = random.Random(f"parcel:{pnu}")
        if dong.rural:
            category = rng.choice(("전", "답", "임야", "대", "대"))
        else:
            category = rng.choices(("대", "잡종지", "전"), (82, 10, 8))[0]
        buildable = category in ("대", "잡종지")

        zone = (dong.zone if rng.random() < 0.65 or dong.zone2 is None else dong.zone2) if buildable else NATURAL_GREEN
        if buildable and dong.zone2 and zone != dong.zone2 and rng.random() < 0.15:
            zonings = (ZoningPart(zone, 0.8, "포함"), ZoningPart(dong.zone2, 0.2, "저촉"))
        else:
            zonings = (ZoningPart(zone, 1.0, "포함"),)

        restrictions = [rng.choice(GENERAL_RESTRICTIONS)]
        for extra in rng.sample(GENERAL_RESTRICTIONS, rng.randint(0, 2)):
            if extra not in restrictions:
                restrictions.append(extra)
        if zone == NATURAL_GREEN and rng.random() < 0.3:
            restrictions.append(GREEN_BELT)

        lat0, lng0 = cell_origin(i, j)
        w, h = rng.uniform(0.6, 0.95), rng.uniform(0.6, 0.95)
        west, south = lng0 + CELL_DLNG * (1 - w) / 2, lat0 + CELL_DLAT * (1 - h) / 2
        east, north = west + CELL_DLNG * w, south + CELL_DLAT * h
        ring = [
            [round(x, 6), round(y, 6)]
            for x, y in ((west, south), (east, south), (east, north), (west, north), (west, south))
        ]
        area_m2 = (east - west) * 111_320 * math.cos(math.radians(south)) * (north - south) * 110_574

        return ParcelData(
            pnu=pnu,
            jibun_address=f"부산 {dong.sigungu.name} {dong.name} {i}" + (f"-{j}" if j else ""),
            land_category=category,
            land_area_m2=round(area_m2, 1),
            official_land_price_per_m2=int(round(dong.land_ppp * 1e4 / 3.3058 * rng.uniform(0.55, 0.75) / 1000)) * 1000,
            official_land_price_year=self._year,
            road_side=rng.choice(ROAD_SIDES),
            shape=rng.choice(SHAPES),
            geometry={"type": "Polygon", "coordinates": [ring]},
            zonings=zonings,
            restrictions=tuple(restrictions),
        )

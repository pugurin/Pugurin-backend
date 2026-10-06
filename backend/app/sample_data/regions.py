"""개발·테스트용 샘플 지역 데이터. 좌표·법정동 코드는 근사값이며 실제 서비스 데이터가 아니다."""

import math
from dataclasses import dataclass

LAT0, LNG0 = 34.85, 128.75
CELL_DLAT, CELL_DLNG = 0.0002, 0.00025


@dataclass(frozen=True)
class SigunguSample:
    code: str
    name: str
    lat: float
    lng: float


@dataclass(frozen=True)
class DongSample:
    sigungu_code: str
    name: str
    suffix: str
    lat: float
    lng: float
    apt_ppp: int  # 아파트 전용 평당 매매가(만원)
    activity: int  # 최근 12개월 아파트 매매 거래 규모(건)
    land_ppp: int  # 토지 평당가(만원)
    land_n: int  # 최근 12개월 토지 거래 규모(건)
    zone: str
    zone2: str | None = None

    @property
    def code(self) -> str:
        return self.sigungu_code + self.suffix

    @property
    def stem(self) -> str:
        base = self.name.removesuffix("동").removesuffix("읍")
        return base if len(base) >= 2 else self.name

    @property
    def rural(self) -> bool:
        return self.land_ppp < 700

    @property
    def sigungu(self) -> SigunguSample:
        return SIGUNGU_BY_CODE[self.sigungu_code]


SIGUNGU: tuple[SigunguSample, ...] = (
    SigunguSample("26110", "중구", 35.1064, 129.0323),
    SigunguSample("26140", "서구", 35.0977, 129.0243),
    SigunguSample("26170", "동구", 35.1293, 129.0454),
    SigunguSample("26200", "영도구", 35.0912, 129.0679),
    SigunguSample("26230", "부산진구", 35.1629, 129.0530),
    SigunguSample("26260", "동래구", 35.2050, 129.0837),
    SigunguSample("26290", "남구", 35.1365, 129.0843),
    SigunguSample("26320", "북구", 35.1972, 128.9903),
    SigunguSample("26350", "해운대구", 35.1631, 129.1639),
    SigunguSample("26380", "사하구", 35.1046, 128.9748),
    SigunguSample("26410", "금정구", 35.2430, 129.0922),
    SigunguSample("26440", "강서구", 35.2124, 128.9803),
    SigunguSample("26470", "연제구", 35.1760, 129.0820),
    SigunguSample("26500", "수영구", 35.1454, 129.1133),
    SigunguSample("26530", "사상구", 35.1527, 128.9910),
    SigunguSample("26710", "기장군", 35.2446, 129.2222),
)
SIGUNGU_BY_CODE = {s.code: s for s in SIGUNGU}

R2, R3, R1 = "제2종일반주거지역", "제3종일반주거지역", "제1종일반주거지역"
C1, C2 = "일반상업지역", "근린상업지역"
GN, GP = "자연녹지지역", "계획관리지역"

DONGS: tuple[DongSample, ...] = (
    DongSample("26350", "우동", "10500", 35.1640, 129.1610, 3200, 128, 2100, 18, R3, C1),
    DongSample("26350", "중동", "10600", 35.1630, 129.1740, 3400, 214, 2300, 11, R3, C1),
    DongSample("26350", "좌동", "10700", 35.1710, 129.1760, 2500, 260, 1500, 6, R3, C2),
    DongSample("26350", "재송동", "10800", 35.1790, 129.1270, 2300, 190, 1020, 14, R2, R3),
    DongSample("26350", "반여동", "10900", 35.1980, 129.1310, 1800, 240, 760, 23, R2, GN),
    DongSample("26350", "송정동", "11000", 35.1790, 129.2000, 2100, 52, 1350, 24, R1, GN),
    DongSample("26500", "남천동", "10100", 35.1450, 129.1130, 3600, 140, 2400, 9, R3, C2),
    DongSample("26500", "광안동", "10200", 35.1530, 129.1150, 3000, 260, 1900, 12, R3, C2),
    DongSample("26500", "민락동", "10300", 35.1560, 129.1270, 3300, 150, 2600, 7, R3, C1),
    DongSample("26500", "망미동", "10400", 35.1680, 129.1130, 2400, 120, 1500, 8, R2, R3),
    DongSample("26500", "수영동", "10500", 35.1640, 129.1170, 2500, 90, 1700, 5, R2, C2),
    DongSample("26230", "전포동", "10100", 35.1560, 129.0650, 2700, 180, 2200, 14, C1, R3),
    DongSample("26230", "부전동", "10200", 35.1600, 129.0590, 2600, 150, 2600, 11, C1, R3),
    DongSample("26230", "양정동", "10300", 35.1700, 129.0710, 2300, 210, 1500, 12, R2, R3),
    DongSample("26230", "범천동", "10400", 35.1500, 129.0600, 2000, 120, 1700, 9, R2, C2),
    DongSample("26230", "개금동", "10500", 35.1520, 129.0190, 1700, 260, 1100, 15, R2, R3),
    DongSample("26230", "당감동", "10600", 35.1670, 129.0420, 1800, 190, 1200, 10, R2, R3),
    DongSample("26470", "연산동", "10100", 35.1830, 129.0820, 2600, 450, 1500, 25, R3, C2),
    DongSample("26470", "거제동", "10200", 35.1930, 129.0790, 2700, 260, 1500, 12, R3, R2),
    DongSample("26260", "온천동", "10100", 35.2130, 129.0840, 2600, 320, 1500, 16, R3, R2),
    DongSample("26260", "사직동", "10200", 35.1950, 129.0620, 2700, 240, 1450, 11, R3, R2),
    DongSample("26290", "대연동", "10100", 35.1380, 129.0910, 2600, 420, 1400, 17, R3, R2),
    DongSample("26290", "용호동", "10200", 35.1220, 129.1130, 2000, 300, 1100, 14, R2, R3),
    DongSample("26410", "장전동", "10100", 35.2330, 129.0870, 1900, 240, 1000, 21, R2, GN),
    DongSample("26320", "화명동", "10100", 35.2280, 129.0090, 1700, 410, 900, 28, R2, R3),
    DongSample("26530", "괘법동", "10100", 35.1600, 128.9810, 1500, 180, 1100, 22, R2, C2),
    DongSample("26380", "하단동", "10100", 35.1060, 128.9680, 1500, 260, 1000, 20, R2, C2),
    DongSample("26440", "명지동", "10100", 35.0990, 128.9150, 1900, 480, 900, 38, R2, GP),
    DongSample("26710", "정관읍", "25000", 35.3250, 129.1800, 1700, 480, 480, 58, R2, GP),
    DongSample("26110", "남포동", "10100", 35.0980, 129.0290, 1400, 60, 2300, 6, C1, R3),
    DongSample("26110", "중앙동", "10200", 35.1030, 129.0360, 1600, 60, 2600, 6, C1, R3),
    DongSample("26140", "서대신동", "10100", 35.1100, 129.0190, 1800, 260, 1200, 12, R2, R3),
    DongSample("26140", "암남동", "10200", 35.0780, 129.0170, 1600, 120, 900, 9, R1, GN),
    DongSample("26170", "초량동", "10100", 35.1150, 129.0370, 2000, 250, 1400, 10, R3, C2),
    DongSample("26170", "수정동", "10200", 35.1230, 129.0430, 1800, 160, 1200, 8, R2, R3),
    DongSample("26200", "동삼동", "10100", 35.0760, 129.0850, 1500, 200, 900, 14, R2, GN),
    DongSample("26200", "영선동", "10200", 35.0950, 129.0460, 1300, 150, 850, 12, R2, R3),
)
DONG_BY_CODE = {d.code: d for d in DONGS}


def nearest_dong(lat: float, lng: float) -> DongSample:
    k = math.cos(math.radians(35.1))
    return min(DONGS, key=lambda d: (d.lat - lat) ** 2 + ((d.lng - lng) * k) ** 2)


def grid_cell(lat: float, lng: float) -> tuple[int, int] | None:
    i, j = math.floor((lat - LAT0) / CELL_DLAT), math.floor((lng - LNG0) / CELL_DLNG)
    return (i, j) if 0 <= i <= 9999 and 0 <= j <= 9999 else None


def is_road_cell(i: int, j: int) -> bool:
    return (i * 31 + j * 17) % 11 == 0


def make_pnu(dong_code: str, i: int, j: int) -> str:
    return f"{dong_code}1{i:04d}{j:04d}"


def parse_pnu(pnu: str) -> tuple[str, int, int] | None:
    if len(pnu) != 19 or not pnu.isdigit() or pnu[10] != "1":
        return None
    return pnu[:10], int(pnu[11:15]), int(pnu[15:19])


def cell_origin(i: int, j: int) -> tuple[float, float]:
    return LAT0 + i * CELL_DLAT, LNG0 + j * CELL_DLNG

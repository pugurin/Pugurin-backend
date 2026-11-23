"""가짜 브이월드 정비구역 파일(SHP·SHX·DBF). 실제 파일(전국 2,251개)과 같은 칸을 작게 만든다.

좌표는 실제 원천처럼 EPSG:5186(m)이다. 기준점은 tests/fakes/schoolzone.py와 같다.
"""

from dataclasses import dataclass, field
from pathlib import Path

from tests.fakes.schoolzone import write_dbf, write_polygons

# (위도, 경도) → EPSG:5186 (x, y). pyproj로 구한 값이다
OFFICE_5186 = (399031.020, 289806.615)  # 35.185, 129.185 (FakeLookup의 번호 2500)
PROMOTION_5186 = (397652.337, 290331.529)  # 35.19, 129.17

FIELDS = [("A0", "N", 9), ("A1", "C", 33), ("A2", "C", 254), ("A3", "D", 8), ("A4", "C", 5), ("A5", "C", 6)]
FIELDS += [("A6", "C", 200), ("A7", "C", 8), ("A8", "C", 200), ("A9", "C", 200)]


@dataclass
class FakeZone:
    zone_id: str  # A1
    name: str = ""  # A9
    note: str = ""  # A8
    sigungu: str = "26230"  # A4
    designated: str = "20200101"  # A7
    rings: list[list[tuple[float, float]]] = field(default_factory=list)  # 비우면 null shape
    base: str = "20261106"  # A3


def write_zone_files(directory: Path, zones: list[FakeZone], folder: str = "zones") -> Path:
    """`directory/<folder>/AL_D029_00_20261109.*`를 만든다(재정비촉진은 folder="promotion")."""
    (directory / folder).mkdir(parents=True, exist_ok=True)
    stem = directory / folder / "AL_D029_00_20261109"
    rows = [
        [i + 1, z.zone_id, "도시및주거환경정비/정비구역", z.base, z.sigungu, "UDT100", "정비구역", z.designated]
        + [z.note, z.name]
        for i, z in enumerate(zones)
    ]
    write_dbf(stem.with_suffix(".dbf"), FIELDS, rows)
    write_polygons(stem.with_suffix(".shp"), stem.with_suffix(".shx"), [z.rings for z in zones])
    stem.with_suffix(".cpg").write_text("EUC-KR")
    return directory

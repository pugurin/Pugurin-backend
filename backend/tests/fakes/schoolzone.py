"""가짜 학구도 파일(SHP·SHX·DBF·연계정보 CSV). 실제 파일(35MB)과 같은 형식을 작게 만들어 적재를 시험한다.

좌표는 실제 원천처럼 EPSG:5186(m)이다. 아래 기준점은 PostGIS `ST_Transform`으로 구한 값이다.
"""

import csv
import struct
from dataclasses import dataclass, field
from pathlib import Path

# (위도, 경도) → EPSG:5186 (x, y)
ORIGIN_5186 = (396813.685, 286982.567)  # 35.16, 129.16 (tests의 ORIGIN, 가상 학교들 바로 옆)
NORTH_5186 = (396789.580, 288092.270)  # 35.17, 129.16 (ORIGIN에서 북쪽으로 약 1.1km)
FAR_5186 = (400360.738, 291501.340)  # 35.20, 129.20

ZONE_FIELDS = [("OBJECTID", "N", 10), ("HAKGUDO_ID", "C", 10), ("HAKGUDO_NM", "C", 60), ("HAKGUDO_GB", "C", 1)]
ZONE_FIELDS += [("SD_CD", "C", 2), ("SGG_CD", "C", 3), ("BASE_DT", "C", 10)]
LINK_HEADER = ["학구ID", "학교ID", "학교명", "학교급구분", "시도교육청코드", "시도교육청명", "교육지원청코드"]
LINK_HEADER += ["교육지원청명", "데이터기준일자"]


def square(center: tuple[float, float], half: float) -> list[tuple[float, float]]:
    """시계 방향(shapefile의 바깥 고리 방향)으로 닫힌 정사각형."""
    x, y = center
    return [
        (x - half, y - half),
        (x - half, y + half),
        (x + half, y + half),
        (x + half, y - half),
        (x - half, y - half),
    ]


@dataclass
class FakeZone:
    zone_id: str
    name: str
    gb: str = "0"
    sd_cd: str = "26"
    rings: list[list[tuple[float, float]]] = field(default_factory=list)  # 비우면 null shape
    base_date: str = "2026-09-20"


def link(zone_id: str, school_id: str, name: str, level: str = "초등학교") -> list[str]:
    office = ("7150000", "부산광역시교육청", "7211000", "부산광역시해운대교육지원청")
    return [zone_id, school_id, name, level, office[0], office[1], office[2], office[3], "2026-09-20"]


def write_zone_files(directory: Path, zones: list[FakeZone], links: list[list[str]]) -> Path:
    """`directory/zones/초등학교통학구역.*`와 `directory/links/…연계정보….csv`를 만든다."""
    (directory / "zones").mkdir(parents=True, exist_ok=True)
    (directory / "links").mkdir(parents=True, exist_ok=True)
    stem = directory / "zones" / "초등학교통학구역"
    write_dbf(
        stem.with_suffix(".dbf"),
        ZONE_FIELDS,
        [[i + 1, z.zone_id, z.name, z.gb, z.sd_cd, "710", z.base_date] for i, z in enumerate(zones)],
    )
    write_polygons(stem.with_suffix(".shp"), stem.with_suffix(".shx"), [z.rings for z in zones])
    stem.with_suffix(".cpg").write_text("EUC-KR")
    with (directory / "links" / "한국교육시설안전원_학교학구도연계정보_20260920.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as f:
        w = csv.writer(f)
        w.writerow(LINK_HEADER)
        w.writerows(links)
    return directory


def write_dbf(path: Path, fields: list[tuple[str, str, int]], rows: list[list]) -> None:
    record_len = 1 + sum(n for _, _, n in fields)
    header_len = 32 + 32 * len(fields) + 1
    out = bytearray(struct.pack("<BBBBIHH20x", 3, 126, 9, 20, len(rows), header_len, record_len))
    for name, kind, n in fields:
        out += struct.pack("<11sc4xB15x", name.encode(), kind.encode(), n)
    out += b"\r"
    for row in rows:
        out += b" "
        for (_, kind, n), value in zip(fields, row, strict=True):
            raw = str(value).encode("cp949")
            out += raw.rjust(n) if kind == "N" else raw.ljust(n)
    out += b"\x1a"
    path.write_bytes(bytes(out))


def write_polygons(shp: Path, shx: Path, shapes: list[list[list[tuple[float, float]]]]) -> None:
    records: list[bytes] = []
    for rings in shapes:
        if not rings:
            records.append(struct.pack("<i", 0))
            continue
        points = [p for ring in rings for p in ring]
        xs, ys = [p[0] for p in points], [p[1] for p in points]
        content = struct.pack("<i4d2i", 5, min(xs), min(ys), max(xs), max(ys), len(rings), len(points))
        start = 0
        for ring in rings:
            content += struct.pack("<i", start)
            start += len(ring)
        content += b"".join(struct.pack("<2d", x, y) for x, y in points)
        records.append(content)

    body, index, offset = bytearray(), bytearray(), 50  # 단위는 16비트 워드, 헤더가 100바이트
    for i, content in enumerate(records, start=1):
        words = len(content) // 2
        body += struct.pack(">2i", i, words) + content
        index += struct.pack(">2i", offset, words)
        offset += 4 + words
    shp.write_bytes(_shp_header(50 + len(body) // 2) + bytes(body))
    shx.write_bytes(_shp_header(50 + len(index) // 2) + bytes(index))


def _shp_header(length_words: int) -> bytes:
    return struct.pack(">7i", 9994, 0, 0, 0, 0, 0, length_words) + struct.pack("<2i8d", 1000, 5, *([0.0] * 8))

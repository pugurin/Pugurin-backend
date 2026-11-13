"""초등학교 통학구역(한국교육시설안전원 학구도) 파일을 `school_zones`·`school_zone_schools`에 통째로 갈아 끼운다.

원천은 내려받아 둔 파일이다(API 호출 없음, 기본 위치 `data/schoolzone`, git 제외).
- `zones/*.shp|.shx|.dbf`: 전국 통학구역 다각형. 좌표계 EPSG:5186, DBF는 cp949. 부산(SD_CD='26')만 싣는다.
- `links/*.csv`(학교학구도연계정보, UTF-8 BOM): 학구ID ↔ 학교. 학교ID는 학구도 자체 코드(B…)라
  나이스 학교코드와 다르므로 **학교명**으로 `schools`의 초등학교에 맞춘다. 같은 이름이 여럿이면(부산 송정초 둘)
  구역 안에 좌표가 든 학교, 그런 학교가 하나가 아니면 구역에서 가장 가까운 학교에 연결한다.
  못 맞춘 이름·좌표 있는 후보가 없는 학교는 연결하지 않고 리포트에 남긴다.

SHP는 다각형(Polygon, 유형 5)만 쓰므로 의존성을 더하지 않고 여기서 직접 읽는다.
고리(ring)를 그대로 넘기고 PostGIS `ST_BuildArea`가 바깥 고리·구멍을 맞춘다.
"""

import csv
import struct
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.repositories.types import SHARED_ZONE, ZONE

BUSAN_SD_CD = "26"
ELEMENTARY = "초등학교"
DBF_ENCODING = "cp949"  # .cpg는 EUC-KR. cp949가 그 상위 집합이다
SOURCE_SRID = 5186  # Korea 2000 / Central Belt 2010

# HAKGUDO_GB. 실제 파일에서 '1'인 부산 22곳은 모두 이름에 '공동'이 들어 있고 학교가 둘 이상 연결된 구역과 정확히 같다
# (전국의 '1'에는 광역통학구역도 섞여 있지만 학교가 여럿에 걸친 구역이라 같이 공동통학구역으로 본다)
_KIND_BY_CODE = {"0": ZONE, "1": SHARED_ZONE}

Ring = list[tuple[float, float]]


class NoElementarySchools(RuntimeError):
    """학교 목록(`schools`)에 초등학교가 없어 구역을 학교에 연결할 수 없다."""


@dataclass(frozen=True)
class ZoneFiles:
    shp: Path
    shx: Path
    dbf: Path
    links: Path


@dataclass(frozen=True)
class ZoneRecord:
    index: int  # SHP·DBF 레코드 순번(0부터)
    zone_id: str
    name: str
    kind: str
    base_date: date | None


@dataclass(frozen=True)
class LinkRow:
    zone_id: str
    school_id: str  # 학구도 자체 학교ID(B…)
    school_name: str
    level: str


@dataclass(frozen=True)
class LocationLink:
    """이름만으로 정할 수 없어 구역 위치로 고를 연결."""

    zone_id: str
    school_name: str
    candidates: tuple[str, ...]  # 같은 이름의 우리 학교 ID


@dataclass(frozen=True)
class Candidate:
    school_id: str
    inside: bool  # 학교 좌표가 구역 안
    distance_m: float  # 구역에서 학교까지 거리


@dataclass
class SchoolMatch:
    pairs: list[tuple[str, str]]  # (zone_id, schools.id)
    unmatched: list[str]  # 우리 학교 목록에 없는 학교명
    by_location: list[LocationLink]  # 같은 이름이 여럿이라 위치로 고를 연결


@dataclass
class SchoolZoneReport:
    zones: int = 0
    links: int = 0
    without_geometry: list[str] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)
    ambiguous: list[str] = field(default_factory=list)  # 같은 이름이 여럿인데 좌표 있는 후보가 없어 연결하지 않음
    by_location: list[tuple[str, str, str]] = field(default_factory=list)  # (학교명, zone_id, schools.id)
    base_date: date | None = None


def find_zone_files(directory: Path) -> ZoneFiles | None:
    """`zones/`의 SHP 한 벌과 `links/`의 CSV를 찾는다. 하나라도 없으면 None."""
    shps = sorted((directory / "zones").glob("*.shp")) if (directory / "zones").is_dir() else []
    csvs = sorted((directory / "links").glob("*.csv")) if (directory / "links").is_dir() else []
    if not shps or not csvs:
        return None
    shp = shps[0]
    shx, dbf = shp.with_suffix(".shx"), shp.with_suffix(".dbf")
    if not (shx.exists() and dbf.exists()):
        return None
    return ZoneFiles(shp=shp, shx=shx, dbf=dbf, links=csvs[-1])  # 파일명 끝이 기준일이라 마지막이 최신


def read_dbf(path: Path) -> list[dict[str, str]]:
    """dBASE III 레코드를 문자열 dict로 읽는다(숫자 칸도 문자열). SHP와 순번을 맞추려고 지운 레코드는 빈 dict."""
    raw = path.read_bytes()
    count, header_len, record_len = struct.unpack("<IHH", raw[4:12])
    fields: list[tuple[str, int]] = []
    pos = 32
    while raw[pos] != 0x0D:
        name = raw[pos : pos + 11].split(b"\0")[0].decode("ascii")
        fields.append((name, raw[pos + 16]))
        pos += 32
    rows = []
    for i in range(count):
        record = raw[header_len + i * record_len : header_len + (i + 1) * record_len]
        if record[:1] == b"*":
            rows.append({})
            continue
        row, at = {}, 1
        for name, length in fields:
            row[name] = record[at : at + length].decode(DBF_ENCODING, errors="replace").strip()
            at += length
        rows.append(row)
    return rows


def read_polygons(shp: Path, shx: Path, indices: Iterable[int]) -> dict[int, list[Ring]]:
    """고른 레코드의 고리 목록만 읽는다(전국 파일이 크다). null shape는 빈 목록."""
    offsets = shx.read_bytes()[100:]
    shapes: dict[int, list[Ring]] = {}
    with shp.open("rb") as f:
        for i in indices:
            offset_words, length_words = struct.unpack(">2i", offsets[i * 8 : i * 8 + 8])
            f.seek(offset_words * 2 + 8)
            content = f.read(length_words * 2)
            shape_type = struct.unpack("<i", content[:4])[0]
            if shape_type == 0:
                shapes[i] = []
                continue
            if shape_type not in (5, 15, 25):  # Polygon, PolygonZ, PolygonM. Z·M 값은 쓰지 않는다
                raise ValueError(f"다각형이 아닌 레코드입니다: {i}번, 유형 {shape_type}")
            n_parts, n_points = struct.unpack("<2i", content[36:44])
            parts = list(struct.unpack(f"<{n_parts}i", content[44 : 44 + 4 * n_parts])) + [n_points]
            start = 44 + 4 * n_parts
            coords = struct.unpack(f"<{n_points * 2}d", content[start : start + 16 * n_points])
            points = list(zip(coords[0::2], coords[1::2], strict=True))
            shapes[i] = [points[a:b] for a, b in zip(parts, parts[1:], strict=False) if b - a >= 3]
    return shapes


def rings_wkt(rings: list[Ring]) -> str:
    """고리를 닫힌 선으로 넘긴다. 어느 것이 구멍인지는 `ST_BuildArea`가 정한다. 좌표는 mm까지."""
    lines = []
    for ring in rings:
        closed = ring if ring[0] == ring[-1] else [*ring, ring[0]]
        lines.append("(" + ",".join(f"{x:.3f} {y:.3f}" for x, y in closed) + ")")
    return "MULTILINESTRING(" + ",".join(lines) + ")"


def zone_kind(code: str, name: str) -> str:
    if code in _KIND_BY_CODE:
        return _KIND_BY_CODE[code]
    return SHARED_ZONE if "공동" in name else ZONE


def _date(raw: str) -> date | None:
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def busan_zones(rows: list[dict[str, str]]) -> list[ZoneRecord]:
    """부산 구역만. `index`로 SHP의 같은 순번 레코드를 읽는다."""
    return [
        ZoneRecord(
            i, r["HAKGUDO_ID"], r["HAKGUDO_NM"], zone_kind(r["HAKGUDO_GB"], r["HAKGUDO_NM"]), _date(r["BASE_DT"])
        )
        for i, r in enumerate(rows)
        if r.get("SD_CD") == BUSAN_SD_CD and r.get("HAKGUDO_ID")
    ]


def read_links(path: Path) -> list[LinkRow]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return [
            LinkRow(r["학구ID"].strip(), r["학교ID"].strip(), r["학교명"].strip(), r["학교급구분"].strip())
            for r in csv.DictReader(f)
        ]


def busan_links(rows: list[LinkRow], zone_ids: set[str]) -> list[LinkRow]:
    """실은 구역(부산)의 초등학교 행만."""
    return [r for r in rows if r.level == ELEMENTARY and r.zone_id in zone_ids]


def match_schools(links: list[LinkRow], schools: Iterable[tuple[str, str]]) -> SchoolMatch:
    """학교명이 정확히 같은 초등학교 하나에 연결한다. 어느 쪽이든 같은 이름이 여럿이면 위치로 고르게 넘긴다."""
    ours: dict[str, list[str]] = defaultdict(list)
    for school_id, name in schools:
        ours[name.strip()].append(school_id)
    theirs: dict[str, set[str]] = defaultdict(set)
    for r in links:
        theirs[r.school_name].add(r.school_id)
    pairs: list[tuple[str, str]] = []
    unmatched: set[str] = set()
    by_location: list[LocationLink] = []
    for r in links:
        found = ours.get(r.school_name, [])
        if not found:
            unmatched.add(r.school_name)
        elif len(found) > 1 or len(theirs[r.school_name]) > 1:
            located = LocationLink(r.zone_id, r.school_name, tuple(sorted(found)))
            if located not in by_location:
                by_location.append(located)
        elif (r.zone_id, found[0]) not in pairs:
            pairs.append((r.zone_id, found[0]))
    return SchoolMatch(pairs, sorted(unmatched), by_location)


def pick_school(candidates: list[Candidate]) -> str | None:
    """구역 안에 든 학교가 정확히 하나면 그 학교, 아니면 구역에서 가장 가까운 학교(같으면 ID 순). 후보가 없으면 None."""
    inside = [c for c in candidates if c.inside]
    if len(inside) == 1:
        return inside[0].school_id
    nearest = min(candidates, key=lambda c: (c.distance_m, c.school_id), default=None)
    return nearest.school_id if nearest else None


_ELEMENTARY_SCHOOLS = text("SELECT id, name FROM schools WHERE kind = :kind AND closed_at IS NULL")
# ST_BuildArea가 닫힌 선들로 면을 만들고(안쪽 고리는 구멍), 4326으로 바꾼 뒤 MultiPolygon으로 맞춘다.
# 면이 안 나오는 구역은 건너뛴다(NOT NULL)
_INSERT_ZONE = text(
    f"""
    INSERT INTO school_zones (zone_id, name, kind, geom, base_date, loaded_at)
    SELECT :zone_id, :name, :kind, g, :base_date, :now FROM (
        SELECT ST_Multi(ST_CollectionExtract(ST_MakeValid(ST_Transform(
            ST_BuildArea(ST_GeomFromText(:wkt, {SOURCE_SRID})), 4326)), 3)) AS g
    ) built
    WHERE g IS NOT NULL AND NOT ST_IsEmpty(g)
    """
)
# 좌표 없는 학교는 후보에서 뺀다
_CANDIDATES = text(
    """
    SELECT s.id AS school_id, ST_Contains(z.geom, s.location) AS inside,
           ST_Distance(z.geom::geography, s.location::geography) AS distance_m
    FROM school_zones z JOIN schools s ON s.id = ANY(CAST(:ids AS text[]))
    WHERE z.zone_id = :zone_id AND s.location IS NOT NULL
    """
)
_INSERT_LINK = text(
    """
    INSERT INTO school_zone_schools (zone_id, school_id)
    SELECT :zone_id, :school_id WHERE EXISTS (SELECT 1 FROM school_zones WHERE zone_id = :zone_id)
    """
)


async def load_school_zones(
    engine: AsyncEngine, directory: Path, *, now: datetime, out: Callable[[str], None] = print
) -> SchoolZoneReport:
    """한 트랜잭션에서 지우고 다시 넣는다. 학교 목록에 초등학교가 없으면 `NoElementarySchools`(아무것도 안 바꿈)."""
    files = find_zone_files(directory)
    if files is None:
        raise FileNotFoundError(f"학구도 파일이 없습니다: {directory}")
    zones = busan_zones(read_dbf(files.dbf))
    shapes = read_polygons(files.shp, files.shx, [z.index for z in zones])
    links = busan_links(read_links(files.links), {z.zone_id for z in zones})
    out(f"  부산 통학구역 {len(zones):,}곳, 연계 학교 행 {len(links):,}개")

    zone_rows = [
        {
            "zone_id": z.zone_id,
            "name": z.name,
            "kind": z.kind,
            "wkt": rings_wkt(shapes[z.index]),
            "base_date": z.base_date,
            "now": now,
        }
        for z in zones
        if shapes.get(z.index)
    ]
    async with engine.begin() as conn:
        schools = (await conn.execute(_ELEMENTARY_SCHOOLS, {"kind": ELEMENTARY})).all()
        if not schools:
            raise NoElementarySchools(
                "학교 목록에 초등학교가 없습니다. 먼저 `python -m app.ingestion facilities`로 학교를 동기화하세요"
            )
        match = match_schools(links, [(s.id, s.name) for s in schools])
        await conn.execute(text("DELETE FROM school_zone_schools"))
        await conn.execute(text("DELETE FROM school_zones"))
        if zone_rows:
            await conn.execute(_INSERT_ZONE, zone_rows)
        pairs = list(match.pairs)
        by_location: list[tuple[str, str, str]] = []
        unresolved: set[str] = set()
        for want in match.by_location:
            rows = await conn.execute(_CANDIDATES, {"zone_id": want.zone_id, "ids": list(want.candidates)})
            picked = pick_school([Candidate(r.school_id, r.inside, r.distance_m) for r in rows])
            if picked is None:
                unresolved.add(want.school_name)
            elif (want.zone_id, picked) not in pairs:
                pairs.append((want.zone_id, picked))
                by_location.append((want.school_name, want.zone_id, picked))
        if pairs:
            await conn.execute(_INSERT_LINK, [{"zone_id": z, "school_id": s} for z, s in pairs])
        loaded = {r[0] for r in (await conn.execute(text("SELECT zone_id FROM school_zones"))).all()}
        links_loaded = (await conn.execute(text("SELECT count(*) FROM school_zone_schools"))).scalar_one()
    return SchoolZoneReport(
        zones=len(loaded),
        links=links_loaded,
        without_geometry=[z.zone_id for z in zones if z.zone_id not in loaded],  # 도형이 없거나 면이 안 나온 구역
        unmatched=match.unmatched,
        ambiguous=sorted(unresolved),
        by_location=by_location,
        base_date=max((z.base_date for z in zones if z.base_date), default=None),
    )

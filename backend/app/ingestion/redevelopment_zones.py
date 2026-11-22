"""정비구역 경계(브이월드 용도지역지구정보 "도시및주거환경정비/정비구역" SHP)를 `redevelopment_zones`에
통째로 갈아 끼우고, 우리 정비사업 구역(`redevelopments`)과 일대일로 잇는다. 설계: docs/REDEVELOPMENT_ZONES.md

원천은 내려받아 둔 파일이다(브이월드 로그인 필요, API 호출 없음). 기본 위치는 `default_zone_dir()`의
`data/redevelopment`(git 제외)이고, `zones/*.shp|.shx|.dbf`를 읽는다. 좌표계 EPSG:5186, DBF는 cp949.
SHP 읽기는 학구도 적재(`school_zones`)의 함수를 그대로 쓴다.
"""

import re
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.ingestion.school_zones import SOURCE_SRID, read_dbf, read_polygons, rings_wkt

BACKEND_DIR = Path(__file__).resolve().parents[2]
BUSAN_PREFIX = "26"
MIN_AREA_M2 = 1000.0  # 이보다 작은 조각은 구역이 아니다(실제 구역은 하위 10%도 약 8,000㎡)
NESTED_TOLERANCE_M = 1.0  # 다른 구역 안에 통째로 든 조각을 가릴 때의 여유
ZONE_LIMIT = 300
# 경계 종류 → 파일 폴더. zone: 도시및주거환경정비/정비구역, promotion: 도시재정비/재정비촉진지구,
# planned: 정비예정구역/정비계획을수립중인지역 (브이월드 용도지역지구정보 전체데이터 SHP)
LAYER_DIRS = {"zone": "zones", "promotion": "promotion", "planned": "planned"}

# 구역 안의 시설(도로·공공보행통로·공원…)은 구역이 아니라서 뺀다
_FACILITY = re.compile(r"통로|도로|기반시설|공원|녹지|주차|광장|학교|용지|하수도|구거")
# 재정비촉진지구 파일에서 구역이 아닌 것: 존치(그대로 두는) 구역과 지구 안 시설 필지
_PROMOTION_EXTRA = re.compile(r"존치|근생|종교|교육\d|공공청사")
# 재정비촉진 구역 이름 뒤에 붙은 법 조문 설명("…-국토의계획및이용에관한법률제56조규정에 의한 …")
_LAW_NOTE = re.compile(r"-\s*국토.*$")
# 이름 칸에 이름 대신 들어 있는 안내 문구
_NOT_A_NAME = re.compile(r"별도문의|^\(.*\)$")
_NOTICE = re.compile(r"고시\s*제")
# 핵심 이름을 남기려고 지우는 말. 긴 것부터 지운다
_BUSINESS_WORDS = sorted(
    ["주택", "재개발", "재건축", "도시환경정비", "주거환경개선", "재정비촉진", "정비", "구역", "사업", "일원", "지구"],
    key=len,
    reverse=True,
)
# 이 파일(도시 및 주거환경정비법 정비구역)에 경계가 있는 사업. 가로주택·소규모재건축은 법이 달라 경계가 없다
LINKABLE_KINDS = ("redevelopment", "reconstruction", "residential_improvement", "urban_environment")


@dataclass(frozen=True)
class ZoneFiles:
    shp: Path
    shx: Path
    dbf: Path


@dataclass(frozen=True)
class ZoneRecord:
    index: int  # SHP 레코드 순번
    zone_id: str
    layer: str
    name: str | None
    notice: str | None
    designated_on: date | None
    base_date: date | None
    sigungu_code: str


@dataclass
class RedevelopmentZoneReport:
    zones: int = 0
    layers: dict[str, int] = field(default_factory=dict)  # 종류별로 실은 경계 수
    skipped: dict[str, int] = field(default_factory=dict)  # 뺀 이유별 개수
    linked_by_point: int = 0
    linked_by_name: int = 0
    linked_by_office: int = 0
    base_date: date | None = None


def default_zone_dir(backend_dir: Path = BACKEND_DIR) -> Path:
    """작업 폴더와 상관없이 찾는다. Docker(/app)에 마운트한 `backend/data/redevelopment`이 먼저, 없으면 저장소 루트."""
    inside = backend_dir / "data" / "redevelopment"
    return inside if inside.is_dir() else backend_dir.parent / "data" / "redevelopment"


def find_layer_files(directory: Path) -> dict[str, ZoneFiles]:
    """경계 종류별 SHP 한 벌. 있는 것만."""
    found = {layer: find_zone_files(directory, folder) for layer, folder in LAYER_DIRS.items()}
    return {layer: files for layer, files in found.items() if files is not None}


def find_zone_files(directory: Path, folder: str = "zones") -> ZoneFiles | None:
    """`folder/`의 SHP 한 벌. 여럿이면 이름 끝(기준일)이 가장 늦은 것. 없으면 None."""
    shps = sorted((directory / folder).glob("*.shp")) if (directory / folder).is_dir() else []
    if not shps:
        return None
    shp = shps[-1]
    shx, dbf = shp.with_suffix(".shx"), shp.with_suffix(".dbf")
    return ZoneFiles(shp, shx, dbf) if shx.exists() and dbf.exists() else None


def _clean(raw: str | None) -> str:
    """원천은 빈 칸을 NUL(\\x00)로 채우고, 이름 뒤에 줄을 바꿔 정정 메모를 붙이기도 한다. 첫 줄만 쓴다."""
    text = (raw or "").replace("\x00", "").strip()
    return text.splitlines()[0].strip() if text else ""


def _date(raw: str) -> date | None:
    try:
        return datetime.strptime(_clean(raw), "%Y%m%d").date()
    except ValueError:
        return None


def _name(raw: str) -> str | None:
    """이름 칸 값이 이름이면 그대로, 비었거나 안내 문구·사업 종류만 있으면 None."""
    if not raw or _NOTICE.search(raw) or _NOT_A_NAME.search(raw) or not core_name(raw):
        return None
    return raw


def busan_zone_records(rows: list[dict[str, str]], layer: str = "zone") -> list[ZoneRecord]:
    """부산 구역만, 구역 안의 시설은 빼고. 면적·포함 관계로 거르는 것은 DB에 넣은 뒤에 한다.

    이름은 A9(이름), 없으면 A8(비고)에서 찾는다. A8에는 "주택재개발정비구역"처럼 종류만 있기도 해서
    핵심 이름이 있어야 이름으로 본다.
    """
    out: list[ZoneRecord] = []
    for i, r in enumerate(rows):
        if not _clean(r.get("A1")) or not _clean(r.get("A4")).startswith(BUSAN_PREFIX):
            continue
        raw_name, note = _clean(r.get("A9")), _clean(r.get("A8"))
        if layer == "promotion":
            raw_name, note = _LAW_NOTE.sub("", raw_name).strip(), _LAW_NOTE.sub("", note).strip()
            label = raw_name or note
            if "촉진" not in label or "구역" not in label or _PROMOTION_EXTRA.search(label):
                continue  # 지구 바깥 경계·존치 구역·지구 안 시설
        elif _FACILITY.search(raw_name) or _FACILITY.search(note):
            continue
        notice = raw_name if _NOTICE.search(raw_name) else None
        name = _name(raw_name) or _name(note)
        out.append(
            ZoneRecord(
                i,
                _clean(r["A1"]),
                layer,
                name,
                notice,
                _date(r.get("A7", "")),
                _date(r.get("A3", "")),
                _clean(r["A4"]),
            )
        )
    return out


def core_name(name: str) -> str:
    """이름에서 공백과 사업 이름을 지운 핵심 이름. 예: "광안2주택재건축구역" → "광안2"."""
    core = re.sub(r"[\s,.·()\[\]]+", "", name)
    for word in _BUSINESS_WORDS:
        core = core.replace(word, "")
    return core


def _neighbourhood(core: str) -> str:
    """핵심 이름 앞의 한글(동네 이름). 예: "대연5" → "대연"."""
    m = re.match(r"[가-힣]+", core)
    return m.group() if m else ""


def link_pairs(
    point_hits: Iterable[tuple[str, str, float]],
    ours: Iterable[tuple[str, str, str, str | None]],
    zones: Iterable[tuple[str, str | None]],
    *,
    office_hits: Iterable[tuple[str, str, float]] = (),
) -> dict[str, tuple[str, str]]:
    """우리 구역 → (경계, 잇는 방법). 일대일이고, 좌표 → 이름 → 조합 사무실 순으로 잇는다.

    - point_hits: (우리 구역, 그 지번 좌표를 품은 경계, 경계 면적). 여럿이면 가장 큰 경계.
    - ours: (우리 구역, 구역명, 사업 종류, 위치 원문). `LINKABLE_KINDS`만 잇는다.
    - zones: (경계, 이름). 이름은 양쪽에서 핵심 이름이 하나뿐일 때만 잇는다.
    - office_hits: (우리 구역, 그 조합 사무실 좌표를 품은 경계, 면적). 사무실은 구역 밖일 수 있어 빡빡하게 본다:
      아직 안 이어진 경계에 사무실이 하나만 들고, 구역 이름 앞의 동네(예: "대연5"의 "대연")가 사무실 주소에 있을 때만.
    """
    ours = [o for o in ours if o[2] in LINKABLE_KINDS]
    linkable = {o[0] for o in ours}
    taken: set[str] = set()
    pairs: dict[str, tuple[str, str]] = {}

    by_ours: dict[str, list[tuple[float, str]]] = defaultdict(list)
    for ours_id, zone_id, area in point_hits:
        if ours_id in linkable:
            by_ours[ours_id].append((area, zone_id))
    for ours_id in sorted(by_ours):
        for _, zone_id in sorted(by_ours[ours_id], key=lambda h: (-h[0], h[1])):
            if zone_id not in taken:
                pairs[ours_id] = (zone_id, "point")
                taken.add(zone_id)
                break

    zone_cores: dict[str, list[str]] = defaultdict(list)
    for zone_id, name in zones:
        if name and (core := core_name(name)):
            zone_cores[core].append(zone_id)
    our_cores: dict[str, list[str]] = defaultdict(list)
    for ours_id, area_name, *_ in ours:
        if core := core_name(area_name):
            our_cores[core].append(ours_id)
    for core, ids in sorted(our_cores.items()):
        found = zone_cores.get(core, [])
        if len(ids) != 1 or len(found) != 1:
            continue
        ours_id, zone_id = ids[0], found[0]
        if ours_id not in pairs and zone_id not in taken:
            pairs[ours_id] = (zone_id, "name")
            taken.add(zone_id)

    where = {o[0]: o[3] or "" for o in ours}
    names = {o[0]: o[1] for o in ours}
    offices: dict[str, list[tuple[float, str]]] = defaultdict(list)
    for ours_id, zone_id, area in office_hits:
        if ours_id in linkable and ours_id not in pairs and zone_id not in taken:
            offices[ours_id].append((area, zone_id))
    picks = {
        ours_id: max(found, key=lambda h: (h[0], h[1]))[1]
        for ours_id, found in offices.items()
        if (hood := _neighbourhood(core_name(names[ours_id]))) and hood in where[ours_id]
    }
    crowded = {z for z in picks.values() if list(picks.values()).count(z) > 1}  # 한 건물에 여러 조합
    for ours_id, zone_id in sorted(picks.items()):
        if zone_id not in crowded:
            pairs[ours_id] = (zone_id, "office")
            taken.add(zone_id)
    return pairs


# ST_BuildArea로 면을 만들고 4326 MultiPolygon으로 맞춘다. 면이 안 나오면 건너뛴다(학구도와 같음)
_INSERT_ZONE = text(
    f"""
    INSERT INTO redevelopment_zones
        (zone_id, layer, name, notice, designated_on, sigungu_code, geom, label_point, area_m2, base_date, loaded_at)
    SELECT :zone_id, :layer, :name, :notice, :designated_on, :sigungu_code, g, ST_PointOnSurface(g),
           ST_Area(g::geography), :base_date, :now
    FROM (
        SELECT ST_Multi(ST_CollectionExtract(ST_MakeValid(ST_Transform(
            ST_BuildArea(ST_GeomFromText(:wkt, {SOURCE_SRID})), 4326)), 3)) AS g
    ) built
    WHERE g IS NOT NULL AND NOT ST_IsEmpty(g)
    """
)
_DELETE_SMALL = text("DELETE FROM redevelopment_zones WHERE area_m2 < :min_area")
# 더 큰 다른 구역 안에 통째로 든 조각(구역 안의 시설·필지)
_DELETE_NESTED = text(
    """
    DELETE FROM redevelopment_zones inner_z
    USING redevelopment_zones outer_z
    WHERE outer_z.zone_id <> inner_z.zone_id AND outer_z.area_m2 > inner_z.area_m2
      AND ST_Covers(ST_Buffer(outer_z.geom::geography, :tolerance)::geometry, inner_z.geom)
    """
)
_OFFICE_HITS = text(
    """
    SELECT r.id AS ours_id, z.zone_id, z.area_m2
    FROM redevelopments r JOIN redevelopment_zones z ON ST_Contains(z.geom, r.office_geom)
    WHERE r.closed_at IS NULL AND r.office_geom IS NOT NULL
    """
)
_POINT_HITS = text(
    """
    SELECT r.id AS ours_id, z.zone_id, z.area_m2
    FROM redevelopments r JOIN redevelopment_zones z ON ST_Contains(z.geom, r.geom)
    WHERE r.closed_at IS NULL AND r.location_precise AND r.geom IS NOT NULL
    """
)
_OURS = text("SELECT id, area_name, kind, location FROM redevelopments WHERE closed_at IS NULL")
_ZONES = text("SELECT zone_id, name FROM redevelopment_zones")
_CLEAR_LINKS = text("UPDATE redevelopment_zones SET redevelopment_id = NULL, link_method = NULL")
_SET_LINK = text(
    "UPDATE redevelopment_zones SET redevelopment_id = :ours_id, link_method = :method WHERE zone_id = :zone_id"
)


async def relink(conn: AsyncConnection) -> tuple[int, int, int]:
    """경계와 우리 구역을 다시 잇는다. (좌표·이름·사무실로 이은 수). 정비사업 동기화 뒤에도 부른다."""
    hits = [(r.ours_id, r.zone_id, r.area_m2) for r in await conn.execute(_POINT_HITS)]
    offices = [(r.ours_id, r.zone_id, r.area_m2) for r in await conn.execute(_OFFICE_HITS)]
    ours = [(r.id, r.area_name, r.kind, r.location) for r in await conn.execute(_OURS)]
    zones = [(r.zone_id, r.name) for r in await conn.execute(_ZONES)]
    pairs = link_pairs(hits, ours, zones, office_hits=offices)
    await conn.execute(_CLEAR_LINKS)
    if pairs:
        await conn.execute(
            _SET_LINK, [{"ours_id": o, "zone_id": z, "method": m} for o, (z, m) in sorted(pairs.items())]
        )
    methods = [m for _, m in pairs.values()]
    return methods.count("point"), methods.count("name"), methods.count("office")


async def load_redevelopment_zones(
    engine: AsyncEngine, directory: Path, *, now: datetime, out: Callable[[str], None] = print
) -> RedevelopmentZoneReport:
    """있는 종류(정비구역·재정비촉진·정비예정)를 한 트랜잭션에서 지우고 다시 넣은 뒤 우리 구역과 잇는다."""
    layer_files = find_layer_files(directory)
    if "zone" not in layer_files:
        raise FileNotFoundError(f"정비구역 경계 파일이 없습니다: {directory / LAYER_DIRS['zone']}")
    zone_rows: list[dict] = []
    busan = facilities = 0
    base_dates: list[date] = []
    for layer, files in layer_files.items():
        rows = read_dbf(files.dbf)
        records = busan_zone_records(rows, layer)
        busan_rows = sum(1 for r in rows if _clean(r.get("A4")).startswith(BUSAN_PREFIX))
        busan += busan_rows
        facilities += busan_rows - len(records)
        shapes = read_polygons(files.shp, files.shx, [z.index for z in records])
        out(f"  {layer}: 부산 경계 {busan_rows:,}개 중 시설을 뺀 {len(records):,}개")
        base_dates += [z.base_date for z in records if z.base_date]
        zone_rows += [
            {
                "zone_id": z.zone_id,
                "layer": z.layer,
                "name": z.name,
                "notice": z.notice,
                "designated_on": z.designated_on,
                "sigungu_code": z.sigungu_code,
                "wkt": rings_wkt(shapes[z.index]),
                "base_date": z.base_date,
                "now": now,
            }
            for z in records
            if shapes.get(z.index)
        ]
    report = RedevelopmentZoneReport(base_date=max(base_dates, default=None))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM redevelopment_zones"))
        if zone_rows:
            await conn.execute(_INSERT_ZONE, zone_rows)
        inserted = (await conn.execute(text("SELECT count(*) FROM redevelopment_zones"))).scalar_one()
        small = (await conn.execute(_DELETE_SMALL, {"min_area": MIN_AREA_M2})).rowcount
        nested = (await conn.execute(_DELETE_NESTED, {"tolerance": NESTED_TOLERANCE_M})).rowcount
        report.zones = inserted - small - nested
        layers = await conn.execute(text("SELECT layer, count(*) FROM redevelopment_zones GROUP BY layer"))
        report.layers = {layer: n for layer, n in layers}
        report.skipped = {
            "시설": facilities,
            "도형 없음": busan - facilities - inserted,
            "1,000㎡ 미만": small,
            "다른 구역 안": nested,
        }
        report.linked_by_point, report.linked_by_name, report.linked_by_office = await relink(conn)
    return report

"""정비구역 경계(브이월드 용도지역지구정보 "도시및주거환경정비/정비구역" SHP)를 `redevelopment_zones`에
통째로 갈아 끼우고, 우리 정비사업 구역(`redevelopments`)과 잇는다(조각 여럿이 한 구역에
붙을 수 있다). 설계: docs/REDEVELOPMENT_ZONES.md

원천은 내려받아 둔 파일이다(브이월드 로그인 필요, API 호출 없음). 기본 위치는 `default_zone_dir()`의
`data/redevelopment`(git 제외)이고, `zones/*.shp|.shx|.dbf`를 읽는다. 좌표계 EPSG:5186, DBF는 cp949.
SHP 읽기는 학구도 적재(`school_zones`)의 함수를 그대로 쓴다.
"""

import json
import logging
import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from functools import cache
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.db.legal_dong import load_legal_dongs
from app.ingestion.school_zones import SOURCE_SRID, read_dbf, read_polygons, rings_wkt

logger = logging.getLogger(__name__)

BACKEND_DIR = Path(__file__).resolve().parents[2]
BUSAN_PREFIX = "26"
MIN_AREA_M2 = 1000.0  # 이보다 작은 조각은 구역이 아니다(실제 구역은 하위 10%도 약 8,000㎡)
NESTED_TOLERANCE_M = 1.0  # 다른 구역 안에 통째로 든 조각을 가릴 때의 여유
TWIN_AREA_RATIO = 0.99  # 서로를 품고 면적이 이만큼 같으면 같은 경계 두 벌로 본다
TOUCH_TOLERANCE_M = 1.0  # 이만큼 가까운 두 경계는 맞닿은 것으로 본다(구 경계로 나뉜 한 구역의 조각)
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


# 괄호 안 한두 글자 기호("(A)")는 이름의 일부라 남긴다. 그 밖의 괄호 내용(날짜·단지명)은 뺀다
_PAREN_NOTE = re.compile(r"\(([^)]*)\)")
# 사업 종류를 가리키는 말(이름에서 종류를 읽을 때 긴 것부터 본다)
_KIND_WORDS = [
    ("도시환경", "urban_environment"),
    ("주거환경", "residential_improvement"),
    ("재건축", "reconstruction"),
    ("재개발", "redevelopment"),
    ("주택개량", "redevelopment"),
]


def core_name(name: str) -> str:
    """이름에서 공백·문장부호·괄호 메모·사업 이름을 지운 핵심 이름. 예: "광안2주택재건축구역" → "광안2"."""
    text = _PAREN_NOTE.sub(lambda m: m.group(1) if re.fullmatch(r"[A-Za-z0-9]{1,2}", m.group(1)) else "", name)
    text = text.split("정비계획")[0]  # 고시 제목("… 정비계획 결정 및 … 고시")은 앞부분만
    if "-" in text and not re.match(r"[가-힣]+\d", text.split("-")[0].strip()):
        text = text.split("-", 1)[1]  # "주택재건축정비사업-남천2…"처럼 앞에 붙은 사업 설명
    core = re.sub(r"[\s,.·\[\]]+", "", text)
    for word in _BUSINESS_WORDS:
        core = core.replace(word, "")
    return core


def name_kind(name: str) -> str | None:
    """이름에 적힌 사업 종류. 없으면 None."""
    compact = re.sub(r"\s+", "", name)
    return next((kind for word, kind in _KIND_WORDS if word in compact), None)


def _neighbourhood(core: str) -> str:
    """핵심 이름 앞의 한글(동네 이름). 예: "대연5" → "대연"."""
    m = re.match(r"[가-힣]+", core)
    return m.group() if m else ""


@cache
def _busan_sigungu() -> tuple[dict[str, str], dict[str, set[str]]]:
    """(시군구 이름 → 코드, 법정동·읍면 이름 → 시군구 코드들). 법정동 마스터(regions 시드와 같은 CSV)에서 만든다."""
    by_name: dict[str, str] = {}
    by_dong: dict[str, set[str]] = defaultdict(set)
    for d in load_legal_dongs():
        if d.level == "sigungu":
            by_name[d.name] = d.code
        else:
            by_dong[d.name.split()[0]].add(d.code[:5])  # 기장군은 "기장읍 교리"처럼 리까지 있어 읍면만 본다
    return by_name, by_dong


def sigungu_of(location: str | None) -> str | None:
    """우리 구역 위치 원문에서 시군구 코드를 읽는다. 모르면 None.

    부산시 정비사업 원천에는 시군구 칸이 없어서 위치 원문(지번 또는 조합 사무실 주소)을 본다.
    1) 구·군 이름이 낱말로 하나만 있으면 그것("강서구"를 "서구"로 읽지 않게 낱말 단위로 본다).
    2) 없으면 법정동·읍면 이름으로 찾되, 그 이름이 한 시군구에만 있을 때만("송정동"은 해운대구·강서구 둘 다라 모른다).
       행정동 번호("대연6동")는 떼고 법정동("대연동")으로 본다.
    """
    if not location:
        return None
    by_name, by_dong = _busan_sigungu()
    words = re.findall(r"[가-힣0-9]+", location)
    gu = {by_name[w] for w in words if w in by_name}
    if len(gu) == 1:
        return gu.pop()
    if gu:
        return None
    found: set[str] = set()
    for w in words:
        dong = re.sub(r"\d+(동)$", r"\1", w)  # 대연6동 → 대연동
        for name in (w, dong):
            found |= by_dong.get(name, set())
    return found.pop() if len(found) == 1 else None


_SWAPPED = {"redevelopment", "reconstruction"}


def link_pairs(
    point_hits: Iterable[tuple[str, str, float]],
    ours: Iterable[tuple[str, str, str, str | None]],
    zones: Iterable[tuple[str, str | None]],
    *,
    office_hits: Iterable[tuple[str, str, float]] = (),
    zone_sigungu: Mapping[str, str] | None = None,
    ours_location_kind: Mapping[str, str] | None = None,
    zone_layers: Mapping[str, str] | None = None,
    zone_touches: Iterable[tuple[str, str]] = (),
) -> dict[str, tuple[str, str]]:
    """경계 → (우리 구역, 잇는 방법). 좌표 → 이름 → 조합 사무실 순으로 잇는다.

    경계 하나는 우리 구역 하나에만 붙는다. 우리 구역 하나에는 이름이 같은 조각(경계) 여럿이 붙을 수 있다.
    - point_hits: (우리 구역, 그 지번 좌표를 품은 경계, 경계 면적). 여럿이면 가장 큰 경계.
    - ours: (우리 구역, 구역명, 사업 종류, 위치 원문). `LINKABLE_KINDS`만 잇는다.
    - zones: (경계, 이름). 핵심 이름과 사업 종류가 같은 경계 묶음이 우리 쪽에도 하나뿐이면 잇는다(같은 번호의
      재개발·재건축을 가른다). 종류로 못 가르면 핵심 이름만으로, 양쪽에서 그 이름이 하나뿐일 때 잇는다
      (재개발 ↔ 재건축으로 엇갈리면 잇지 않는다). 좌표로 이어진 구역도 남은 같은 이름 조각을 더 받는다.
      같은 이름 조각들이 좌표로 서로 다른 우리 구역에 이어졌으면 그 이름은 믿지 않고 더 붙이지 않는다.
      우리 쪽에 그 이름(종류)의 구역이 없으면, 좌표로 이은 경계와 이름·종류가 같은 조각은 그 구역을 따라간다.
      다른 구에 같은 동 이름이 있을 수 있어(송정동: 해운대구·강서구) 시군구로 좁힌다. 우리 구역의 시군구는
      위치 원문(`sigungu_of`), 없으면 좌표로 이어진 경계의 시군구다.
      그래도 모르면 경계 묶음이 한 시군구 안일 때만 잇는다.
      다만 구 경계에서 둘로 나뉜 구역(양정2: 부산진구·연제구)은 조각이 서로 맞닿아 있으므로, 고른 조각과
      맞닿은 같은 이름 조각은 시군구가 달라도 함께 붙이고, 묶음 전체가 한 덩어리로 이어져 있으면 시군구를 몰라도 잇는다.
    - zone_sigungu: 경계 → 시군구 코드(A4). 없는 경계는 시군구를 모르는 것으로 보고 거르지 않는다.
    - ours_location_kind: 우리 구역 → 위치 종류(lot/office/none). 없으면 lot으로 본다. 지번(lot)의 시군구는 확정값이고,
      조합 사무실 주소의 시군구는 구역과 다를 수 있어 경계 묶음이 여러 시군구에 걸칠 때 고르는 데에만 쓴다.
    - zone_layers: 경계 → 종류(zone/promotion/planned). 좌표 단계는 정비구역·재정비촉진을 정비예정구역보다 먼저 본다.
    - zone_touches: 맞닿은 경계 쌍(`TOUCH_TOLERANCE_M` 안). 시군구로 거를 때 구 경계로 나뉜 조각을 살리는 데에만 쓴다.
    - office_hits: (우리 구역, 그 조합 사무실 좌표를 품은 경계, 면적). 사무실은 구역 밖일 수 있어 빡빡하게 본다:
      아직 안 이어진 경계에 사무실이 하나만 들고, 구역 이름 앞의 동네(예: "대연5"의 "대연")가 사무실 주소에 있을 때만.
    """
    ours = [o for o in ours if o[2] in LINKABLE_KINDS]
    linkable = {o[0] for o in ours}
    links: dict[str, tuple[str, str]] = {}  # 경계 → (우리 구역, 방법)
    done: set[str] = set()  # 이미 이어진 우리 구역

    def link(zone_ids: Iterable[str], ours_id: str, method: str) -> None:
        free = [z for z in zone_ids if z not in links]
        for zone_id in free:
            links[zone_id] = (ours_id, method)
        if free:
            done.add(ours_id)

    by_ours: dict[str, list[tuple[float, str]]] = defaultdict(list)
    for ours_id, zone_id, area in point_hits:
        if ours_id in linkable:
            by_ours[ours_id].append((area, zone_id))
    layer_of = dict(zone_layers or {})
    for ours_id in sorted(by_ours):
        # 정비예정구역은 정비구역을 덮을 만큼 클 수 있다. 종류(정비구역·재정비촉진 → 정비예정) 다음에 면적으로 고른다
        hits_in_order = sorted(by_ours[ours_id], key=lambda h: (layer_of.get(h[1]) == "planned", -h[0], h[1]))
        for _, zone_id in hits_in_order:
            if zone_id not in links:
                link([zone_id], ours_id, "point")
                break

    zones_by_kind: dict[tuple[str, str | None], list[str]] = defaultdict(list)
    zones_by_core: dict[str, set[str | None]] = defaultdict(set)
    zone_keys: dict[str, tuple[str, str | None]] = {}
    for zone_id, name in zones:
        if name and (core := core_name(name)):
            zone_keys[zone_id] = (core, name_kind(name))
            zones_by_kind[zone_keys[zone_id]].append(zone_id)
            zones_by_core[core].add(name_kind(name))
    ours_by_kind: dict[tuple[str, str], list[str]] = defaultdict(list)
    ours_by_core: dict[str, list[str]] = defaultdict(list)
    for ours_id, area_name, kind, _ in ours:
        if core := core_name(area_name):
            ours_by_kind[(core, kind)].append(ours_id)
            ours_by_core[core].append(ours_id)
    gu_of_zone = dict(zone_sigungu or {})
    where = {o[0]: o[3] or "" for o in ours}
    # 우리 구역의 확정 시군구: 구역 위치 원문(지번, 또는 번지 없는 "OO구 OO동 일원") → 좌표로 이어진 경계의 시군구 순.
    # 조합 사무실 주소의 시군구는 구역과 다를 수 있어(범천1: 경계는 부산진구, 사무실은 동구) 힌트로만 둔다
    location_kind = dict(ours_location_kind or {})
    read_gu = {o[0]: sigungu_of(o[3]) for o in ours}
    gu_of_ours = {o_id: gu if location_kind.get(o_id, "lot") != "office" else None for o_id, gu in read_gu.items()}
    office_gu = {o_id: gu for o_id, gu in read_gu.items() if location_kind.get(o_id, "lot") == "office"}
    for zone_id, (ours_id, _) in links.items():
        if gu_of_ours.get(ours_id) is None:
            gu_of_ours[ours_id] = gu_of_zone.get(zone_id)

    touches: dict[str, set[str]] = defaultdict(set)
    for a, b in zone_touches:
        touches[a].add(b)
        touches[b].add(a)

    def with_touching(picked: list[str], zone_ids: list[str]) -> list[str]:
        """고른 조각과 (이어서) 맞닿은 묶음 안 조각을 더한다. 구 경계로 나뉜 한 구역의 나머지 조각이다."""
        bundle, found, todo = set(zone_ids), set(picked), list(picked)
        while todo:
            for z in touches[todo.pop()] & bundle - found:
                found.add(z)
                todo.append(z)
        return [z for z in zone_ids if z in found]

    def same_gu(zone_ids: list[str], ours_id: str) -> list[str]:
        """우리 시군구 안의 조각과 그것에 맞닿은 조각. 확정 시군구를 모르면 묶음이 한 시군구 안이거나 한 덩어리로
        이어졌을 때 그대로 잇고, 아니면 사무실 시군구에 든 조각(과 맞닿은 조각)만 고른다(사무실 시군구도 없거나
        묶음에 없으면 잇지 않는다)."""
        gu = gu_of_ours.get(ours_id)
        if gu is not None:
            return with_touching([z for z in zone_ids if gu_of_zone.get(z) in (None, gu)], zone_ids)
        spread = {gu_of_zone[z] for z in zone_ids if z in gu_of_zone}
        if len(spread) <= 1 or len(with_touching(zone_ids[:1], zone_ids)) == len(zone_ids):
            return zone_ids
        hint = office_gu.get(ours_id)
        return with_touching([z for z in zone_ids if gu_of_zone.get(z) == hint], zone_ids) if hint in spread else []

    # 좌표로 이어진 구역이 경계 이름 묶음마다 누구인지. 둘 이상이면 그 이름은 서로 다른 구역에 쓰인 것이다
    point_owners: dict[tuple[str, str | None], set[str]] = defaultdict(set)
    for zone_id, (ours_id, _) in links.items():
        if zone_id in zone_keys:
            point_owners[zone_keys[zone_id]].add(ours_id)

    def by_name(key: tuple[str, str | None], ours_id: str) -> None:
        # 같은 이름 조각이 좌표로 서로 다른 구역에 이어졌으면 남은 조각을 이름으로 붙이지 않는다(리뷰 Psh-09 안전장치)
        if len(point_owners.get(key, ())) <= 1:
            link(same_gu(zones_by_kind[key], ours_id), ours_id, "name")

    # 좌표로 이어진 구역도 이름 단계를 거친다(나머지 조각). 이미 다른 구역에 붙은 경계는 link()가 건너뛴다
    for (core, kind), ids in sorted(ours_by_kind.items()):
        if len(ids) != 1:
            continue
        if (core, kind) in zones_by_kind:
            by_name((core, kind), ids[0])
        elif len(ours_by_core[core]) == 1 and len(zones_by_core.get(core, ())) == 1:
            # 종류가 없거나 옛 이름(도시환경정비 → 재개발)이어도 그 동네 번호가 양쪽에 하나뿐이면 같은 구역이다.
            # 재개발 ↔ 재건축은 같은 번호라도 다른 사업일 수 있어 잇지 않는다
            (zone_kind,) = zones_by_core[core]
            if {zone_kind, kind} != _SWAPPED:
                by_name((core, zone_kind), ids[0])
    # 우리 쪽에 그 이름(종류)의 구역이 없으면, 좌표로 이은 경계와 이름·종류가 같은 조각은 그 구역을 따라간다
    # (우리 구역 이름과 경계 이름이 다른 경우. 리뷰 Psh-09)
    for key, owners in sorted(point_owners.items(), key=lambda kv: (kv[0][0], kv[0][1] or "")):
        claimed = ours_by_kind.get(key, []) if key[1] else ours_by_core.get(key[0], [])
        if len(owners) == 1 and not claimed:
            by_name(key, next(iter(owners)))

    names = {o[0]: o[1] for o in ours}
    offices: dict[str, list[tuple[float, str]]] = defaultdict(list)
    for ours_id, zone_id, area in office_hits:
        if ours_id in linkable and ours_id not in done and zone_id not in links:
            offices[ours_id].append((area, zone_id))
    picks = {
        ours_id: max(found, key=lambda h: (h[0], h[1]))[1]
        for ours_id, found in offices.items()
        if (hood := _neighbourhood(core_name(names[ours_id]))) and hood in where[ours_id]
    }
    crowded = {z for z in picks.values() if list(picks.values()).count(z) > 1}  # 한 건물에 여러 조합
    for ours_id, zone_id in sorted(picks.items()):
        if zone_id not in crowded:
            link([zone_id], ours_id, "office")
    return links


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
# 같은 종류의 더 큰 다른 구역 안에 통째로 든 조각(구역 안의 시설·필지). 종류가 다르면(정비예정구역이
# 정비구역을 덮는 경우 등) 둘 다 남긴다.
# 면적이 거의 같은 두 벌(쌍둥이 경계)은 서로를 품으니 이름 있는 쪽을 남긴다.
# 이름이 둘 다 있거나 없으면 큰 쪽, 면적도 같으면(똑같은 경계를 두 구청이 올린 것) ID가 큰 쪽을 남긴다
_DELETE_NESTED = text(
    """
    DELETE FROM redevelopment_zones inner_z
    USING redevelopment_zones outer_z
    WHERE outer_z.zone_id <> inner_z.zone_id AND outer_z.layer = inner_z.layer
      AND CASE
            WHEN LEAST(outer_z.area_m2, inner_z.area_m2) >= GREATEST(outer_z.area_m2, inner_z.area_m2) * :twin_ratio
            THEN (outer_z.name IS NOT NULL, outer_z.area_m2, outer_z.zone_id)
                 > (inner_z.name IS NOT NULL, inner_z.area_m2, inner_z.zone_id)
            ELSE outer_z.area_m2 > inner_z.area_m2
          END
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
_OURS = text("SELECT id, area_name, kind, location, location_kind FROM redevelopments WHERE closed_at IS NULL")
_ZONES = text("SELECT zone_id, name, sigungu_code, layer FROM redevelopment_zones")
# 맞닿은 이름 있는 경계 쌍(구 경계로 나뉜 한 구역의 조각을 알아보는 데 쓴다). && 로 먼저 좁힌다(약 10m)
_TOUCHES = text(
    """
    SELECT a.zone_id AS a, b.zone_id AS b
    FROM redevelopment_zones a JOIN redevelopment_zones b
      ON a.zone_id < b.zone_id AND a.geom && ST_Expand(b.geom, 0.0001)
     AND ST_DWithin(a.geom::geography, b.geom::geography, :tolerance)
    WHERE a.name IS NOT NULL AND b.name IS NOT NULL
    """
)
_METHOD_ORDER = ("point", "name", "office")
# 적재 기록. 걸러서 0건이어도 "실었다"를 알 수 있게 etl_runs에 남긴다(조회 API의 data_as_of)
_RECORD_RUN = text(
    """
    INSERT INTO etl_runs (job, status, started_at, finished_at, rows_fetched, rows_inserted, report)
    VALUES ('redevelopment_zone', 'success', :now, :now, :fetched, :zones, CAST(:report AS jsonb))
    """
)
_CLEAR_LINKS = text("UPDATE redevelopment_zones SET redevelopment_id = NULL, link_method = NULL")
_SET_LINK = text(
    "UPDATE redevelopment_zones SET redevelopment_id = :ours_id, link_method = :method WHERE zone_id = :zone_id"
)


async def relink(conn: AsyncConnection) -> tuple[int, int, int]:
    """경계와 우리 구역을 다시 잇는다. (좌표·이름·사무실로 이은 수). 정비사업 동기화 뒤에도 부른다."""
    hits = [(r.ours_id, r.zone_id, r.area_m2) for r in await conn.execute(_POINT_HITS)]
    offices = [(r.ours_id, r.zone_id, r.area_m2) for r in await conn.execute(_OFFICE_HITS)]
    ours_rows = (await conn.execute(_OURS)).all()
    ours = [(r.id, r.area_name, r.kind, r.location) for r in ours_rows]
    zone_rows = (await conn.execute(_ZONES)).all()
    zones = [(r.zone_id, r.name) for r in zone_rows]
    links = link_pairs(
        hits,
        ours,
        zones,
        office_hits=offices,
        zone_sigungu={r.zone_id: r.sigungu_code for r in zone_rows},
        ours_location_kind={r.id: r.location_kind for r in ours_rows},
        zone_layers={r.zone_id: r.layer for r in zone_rows},
        zone_touches=[(r.a, r.b) for r in await conn.execute(_TOUCHES, {"tolerance": TOUCH_TOLERANCE_M})],
    )
    await conn.execute(_CLEAR_LINKS)
    if links:
        await conn.execute(
            _SET_LINK, [{"ours_id": o, "zone_id": z, "method": m} for z, (o, m) in sorted(links.items())]
        )
    # 우리 구역 수로 센다(조각 여럿이 붙어도 하나). 좌표로 이은 구역에 이름 조각이 더 붙어도 좌표로 센다
    best: dict[str, str] = {}
    for ours_id, method in links.values():
        if ours_id not in best or _METHOD_ORDER.index(method) < _METHOD_ORDER.index(best[ours_id]):
            best[ours_id] = method
    methods = list(best.values())
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
    # zone_id(A1)는 세 파일 공통 PK라 겹치면 적재 전체가 롤백된다. 먼저 나온 것(정비구역 → 재정비촉진 → 정비예정)만 둔다
    seen: set[str] = set()
    unique_rows: list[dict] = []
    for row in zone_rows:
        if row["zone_id"] not in seen:
            seen.add(row["zone_id"])
            unique_rows.append(row)
    duplicates = len(zone_rows) - len(unique_rows)
    if duplicates:
        logger.warning("정비구역 경계 ID 중복 %d건을 건너뜀(먼저 나온 것만 싣는다)", duplicates)
        out(f"  경고: 경계 ID가 겹친 {duplicates:,}개는 건너뜀")
    zone_rows = unique_rows
    report = RedevelopmentZoneReport(base_date=max(base_dates, default=None))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM redevelopment_zones"))
        if zone_rows:
            await conn.execute(_INSERT_ZONE, zone_rows)
        inserted = (await conn.execute(text("SELECT count(*) FROM redevelopment_zones"))).scalar_one()
        small = (await conn.execute(_DELETE_SMALL, {"min_area": MIN_AREA_M2})).rowcount
        nested = (
            await conn.execute(_DELETE_NESTED, {"tolerance": NESTED_TOLERANCE_M, "twin_ratio": TWIN_AREA_RATIO})
        ).rowcount
        report.zones = inserted - small - nested
        layers = await conn.execute(text("SELECT layer, count(*) FROM redevelopment_zones GROUP BY layer"))
        report.layers = {layer: n for layer, n in layers}
        report.skipped = {
            "시설": facilities,
            "도형 없음": busan - facilities - duplicates - inserted,
            "ID 중복": duplicates,
            "1,000㎡ 미만": small,
            "다른 구역 안": nested,
        }
        report.linked_by_point, report.linked_by_name, report.linked_by_office = await relink(conn)
        await conn.execute(
            _RECORD_RUN,
            {
                "now": now,
                "fetched": busan,
                "zones": report.zones,
                "report": json.dumps({"layers": report.layers, "skipped": report.skipped}, ensure_ascii=False),
            },
        )
    return report

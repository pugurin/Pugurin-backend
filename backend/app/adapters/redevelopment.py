"""부산광역시 정비사업 정보(공공데이터포털, 부산광역시 도시정비과). 부산 재개발·재건축 구역과 사업추진단계.

- 좌표가 없다. 위치 텍스트(`location`)가 지번처럼 보일 때만 지오코딩할 수 있게 검색어를 만든다.
  절반쯤은 비었거나 조합 사무실 주소다(`location_kind`로 구분).
- 사업 유형 칸이 없어 구역명에서 읽는다. 단계 원문은 순서 있는 단계로 맞추고 원문도 남긴다.
- 값이 모두 문자열이고 "미선정"·빈 값이 섞인다.

허용한 칸만 골라 옮긴다(원문을 밖으로 넘기지 않는다). `phone`은 개인 휴대전화일 수 있어 옮기지 않는다.
응답 감싸기 해석은 `parse_page` 한 곳에 모았다(실호출로 확인한 모양 + 단건·빈 목록·최상위 오류 코드).
"""

import asyncio
import hashlib
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx

from app.adapters.json_http import JsonApiClient
from app.adapters.molit.types import QuotaExceeded, SourceAuthError
from app.repositories.ports import SourceUnavailable
from app.repositories.types import LocationKind, RedevelopmentKind, RedevelopmentStage

PAGE_SIZE = 100
MAX_PAGES = 50
NO_CODE_PREFIX = "NOCODE_"  # aCode가 빈 구역의 ID 접두어. 이런 구역은 시 홈페이지 주소를 만들 수 없다
_NULL_TEXT = {"", "-", "미선정"}

# 긴 말부터 검사한다("소규모재건축"이 "재건축"보다 먼저)
_KIND_WORDS: tuple[tuple[str, RedevelopmentKind], ...] = (
    ("소규모재건축", RedevelopmentKind.small_reconstruction),
    ("소규모재개발", RedevelopmentKind.small_redevelopment),
    ("주거환경개선", RedevelopmentKind.residential_improvement),
    ("가로주택", RedevelopmentKind.street_housing),
    ("도시환경", RedevelopmentKind.urban_environment),
    ("재건축", RedevelopmentKind.reconstruction),
    ("재개발", RedevelopmentKind.redevelopment),
)

STAGE_ORDER: dict[RedevelopmentStage, int] = {
    RedevelopmentStage.cancelled: 0,
    RedevelopmentStage.planned: 1,
    RedevelopmentStage.designated: 2,
    RedevelopmentStage.committee: 3,
    RedevelopmentStage.association: 4,
    RedevelopmentStage.review: 5,
    RedevelopmentStage.project_approved: 6,
    RedevelopmentStage.disposal_approved: 7,
    RedevelopmentStage.construction: 8,
    RedevelopmentStage.completed: 9,
    RedevelopmentStage.transferred: 10,
    RedevelopmentStage.dissolved: 11,
}
_STAGE_WORDS: dict[str, RedevelopmentStage] = {
    "해제": RedevelopmentStage.cancelled,
    "취소": RedevelopmentStage.cancelled,
    "예정구역": RedevelopmentStage.planned,
    "기본계획": RedevelopmentStage.planned,
    "정비구역지정": RedevelopmentStage.designated,
    "구역지정": RedevelopmentStage.designated,
    "정비계획": RedevelopmentStage.designated,
    "추진위원회": RedevelopmentStage.committee,
    "조합설립": RedevelopmentStage.association,
    "건축심의": RedevelopmentStage.review,
    "통합심의": RedevelopmentStage.review,
    "사업시행": RedevelopmentStage.project_approved,
    "관리처분": RedevelopmentStage.disposal_approved,
    "착공": RedevelopmentStage.construction,
    "이주": RedevelopmentStage.construction,
    "철거": RedevelopmentStage.construction,
    "준공": RedevelopmentStage.completed,
    "이전고시": RedevelopmentStage.transferred,
    "조합해산": RedevelopmentStage.dissolved,
    "청산": RedevelopmentStage.dissolved,
}
# 긴(구체적인) 말부터: "정비계획 수립 및 정비구역 지정"은 designated, "정비구역 해제"는 cancelled
_STAGE_ORDERED = sorted(_STAGE_WORDS.items(), key=lambda kv: -len(kv[0]))

_NUMBER = re.compile(r"(-?\d+(?:\.\d+)?)(?:㎡|m2|%|세대|동|명)?")
_PAREN = re.compile(r"\([^)]*\)?")
# 사무실 주소 표시: n층, n호, 빌딩, 상가, 도로명 + 건물번호("냉정로 266", "법원로32번길 15")
_OFFICE = re.compile(r"\d+\s*층|\d+\s*호|빌딩|상가|[가-힣0-9]+(?:로|길)\s*\d")
# 구역 지번: 동·가·리 이름 + (산) 지번. "감천1동 202", "중앙동4가 33", "영주동 산 10-3"
_LOT = re.compile(r"[가-힣0-9]+(?:동|가|리)\s*(?:산\s*)?\d+(?:-\d+)?")


@dataclass(frozen=True)
class RedevelopmentItem:
    id: str  # aCode(예: BARA_0000000000056). 비었으면 NOCODE_ + 구역명 해시
    area_name: str
    kind: RedevelopmentKind
    step_raw: str | None
    stage: RedevelopmentStage
    stage_order: int | None
    location: str | None
    location_kind: LocationKind
    geocode_query: str | None  # location_kind가 lot일 때만
    area_m2: float | None
    households: int | None
    buildings: int | None
    union_members: int | None
    floor_area_ratio: float | None
    building_coverage_ratio: float | None
    scale: str | None
    business_entity: str | None
    contractor: str | None
    engineer: str | None
    architect: str | None
    specialists: str | None
    tel: str | None
    images: dict[str, str]  # view(조감도) / location(위치도) / panorama(전경) / layout(배치도), 있는 것만


def _text(v) -> str | None:
    s = " ".join(str(v).split()) if v is not None else ""
    return None if s in _NULL_TEXT else s


def parse_number(v) -> float | None:
    """쉼표·공백·단위(㎡, %, 세대, 동, 명)를 떼고 읽는다. "미선정"·빈 값·범위·설명이 붙은 값은 None."""
    s = _text(v)
    if s is None:
        return None
    m = _NUMBER.fullmatch(s.replace(",", "").replace(" ", ""))
    return float(m.group(1)) if m else None


def _int(v) -> int | None:
    n = parse_number(v)
    return round(n) if n is not None else None


def classify_kind(area_name: str) -> RedevelopmentKind:
    compact = "".join(area_name.split())
    return next((kind for word, kind in _KIND_WORDS if word in compact), RedevelopmentKind.other)


def normalize_stage(step) -> tuple[RedevelopmentStage, int | None]:
    compact = "".join((_text(step) or "").split())
    for word, stage in _STAGE_ORDERED:
        if compact and word in compact:
            return stage, STAGE_ORDER[stage]
    return RedevelopmentStage.unknown, None


def classify_location(location) -> tuple[LocationKind, str | None]:
    """위치 텍스트 → (종류, 지오코딩 검색어). 사무실 주소는 구역 위치가 아니라서 지오코딩하지 않는다."""
    s = _text(location)
    if s is None:
        return LocationKind.none, None
    bare = " ".join(_PAREN.sub(" ", s).split())
    if _OFFICE.search(bare):
        return LocationKind.office, None
    lot = _LOT.search(bare)
    if lot is None:
        return LocationKind.none, None
    query = " ".join(bare[: lot.end()].replace("번지", " ").replace("일원", " ").split())
    if not query.startswith(("부산광역시", "부산 ")):  # "부산진구"는 접두어가 아니다
        query = f"부산광역시 {query}"
    return LocationKind.lot, query


def zone_id(code, area_name: str) -> str:
    c = _text(code)
    if c:
        return c
    normalized = "".join(area_name.split())
    return NO_CODE_PREFIX + hashlib.sha1(normalized.encode()).hexdigest()[:16]


def zone_from_row(row: dict) -> RedevelopmentItem:
    name = _text(row.get("areaName")) or ""
    step = _text(row.get("step"))
    stage, order = normalize_stage(step)
    location = _text(row.get("location"))
    location_kind, query = classify_location(location)
    images = {
        key: url
        for key, field in (
            ("view", "viewImgPath"),
            ("location", "loctImgPath"),
            ("panorama", "panoImgPath"),
            ("layout", "placeImgPath"),
        )
        if (url := _text(row.get(field)))
    }
    return RedevelopmentItem(
        id=zone_id(row.get("aCode"), name),
        area_name=name,
        kind=classify_kind(name),
        step_raw=step,
        stage=stage,
        stage_order=order,
        location=location,
        location_kind=location_kind,
        geocode_query=query,
        area_m2=parse_number(row.get("areaUnit")),
        households=_int(row.get("generationJoo")),
        buildings=_int(row.get("wingNum")),
        union_members=_int(row.get("guildMemNum")),
        floor_area_ratio=parse_number(row.get("gage")),
        building_coverage_ratio=parse_number(row.get("longPyeyul")),
        scale=_text(row.get("scale")),
        business_entity=_text(row.get("businessEntities")),
        contractor=_text(row.get("contractor")),
        engineer=_text(row.get("engineer")),
        architect=_text(row.get("architect")),
        specialists=_text(row.get("specialists")),
        tel=_text(row.get("telNo")),
        images=images,
    )


# 공공데이터포털 공통 오류 코드
_AUTH_CODES = {"20", "30", "31", "32"}  # 접근 거부, 미등록 키, 기한 만료, 미등록 IP
_QUOTA_CODES = {"22"}
_NO_DATA_CODES = {"03"}


def parse_page(data: dict) -> tuple[list[dict], int]:
    """응답 한 쪽 → (행 목록, totalCount).

    실호출로 확인한 모양은 `{"response": {"header": {...}, "body": {"totalCount": "351", "items": {"item": [...]}}}}`다.
    `item`이 객체 하나로 오거나, `items`가 빈 문자열·null이거나, header·body가 최상위에 있어도 읽는다.
    """
    root = data.get("response") if isinstance(data.get("response"), dict) else data
    header = root.get("header") if isinstance(root.get("header"), dict) else root
    code = str(header.get("resultCode") or "").strip()
    message = header.get("resultMsg")
    if code in _AUTH_CODES:
        raise SourceAuthError(f"정비사업 정보 인증키 오류 {code}: {message}")
    if code in _QUOTA_CODES:
        raise QuotaExceeded(f"정비사업 정보 호출 한도 초과: {message}")
    if code in _NO_DATA_CODES:
        return [], 0
    if code not in ("00", "0"):
        raise SourceUnavailable(f"정비사업 정보 오류 {code or '(코드 없음)'}: {message}")
    body = root.get("body") if isinstance(root.get("body"), dict) else root
    items = body.get("items")
    rows = items.get("item") if isinstance(items, dict) else items
    if isinstance(rows, dict):  # 1건이면 배열이 아니라 객체로 온다
        rows = [rows]
    if not isinstance(rows, list):  # "" 또는 None: 데이터 없음
        rows = []
    total = parse_number(body.get("totalCount"))
    return [r for r in rows if isinstance(r, dict)], int(total or 0)


class BusanRedevelopmentClient(JsonApiClient):
    """부산광역시_정비사업 정보 `getMaintenanceBusiness1`. 한 번에 100행, 전체 350건 안팎."""

    def __init__(
        self,
        key: str,
        *,
        base_url: str = "https://apis.data.go.kr/6260000/MaintenanceBusinessStatus1",
        http: httpx.AsyncClient | None = None,
        retries: int = 2,
        backoff_seconds: float = 1.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        super().__init__(http, retries, backoff_seconds, sleep)
        self._key = key
        self._url = f"{base_url.rstrip('/')}/getMaintenanceBusiness1"

    async def zones(self) -> list[RedevelopmentItem]:
        rows: list[dict] = []
        for page in range(1, MAX_PAGES + 1):
            params = {"serviceKey": self._key, "pageNo": page, "numOfRows": PAGE_SIZE, "resultType": "json"}
            page_rows, total = parse_page(await self.get_json(self._url, params, "정비사업 정보"))
            rows.extend(page_rows)
            if not page_rows or len(rows) >= total:
                break
        else:
            raise SourceUnavailable(f"정비사업 정보 페이지가 {MAX_PAGES}개를 넘었습니다")
        return [zone_from_row(r) for r in rows]

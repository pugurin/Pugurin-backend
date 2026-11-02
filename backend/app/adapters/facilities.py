"""생활 시설 원천 두 곳.

- 학교: 나이스 교육정보 개방 포털(open.neis.go.kr) `schoolInfo`. 부산광역시교육청(C10) 학교 전부.
  좌표가 없어 주소를 지오코딩한다.
- 병원: 건강보험심사평가원 병원정보서비스(공공데이터포털) `getHospBasisList`. 부산(sidoCd 210000) 요양기관 전부.
  좌표가 온다.

허용한 칸만 골라 옮긴다(원문을 밖으로 넘기지 않는다).
"""

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date

import httpx

from app.adapters.molit.types import QuotaExceeded, SourceAuthError
from app.repositories.ports import SourceUnavailable

BUSAN_EDU_OFFICE = "C10"
BUSAN_SIDO_CD = "210000"
MAX_PAGES = 50


@dataclass(frozen=True)
class SchoolItem:
    id: str  # 표준 학교코드. 아직 없는 (가칭) 학교는 이름으로 만든 TMP-…
    name: str
    kind: str | None  # 초등학교 / 중학교 / 고등학교 / 특수학교 / 각종학교 …
    foundation: str | None  # 공립 / 사립 / 국립
    coed: str | None  # 남여공학 / 남 / 여
    address: str
    tel: str | None
    homepage: str | None
    opened_on: date | None  # 개교기념일(FOAS_MEMRD). 미래면 아직 개교 전


@dataclass(frozen=True)
class HospitalItem:
    id: str  # 요양기관기호(ykiho, 암호화된 문자열)
    name: str
    class_name: str | None  # 상급종합 / 종합병원 / 병원 / 의원 / 치과의원 / 한의원 / 요양병원 …
    address: str
    lat: float | None
    lng: float | None
    tel: str | None
    homepage: str | None
    doctors: int | None
    opened_on: date | None


def _text(v) -> str | None:
    s = str(v).strip() if v is not None else ""
    return s or None


def _ymd(v) -> date | None:
    s = str(v or "").strip()
    try:
        return date(int(s[:4]), int(s[4:6]), int(s[6:8])) if len(s) == 8 and s.isdigit() else None
    except ValueError:
        return None


def _url(v) -> str | None:
    s = _text(v)
    return s if s and s.rstrip("/") not in ("http:", "https:", "http://", "https://") else None


def _float(v) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def school_from_row(row: dict) -> SchoolItem:
    name = _text(row.get("SCHUL_NM")) or ""
    code = _text(row.get("SD_SCHUL_CODE"))
    return SchoolItem(
        id=code or "TMP-" + hashlib.sha1(name.encode()).hexdigest()[:12],
        name=name,
        kind=_text(row.get("SCHUL_KND_SC_NM")),
        foundation=_text(row.get("FOND_SC_NM")),
        coed=_text(row.get("COEDU_SC_NM")),
        address=_text(row.get("ORG_RDNMA")) or "",
        tel=_text(row.get("ORG_TELNO")),
        homepage=_url(row.get("HMPG_ADRES")),
        opened_on=_ymd(row.get("FOAS_MEMRD")) or _ymd(row.get("FOND_YMD")),
    )


def hospital_from_row(row: dict) -> HospitalItem:
    count = row.get("drTotCnt")
    return HospitalItem(
        id=str(row["ykiho"]),
        name=_text(row.get("yadmNm")) or "",
        class_name=_text(row.get("clCdNm")),
        address=_text(row.get("addr")) or "",
        lat=_float(row.get("YPos")),
        lng=_float(row.get("XPos")),
        tel=_text(row.get("telno")),
        homepage=_url(row.get("hospUrl")),
        doctors=int(count) if isinstance(count, int | float) or str(count or "").isdigit() else None,
        opened_on=_ymd(row.get("estbDd")),
    )


class _Http:
    def __init__(self, http, retries, backoff, sleep):
        self._http = http or httpx.AsyncClient(timeout=30)
        self._retries, self._backoff, self._sleep = retries, backoff, sleep
        self.calls_made = 0

    async def get_json(self, url: str, params: dict, name: str) -> dict:
        last: Exception | None = None
        for attempt in range(self._retries + 1):
            self.calls_made += 1
            try:
                response = await self._http.get(url, params=params)
            except httpx.TransportError as e:
                last = e
            else:
                code = response.status_code
                if code in (401, 403):
                    raise SourceAuthError(f"{name} 인증 거부(HTTP {code})")
                if code == 429:
                    raise QuotaExceeded(f"{name} 호출 한도 초과(HTTP 429)")
                if code >= 500:
                    last = SourceUnavailable(f"{name} 서버 오류(HTTP {code})")
                elif code >= 400:
                    raise SourceUnavailable(f"{name} 요청 오류(HTTP {code})")
                else:
                    try:
                        return response.json()
                    except ValueError:
                        # 공공데이터포털은 키 오류를 XML로 돌려준다
                        text = response.text
                        if "SERVICE_KEY" in text or "SERVICE KEY" in text:
                            raise SourceAuthError(f"{name} 인증키 오류") from None
                        if "LIMITED" in text:
                            raise QuotaExceeded(f"{name} 호출 한도 초과") from None
                        raise SourceUnavailable(f"{name} 응답이 JSON이 아닙니다") from None
            if attempt < self._retries:
                await self._sleep(self._backoff * 2**attempt)
        raise SourceUnavailable(f"{name} 호출 실패: {last}") from last

    async def aclose(self) -> None:
        await self._http.aclose()


class NeisSchoolClient(_Http):
    """나이스 교육정보 개방 포털. 한 번에 최대 1,000행."""

    def __init__(
        self,
        key: str,
        *,
        base_url: str = "https://open.neis.go.kr/hub",
        http: httpx.AsyncClient | None = None,
        retries: int = 2,
        backoff_seconds: float = 1.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        super().__init__(http, retries, backoff_seconds, sleep)
        self._key = key
        self._url = f"{base_url.rstrip('/')}/schoolInfo"

    async def schools(self, office: str = BUSAN_EDU_OFFICE) -> list[SchoolItem]:
        rows: list[dict] = []
        for page in range(1, MAX_PAGES + 1):
            params = {"KEY": self._key, "Type": "json", "ATPT_OFCDC_SC_CODE": office, "pIndex": page, "pSize": 1000}
            data = await self.get_json(self._url, params, "나이스")
            if "schoolInfo" not in data:
                result = data.get("RESULT") or {}
                code = result.get("CODE", "")
                if code == "INFO-200":  # 데이터 없음(마지막 페이지 다음)
                    break
                if code in ("ERROR-290", "ERROR-300"):
                    raise SourceAuthError(f"나이스 인증키 오류: {result.get('MESSAGE')}")
                if code == "ERROR-337":
                    raise QuotaExceeded(f"나이스 호출 한도 초과: {result.get('MESSAGE')}")
                raise SourceUnavailable(f"나이스 오류 {code}: {result.get('MESSAGE')}")
            head, body = data["schoolInfo"][0]["head"], data["schoolInfo"][1]["row"]
            rows.extend(body)
            if len(rows) >= int(head[0]["list_total_count"]) or not body:
                break
        else:
            raise SourceUnavailable(f"나이스 페이지가 {MAX_PAGES}개를 넘었습니다")
        return [school_from_row(r) for r in rows]


class HiraHospitalClient(_Http):
    """건강보험심사평가원 병원정보서비스(공공데이터포털). 한 번에 최대 1,000행."""

    def __init__(
        self,
        key: str,
        *,
        base_url: str = "https://apis.data.go.kr/B551182/hospInfoServicev2",
        http: httpx.AsyncClient | None = None,
        retries: int = 2,
        backoff_seconds: float = 1.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        super().__init__(http, retries, backoff_seconds, sleep)
        self._key = key
        self._url = f"{base_url.rstrip('/')}/getHospBasisList"

    async def hospitals(self, sido_cd: str = BUSAN_SIDO_CD) -> list[HospitalItem]:
        rows: list[dict] = []
        for page in range(1, MAX_PAGES + 1):
            params = {"serviceKey": self._key, "sidoCd": sido_cd, "numOfRows": 1000, "pageNo": page, "_type": "json"}
            data = await self.get_json(self._url, params, "병원정보")
            response = data.get("response") or {}
            header = response.get("header") or {}
            if header.get("resultCode") != "00":
                raise SourceUnavailable(f"병원정보 오류 {header.get('resultCode')}: {header.get('resultMsg')}")
            body = response.get("body") or {}
            items = (body.get("items") or {}) if isinstance(body.get("items"), dict) else {}
            page_rows = items.get("item") or []
            if isinstance(page_rows, dict):  # 1건이면 배열이 아니라 객체로 온다
                page_rows = [page_rows]
            rows.extend(page_rows)
            if not page_rows or len(rows) >= int(body.get("totalCount") or 0):
                break
        else:
            raise SourceUnavailable(f"병원정보 페이지가 {MAX_PAGES}개를 넘었습니다")
        return [hospital_from_row(r) for r in rows]

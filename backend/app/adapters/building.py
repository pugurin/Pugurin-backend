"""건축물대장(국토교통부 건축HUB, 공공데이터포털 `BldRgstHubService`)에서 아파트 평형별 공급면적을 얻는다.

전유공용면적(`getBrExposPubuseAreaInfo`)은 호마다 전유 1행 + 공용 여러 행이 붙어 온다.
공급면적 = 전유 + **주거공용**(계단·승강기·복도·벽체 등). 지하주차장·관리사무소·커뮤니티 같은 기타공용은 뺀다.
한 페이지 최대 100행이라 큰 단지는 수백 페이지다. 그래서 앞 페이지 몇 장만 받아 평형별 표본을 모은다.

세대수는 받지 않는다. 한 필지에 단지가 여럿(엘지메트로시티 4-2단지 등)이면 대장은 필지 전체 세대수를 준다.

PNU(19자리) = 법정동코드 10 + 대지구분 1(1 일반·2 산) + 본번 4 + 부번 4. 건축물대장의 `platGbCd`는 0 일반·1 산이다.
"""

import asyncio
from collections import Counter, defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx

from app.adapters.molit.types import QuotaExceeded, SourceAuthError
from app.repositories.ports import SourceUnavailable

PYEONG_M2 = 3.3058
ROWS_PER_PAGE = 100  # 원천 최대

# 공용 행의 용도(etcPurps) 글자로 주거공용인지 가른다. 기타공용 단어가 하나라도 있으면 기타공용이다
_RESIDENTIAL = (
    "계단",
    "승강기",
    "엘리베이터",
    "E.V",
    "EV",
    "ELEV",
    "E/V",
    "홀",
    "복도",
    "벽체",
    "코아",
    "코어",
    "전실",
    "현관",
)
_OTHER = (
    "주차", "관리", "경비", "경로", "어린이집", "보육", "기계", "전기", "발전", "펌프", "통신", "MDF", "감시", "방재",
    "커뮤니티", "주민", "도서", "휘트니스", "피트니스", "독서", "저수", "물탱크", "휀룸", "팬룸", "쓰레기", "근린",
    "상가", "부대", "복리", "피난", "정화조", "오수", "변전", "공조",
)  # fmt: skip
# 용도가 이렇게 뭉뚱그려 적힌 공용 행은 주용도가 주거일 때만 주거공용으로 본다
_GENERIC = ("", "아파트", "공동주택", "공동주택(아파트)", "도시형생활주택")
_HOUSING = ("아파트", "공동주택")
# 전용 대비 공급 비율이 이 범위 밖이면 공용 행을 잘못 가른 것으로 보고 버린다
SUPPLY_RATIO = (1.1, 1.7)


@dataclass(frozen=True)
class Parcel:
    sigungu: str
    bjdong: str
    plat_gb: str
    bun: str
    ji: str

    @classmethod
    def from_pnu(cls, pnu: str) -> "Parcel":
        if len(pnu) != 19 or not pnu.isdigit() or pnu[10] not in "12":
            raise ValueError(f"PNU 형식이 아닙니다: {pnu}")
        return cls(pnu[:5], pnu[5:10], str(int(pnu[10]) - 1), pnu[11:15], pnu[15:19])

    def params(self) -> dict:
        return {"sigunguCd": self.sigungu, "bjdongCd": self.bjdong, "platGbCd": self.plat_gb,
                "bun": self.bun, "ji": self.ji}  # fmt: skip


def is_residential_common(purpose: str | None, main_purpose: str | None = None) -> bool:
    p = (purpose or "").replace(" ", "")
    if p in _GENERIC:
        return (main_purpose or "").strip() in _HOUSING
    return any(k in p for k in _RESIDENTIAL) and not any(k in p for k in _OTHER)


def _is_apartment_unit(row: dict) -> bool:
    return row.get("exposPubuseGbCdNm") == "전유" and "아파트" in f"{row.get('mainPurpsCdNm')}{row.get('etcPurps')}"


def supply_samples(pages: dict[int, list[dict]], last_page: int) -> dict[int, list[float]]:
    """받은 페이지들 → 전용면적 평형(반올림 ㎡)별 공급면적(㎡) 표본.

    한 호의 행은 이어서 온다. 받은 페이지의 첫·끝에 걸친 호는 이웃 페이지를 안 받았으면 행이 잘렸을 수 있어 뺀다.
    """
    units: dict[tuple, list[dict]] = defaultdict(list)
    cut: set[tuple] = set()
    for page, rows in pages.items():
        for i, row in enumerate(rows):
            key = (row.get("mgmBldrgstPk"), row.get("dongNm"), row.get("hoNm"))
            units[key].append(row)
            first_cut = i == 0 and page > 1 and page - 1 not in pages
            last_cut = i == len(rows) - 1 and page < last_page and page + 1 not in pages
            if first_cut or last_cut:
                cut.add(key)
    samples: dict[int, list[float]] = defaultdict(list)
    for key, rows in units.items():
        if key in cut:
            continue
        own = [r for r in rows if _is_apartment_unit(r)]
        if len(own) != 1:
            continue
        exclusive = float(own[0]["area"])
        common = sum(
            float(r["area"])
            for r in rows
            if r.get("exposPubuseGbCdNm") == "공용" and is_residential_common(r.get("etcPurps"), r.get("mainPurpsCdNm"))
        )
        supply = exclusive + common
        if exclusive > 0 and SUPPLY_RATIO[0] <= supply / exclusive <= SUPPLY_RATIO[1]:
            samples[int(exclusive + 0.5)].append(supply)
    return dict(samples)


def supply_pyeong(samples: list[float]) -> float | None:
    """표본 중 가장 흔한 공급 평형(소수 첫째 자리). 같은 평형 안에 A·B 타입이 섞여도 많은 쪽을 쓴다."""
    if not samples:
        return None
    counts = Counter(round(s / PYEONG_M2, 1) for s in samples)
    return max(counts.items(), key=lambda kv: (kv[1], -kv[0]))[0]


class BuildingRegisterClient:
    """국토교통부 건축HUB 건축물대장정보 서비스(공공데이터포털)."""

    def __init__(
        self,
        key: str,
        *,
        base_url: str = "https://apis.data.go.kr/1613000/BldRgstHubService",
        http: httpx.AsyncClient | None = None,
        retries: int = 2,
        backoff_seconds: float = 1.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._key = key
        self._base = base_url.rstrip("/")
        self._http = http or httpx.AsyncClient(timeout=30)
        self._retries, self._backoff, self._sleep = retries, backoff_seconds, sleep
        self.calls_made = 0

    async def unit_areas(self, pnu: str, page: int) -> tuple[list[dict], int]:
        """전유공용면적 한 페이지와 전체 페이지 수."""
        rows, total = await self._items("getBrExposPubuseAreaInfo", Parcel.from_pnu(pnu), page=page, rows=ROWS_PER_PAGE)
        return rows, max(1, -(-total // ROWS_PER_PAGE))

    async def _items(self, operation: str, parcel: Parcel, *, page: int, rows: int) -> tuple[list[dict], int]:
        params = {"serviceKey": self._key, **parcel.params(), "numOfRows": rows, "pageNo": page, "_type": "json"}
        data = await self._get(f"{self._base}/{operation}", params)
        response = data.get("response") or {}
        header = response.get("header") or {}
        if header.get("resultCode") != "00":
            raise SourceUnavailable(f"건축물대장 오류 {header.get('resultCode')}: {header.get('resultMsg')}")
        body = response.get("body") or {}
        items = body.get("items") if isinstance(body.get("items"), dict) else {}
        found = items.get("item") or []
        if isinstance(found, dict):  # 1건이면 배열이 아니라 객체로 온다
            found = [found]
        return found, int(body.get("totalCount") or 0)

    async def _get(self, url: str, params: dict) -> dict:
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
                    raise SourceAuthError(f"건축물대장 인증 거부(HTTP {code})")
                if code == 429:
                    raise QuotaExceeded("건축물대장 호출 한도 초과(HTTP 429)")
                if code >= 500:
                    last = SourceUnavailable(f"건축물대장 서버 오류(HTTP {code})")
                elif code >= 400:
                    raise SourceUnavailable(f"건축물대장 요청 오류(HTTP {code})")
                else:
                    try:
                        return response.json()
                    except ValueError:
                        text = response.text
                        if "SERVICE_KEY" in text or "SERVICE KEY" in text:
                            raise SourceAuthError("건축물대장 인증키 오류") from None
                        if "LIMITED" in text:
                            raise QuotaExceeded("건축물대장 호출 한도 초과") from None
                        last = SourceUnavailable("건축물대장 응답이 JSON이 아닙니다")
            if attempt < self._retries:
                await self._sleep(self._backoff * 2**attempt)
        raise SourceUnavailable(f"건축물대장 호출 실패: {last}") from last

    async def aclose(self) -> None:
        await self._http.aclose()

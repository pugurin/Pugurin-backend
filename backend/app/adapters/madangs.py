"""경매마당 API(https://api.madangs.com): 법원 경매·공매 물건. 원천은 대법원 법원경매정보·온비드.

응답에는 당사자·임차인·응찰자 이름, 점유조사 메모, 등기 권리자 같은 개인정보가 섞여 온다.
이 모듈은 **허용한 필드만 골라 `AuctionItem`으로 옮기고 나머지는 버린다.** 원문 응답을 밖으로 넘기지 않는다.
"""

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import Literal

import httpx

from app.adapters.molit.types import QuotaExceeded, SourceAuthError
from app.core.dates import KST
from app.repositories.ports import SourceUnavailable

Source = Literal["court", "public"]
PAGE_SIZE = 100  # API 최대
MAX_PAGES = 200


@dataclass(frozen=True)
class AuctionItem:
    id: str
    source: Source
    case_no: str
    address: str
    court_name: str | None = None
    title: str | None = None
    usage: str | None = None
    jibun_address: str | None = None
    appraisal_price: int | None = None
    min_price: int | None = None
    deposit: int | None = None
    fail_count: int | None = None
    sale_date: date | None = None
    bid_start_at: datetime | None = None
    bid_end_at: datetime | None = None
    land_area_m2: float | None = None
    building_area_m2: float | None = None
    is_share: bool = False
    status: str | None = None
    schedule: tuple[dict, ...] = field(default=())
    results: tuple[dict, ...] = field(default=())

    @property
    def list_hash(self) -> str:
        """목록에서 오는 값의 지문. 바뀌면(유찰·가격·기일 변경) 상세를 다시 받는다."""
        keys = (self.case_no, self.address, self.usage, self.appraisal_price, self.min_price, self.fail_count)
        raw = json.dumps([*keys, self.sale_date and self.sale_date.isoformat(), self.status], ensure_ascii=False)
        return hashlib.sha1(raw.encode()).hexdigest()


def _int(v) -> int | None:
    try:
        return int(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def _float(v) -> float | None:
    try:
        return float(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def _date(v) -> date | None:
    try:
        return date.fromisoformat(str(v)[:10]) if v else None
    except ValueError:
        return None


def _datetime(v) -> datetime | None:
    try:
        return datetime.strptime(str(v), "%Y-%m-%d %H:%M").replace(tzinfo=KST) if v else None
    except ValueError:
        return None


def _text(v) -> str | None:
    s = str(v).strip() if v is not None else ""
    return s or None


def court_from_list(row: dict) -> AuctionItem:
    return AuctionItem(
        id=row["product_id"],
        source="court",
        case_no=_text(row.get("사건명칭")) or row["product_id"],
        court_name=_text(row.get("법원명")),
        address=_text(row.get("대표소재지명")) or "",
        usage=_text(row.get("물건용도")),
        appraisal_price=_int(row.get("감정가")),
        min_price=_int(row.get("최저가")),
        fail_count=_int(row.get("유찰횟수")),
        sale_date=_date(row.get("매각기일")),
        status="진행",
    )


def court_with_detail(item: AuctionItem, detail: dict) -> AuctionItem:
    """상세에서 숫자·날짜·주소만 더한다. 기일·매각결과도 이름 없는 칸만 옮긴다."""
    schedule = tuple(
        {
            "date": d.get("기일날짜"),
            "kind": _text(d.get("기일종류")),
            "min_price": _int(d.get("최저매각가격")),
            "result": _text(d.get("기일결과")),
            "sale_price": _int(d.get("낙찰가")),
        }
        for d in detail.get("기일내역") or []
    )
    results = tuple(
        {
            "date": r.get("매각일자"),
            "result": _text(r.get("매각결과")),
            "bidders": _int(r.get("응찰자수")),
            "top_price": _int(r.get("최고가금액")),
        }
        for r in detail.get("매각결과") or []
    )
    return replace(
        item,
        jibun_address=_text(detail.get("지번주소")),
        deposit=_int(detail.get("보증금")),
        land_area_m2=_float(detail.get("토지면적")),
        building_area_m2=_float(detail.get("건물면적")),
        schedule=schedule,
        results=results,
    )


def public_from_list(row: dict) -> AuctionItem:
    return AuctionItem(
        id=row["product_id"],
        source="public",
        case_no=_text(row.get("공매관리번호")) or row["product_id"],
        title=_text(row.get("물건명")),
        address=_text(row.get("도로명주소")) or _text(row.get("지번주소")) or "",
        jibun_address=_text(row.get("지번주소")),
        usage=_text(row.get("물건용도")),
        appraisal_price=_int(row.get("감정가")),
        min_price=_int(row.get("최저입찰가")),
        fail_count=_int(row.get("유찰횟수")),
        bid_start_at=_datetime(row.get("입찰시작일시")),
        bid_end_at=_datetime(row.get("입찰종료일시")),
        sale_date=_date(row.get("입찰종료일시")),
        land_area_m2=_float(row.get("토지면적")),
        building_area_m2=_float(row.get("건물면적")),
        is_share=bool(row.get("지분매각여부")),
        status=_text(row.get("물건상태")),
    )


class MadangsClient:
    """경매마당 데이터 API. `ServiceKey` 쿼리로 인증한다. 성공 응답 1건이 호출 1회로 차감된다."""

    def __init__(
        self,
        key: str,
        *,
        base_url: str = "https://api.madangs.com",
        http: httpx.AsyncClient | None = None,
        retries: int = 2,
        backoff_seconds: float = 1.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._key = key
        self._base_url = base_url.rstrip("/")
        self._http = http or httpx.AsyncClient(timeout=30)
        self._retries = retries
        self._backoff = backoff_seconds
        self._sleep = sleep
        self.calls_made = 0

    async def court_list(self, region_prefix: str) -> list[AuctionItem]:
        """진행 중인 법원 경매 물건 전부(`addr`=법정동 코드 앞자리, 부산은 26)."""
        return [court_from_list(r) for r in await self._all_pages("/ca/getcamulList", region_prefix)]

    async def court_detail(self, item: AuctionItem) -> AuctionItem:
        body = await self._get("/ca/getcamulInf", {"product_id": item.id, "dataType": "general"})
        detail = body.get("item")
        if not detail:
            raise SourceUnavailable(f"상세가 비어 있습니다: {item.id}")
        return court_with_detail(item, detail)

    async def public_list(self, region_prefix: str) -> list[AuctionItem]:
        """진행 중인 공매 물건 중 매각만(임대 물건은 뺀다)."""
        rows = await self._all_pages("/go/getgomulList", region_prefix)
        return [public_from_list(r) for r in rows if (r.get("처분방식") or "매각") == "매각"]

    async def _all_pages(self, path: str, region_prefix: str) -> list[dict]:
        rows: list[dict] = []
        for page_no in range(1, MAX_PAGES + 1):
            body = await self._get(path, {"addr": region_prefix, "pageNo": page_no, "numOfRows": PAGE_SIZE})
            rows.extend(body.get("items") or [])
            if not body.get("hasNext"):
                return rows
        raise SourceUnavailable(f"페이지가 {MAX_PAGES}개를 넘었습니다: {path}")

    async def _get(self, path: str, params: dict) -> dict:
        last: Exception | None = None
        for attempt in range(self._retries + 1):
            try:
                response = await self._http.get(f"{self._base_url}{path}", params={"ServiceKey": self._key, **params})
            except httpx.TransportError as e:
                last = e
            else:
                code = response.status_code
                if code in (401, 403):
                    raise SourceAuthError(f"경매마당 인증 거부(HTTP {code})")
                if code == 429:
                    raise QuotaExceeded("경매마당 호출 한도 초과(HTTP 429)")
                if code >= 500:
                    last = SourceUnavailable(f"경매마당 서버 오류(HTTP {code})")
                elif code >= 400:
                    raise SourceUnavailable(f"경매마당 요청 오류(HTTP {code}): {path}")
                else:
                    data = response.json()
                    header = data.get("header") or {}
                    if header.get("resultCode") != "00":
                        raise SourceUnavailable(f"경매마당 오류 {header.get('resultCode')}: {header.get('resultMsg')}")
                    self.calls_made += 1
                    return data.get("body") or {}
            if attempt < self._retries:
                await self._sleep(self._backoff * 2**attempt)
        raise SourceUnavailable(f"경매마당 호출 실패: {last}") from last

    async def aclose(self) -> None:
        await self._http.aclose()

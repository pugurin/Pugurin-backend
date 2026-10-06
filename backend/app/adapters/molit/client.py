import asyncio
import logging
from collections.abc import Awaitable, Callable

import httpx

from app.adapters.molit.parser import assign_ordinals, parse_page
from app.adapters.molit.types import QuotaExceeded, RawTrade, SourceAuthError, SourceKind
from app.repositories.ports import SourceUnavailable

logger = logging.getLogger(__name__)

MAX_PAGES = 50


class MolitClient:
    """공공데이터포털(국토교통부) 실거래가 API 클라이언트. 호출 수를 세고 한도를 넘으면 QuotaExceeded를 낸다."""

    def __init__(
        self,
        trade_key: str,
        rent_key: str,
        *,
        base_url: str = "https://apis.data.go.kr/1613000",
        http: httpx.AsyncClient | None = None,
        page_size: int = 1000,
        daily_limit: int | None = None,
        retries: int = 2,
        backoff_seconds: float = 0.5,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._keys = {"trade": trade_key, "rent": rent_key}
        self._base_url = base_url.rstrip("/")
        self._http = http or httpx.AsyncClient(timeout=30)
        self._page_size = page_size
        self._daily_limit = daily_limit
        self._retries = retries
        self._backoff = backoff_seconds
        self._sleep = sleep
        self._calls = 0

    @property
    def calls_made(self) -> int:
        return self._calls

    def reset_calls(self) -> None:
        self._calls = 0

    async def fetch_month(self, kind: SourceKind, lawd_cd: str, deal_ymd: str) -> list[RawTrade]:
        if not (len(lawd_cd) == 5 and lawd_cd.isdigit()):
            raise ValueError(f"시군구 코드는 숫자 5자리여야 합니다: {lawd_cd}")
        if not (len(deal_ymd) == 6 and deal_ymd.isdigit()):
            raise ValueError(f"계약월은 YYYYMM 형식이어야 합니다: {deal_ymd}")
        key = self._keys["rent" if kind.is_rent else "trade"]
        if not key:
            raise SourceAuthError(f"{'전월세' if kind.is_rent else '매매'} API 키가 설정되지 않았습니다")

        rows: list[RawTrade] = []
        for page_no in range(1, MAX_PAGES + 1):
            body = await self._get(kind, key, lawd_cd, deal_ymd, page_no)
            page = parse_page(body, kind)
            rows.extend(page.rows)
            if not page.rows or len(rows) >= page.total:
                break
        else:
            raise SourceUnavailable(f"페이지가 {MAX_PAGES}개를 넘었습니다: {kind} {lawd_cd} {deal_ymd}")
        return assign_ordinals(rows)

    async def _get(self, kind: SourceKind, key: str, lawd_cd: str, deal_ymd: str, page_no: int) -> bytes:
        url = f"{self._base_url}/{kind.endpoint}"
        params = {
            "serviceKey": key,
            "LAWD_CD": lawd_cd,
            "DEAL_YMD": deal_ymd,
            "pageNo": page_no,
            "numOfRows": self._page_size,
        }
        last_error: Exception | None = None
        for attempt in range(self._retries + 1):
            self._spend()
            try:
                response = await self._http.get(url, params=params)
            except httpx.TransportError as e:
                last_error = e
            else:
                if response.status_code == 429:
                    raise QuotaExceeded("호출 한도 초과(HTTP 429)")
                if response.status_code >= 500:
                    last_error = SourceUnavailable(f"원천 API 서버 오류(HTTP {response.status_code})")
                else:
                    return self._body(response)
            if attempt < self._retries:
                await self._sleep(self._backoff * 2**attempt)
        raise SourceUnavailable(f"원천 API 호출 실패: {last_error}") from last_error

    @staticmethod
    def _body(response: httpx.Response) -> bytes:
        if response.status_code in (401, 403) and not response.content.lstrip().startswith(b"<"):
            raise SourceAuthError(f"인증 거부(HTTP {response.status_code})")
        return response.content

    def _spend(self) -> None:
        if self._daily_limit is not None and self._calls >= self._daily_limit:
            raise QuotaExceeded(f"일일 호출 한도({self._daily_limit}회)에 도달했습니다")
        self._calls += 1

    async def aclose(self) -> None:
        await self._http.aclose()

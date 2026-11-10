"""JSON 오픈API를 부르는 공통 부분. 공공데이터포털처럼 키 오류를 XML로 돌려주는 곳도 견딘다."""

import httpx

from app.adapters.molit.types import QuotaExceeded, SourceAuthError
from app.repositories.ports import SourceUnavailable


class JsonApiClient:
    """JSON 오픈API 공통 호출: 재시도(전송 오류·5xx), 인증·한도 오류 구분, 호출 수 세기."""

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

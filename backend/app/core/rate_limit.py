import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.errors import ErrorCode, error_body

WINDOW_SECONDS = 60
MESSAGE = "요청이 너무 많습니다. 잠시 후 다시 시도해 주세요"


@dataclass(frozen=True)
class RateDecision:
    allowed: bool
    retry_after: int = 0


class RateLimiter(Protocol):
    async def hit(self, key: str, limit: int, window_seconds: int) -> RateDecision:
        """key의 이번 구간 요청 수를 1 늘리고, 한도를 넘었으면 다음 구간까지 남은 초를 알려준다."""
        ...


class MemoryRateLimiter:
    """고정 구간(분 단위) 카운터. 프로세스 하나에서만 맞다 — 여러 인스턴스로 띄우면 Redis 구현(#38)으로 바꾼다."""

    def __init__(self, now: Callable[[], float] = time.time):
        self._now = now
        self._counts: dict[str, tuple[int, int]] = {}  # key → (구간 번호, 횟수)
        self._window: int | None = None

    async def hit(self, key: str, limit: int, window_seconds: int) -> RateDecision:
        now = self._now()
        window = int(now // window_seconds)
        if window != self._window:
            # 구간이 바뀌면 지난 구간 기록은 더 볼 일이 없다
            self._counts = {k: v for k, v in self._counts.items() if v[0] == window}
            self._window = window
        _, count = self._counts.get(key, (window, 0))
        count += 1
        self._counts[key] = (window, count)
        if count <= limit:
            return RateDecision(True)
        return RateDecision(False, max(1, math.ceil((window + 1) * window_seconds - now)))

    def tracked_keys(self) -> int:
        return len(self._counts)


class RateLimitMiddleware:
    """`/api/v1` 요청을 기기(X-Device-Id)·IP 기준으로 센다.

    로그인 사용자 기준(분당 600)은 인증(#16)이 생기면 추가한다.
    """

    def __init__(
        self,
        app: ASGIApp,
        limiter: RateLimiter,
        device_per_minute: int,
        ip_per_minute: int,
        exempt_paths: frozenset[str],
        prefix: str = "/api/v1",
    ):
        self._app = app
        self._limiter = limiter
        self._device_limit = device_per_minute
        self._ip_limit = ip_per_minute
        self._exempt = exempt_paths
        self._prefix = prefix

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not path.startswith(self._prefix) or path in self._exempt:
            await self._app(scope, receive, send)
            return

        client = scope.get("client")
        checks = [(f"ip:{client[0] if client else 'unknown'}", self._ip_limit)]
        if (device := _device_id(scope)) is not None:
            checks.append((f"device:{device}", self._device_limit))

        for key, limit in checks:
            decision = await self._limiter.hit(key, limit, WINDOW_SECONDS)
            if not decision.allowed:
                response = JSONResponse(
                    error_body(ErrorCode.RATE_LIMITED, MESSAGE),
                    status_code=429,
                    headers={"Retry-After": str(decision.retry_after), "Cache-Control": "no-store"},
                )
                await response(scope, receive, send)
                return
        await self._app(scope, receive, send)


def _device_id(scope: Scope) -> UUID | None:
    """형식이 맞는 기기 ID만 센다. 없거나 틀리면 기기 한도는 건너뛰고, 검증 오류(400)는 라우터가 낸다."""
    for name, value in scope.get("headers", ()):
        if name == b"x-device-id":
            try:
                return UUID(value.decode("latin-1"))
            except ValueError:
                return None
    return None

import hashlib

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

NO_CACHE_PATHS = {"/api/v1/health"}


class ConditionalGetMiddleware(BaseHTTPMiddleware):
    """공개 조회 GET 200 응답에 ETag/Cache-Control을 붙이고, If-None-Match가 같으면 304를 돌려준다."""

    def __init__(self, app, max_age: int):
        super().__init__(app)
        self._max_age = max_age

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        path = request.url.path
        if request.method != "GET" or response.status_code != 200 or not path.startswith("/api/v1"):
            return response
        if path in NO_CACHE_PATHS:
            return response

        body = b"".join([chunk async for chunk in response.body_iterator])  # type: ignore[attr-defined]
        etag = f'"{hashlib.sha1(body).hexdigest()}"'
        cache_control = response.headers.get("cache-control", f"public, max-age={self._max_age}")
        headers = {"ETag": etag, "Cache-Control": cache_control}

        candidates = [c.strip().removeprefix("W/") for c in request.headers.get("if-none-match", "").split(",")]
        if etag in candidates and "no-store" not in cache_control:
            return Response(status_code=304, headers=headers)

        passthrough = {
            k: v for k, v in response.headers.items() if k.lower() not in {"content-length", "cache-control"}
        }
        return Response(content=body, status_code=200, headers={**passthrough, **headers})

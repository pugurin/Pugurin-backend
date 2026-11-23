"""uvicorn 접근 로그에서 개인 위치가 담긴 쿼리를 지운다.

대중교통 길찾기(`/transit/routes`)의 쿼리는 집·직장 좌표라 접근 로그에 남기지 않는다. 경로만 남긴다.
"""

import logging

# 이 경로로 시작하면 쿼리(?...)를 지운다
PRIVATE_QUERY_PATHS = ("/api/v1/transit/",)


class RedactPrivateQuery(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn 접근 로그 args: (client_addr, method, full_path, http_version, status_code)
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            path = args[2]
            if "?" in path and path.startswith(PRIVATE_QUERY_PATHS):
                record.args = (*args[:2], path.split("?", 1)[0], *args[3:])
        return True


def install() -> None:
    """여러 번 불러도 필터는 하나만 붙는다(테스트가 앱을 여러 번 만든다)."""
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, RedactPrivateQuery) for f in access.filters):
        access.addFilter(RedactPrivateQuery())

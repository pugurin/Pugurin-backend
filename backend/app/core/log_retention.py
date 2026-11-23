"""접근 로그(uvicorn.access) 보관 기간을 지킨다. 개인정보처리방침: 접속 기록(IP·요청 경로)은 최대 14일.

`PUGURIN_ACCESS_LOG_DIR`가 있으면(운영) 접근 로그를 그 폴더의 `access.log`에 쓰고, 자정마다 새 파일로 나눠
최근 14일치만 남긴다. 도커 로그(stdout)에는 접속 기록을 남기지 않는다. 없으면(개발·테스트) uvicorn 기본 그대로다.
"""

import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

ACCESS_LOG_DAYS = 14


def install(log_dir: Path | None) -> None:
    """여러 번 불러도 파일 핸들러는 하나만 둔다(테스트가 앱을 여러 번 만든다)."""
    if log_dir is None:
        return
    access = logging.getLogger("uvicorn.access")
    target = str((Path(log_dir) / "access.log").resolve())
    if any(isinstance(h, TimedRotatingFileHandler) and h.baseFilename == target for h in access.handlers):
        return
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    # 오늘 파일 + 지난 13일 파일 = 14일. 시각은 서버 시간대가 아니라 UTC 기준으로 나눈다
    handler = TimedRotatingFileHandler(
        target, when="midnight", backupCount=ACCESS_LOG_DAYS - 1, utc=True, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    for old in access.handlers:
        old.close()
    access.handlers = [handler]
    access.propagate = False
    access.setLevel(logging.INFO)

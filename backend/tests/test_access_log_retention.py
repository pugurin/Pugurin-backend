"""접근 로그 보관(개인정보처리방침: 접속 기록은 최대 14일). 운영은 날짜별 파일로 나눠 14일치만 남긴다."""

import logging
from logging.handlers import TimedRotatingFileHandler

import pytest

from app.core import log_retention
from app.core.config import Settings
from app.main import create_app

ACCESS = logging.getLogger("uvicorn.access")


@pytest.fixture(autouse=True)
def restore_access_logger():
    handlers, propagate, level = list(ACCESS.handlers), ACCESS.propagate, ACCESS.level
    yield
    for h in ACCESS.handlers:
        if h not in handlers:
            h.close()
    ACCESS.handlers, ACCESS.propagate = handlers, propagate
    ACCESS.setLevel(level)


def test_with_a_log_dir_access_logs_go_to_daily_files_kept_for_14_days(tmp_path):
    create_app(settings=Settings(access_log_dir=tmp_path))
    [handler] = ACCESS.handlers
    assert isinstance(handler, TimedRotatingFileHandler)
    assert (handler.when, handler.backupCount) == ("MIDNIGHT", log_retention.ACCESS_LOG_DAYS - 1)
    assert log_retention.ACCESS_LOG_DAYS == 14
    assert ACCESS.propagate is False  # 도커 로그(stdout)에는 접속 기록을 남기지 않는다
    ACCESS.info('%s - "%s %s HTTP/%s" %d', "1.2.3.4:5", "GET", "/api/v1/health", "1.1", 200)
    handler.flush()
    assert "1.2.3.4" in (tmp_path / "access.log").read_text()


def test_installing_twice_keeps_one_file_handler(tmp_path):
    create_app(settings=Settings(access_log_dir=tmp_path))
    create_app(settings=Settings(access_log_dir=tmp_path))
    assert len(ACCESS.handlers) == 1


def test_without_a_log_dir_uvicorn_logging_is_untouched():
    before = list(ACCESS.handlers)
    create_app(settings=Settings())
    assert ACCESS.handlers == before

from datetime import datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.dates import KST
from app.main import create_app

# 개발자 로컬 .env(실제 API 키)가 테스트에 섞이지 않게 한다
Settings.model_config["env_file"] = None

FIXED_NOW = datetime(2026, 10, 6, 12, 0, tzinfo=KST)
DATA_AS_OF = "2026-10-06T04:00:00+09:00"
BUSAN = "128.75,34.85,129.35,35.40"
HAEUNDAE = "129.10,35.14,129.22,35.22"
API = "/api/v1"


def make_client(**kwargs) -> TestClient:
    app = create_app(clock=lambda: FIXED_NOW, **kwargs)
    return TestClient(app, headers={"X-Device-Id": str(uuid4())})


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="session")
def client():
    with make_client() as c:
        yield c

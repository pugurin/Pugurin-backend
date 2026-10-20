import os
from datetime import datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.dates import KST
from app.main import create_app

# 개발자 로컬 .env(실제 API 키)가 테스트에 섞이지 않게 한다
Settings.model_config["env_file"] = None
# 테스트는 한 기기·한 IP로 수백 번 호출하므로 요청 한도를 끈다. 한도 자체는 test_rate_limit.py가 켜고 검증한다
os.environ["PUGURIN_RATE_LIMIT_ENABLED"] = "false"

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


# ── 실제 PostGIS·Redis (testcontainers). Docker가 없으면 이 픽스처를 쓰는 테스트는 건너뛴다 ──

POSTGIS_IMAGE = "imresamu/postgis:16-3.5"  # postgis/postgis는 amd64 전용이라 arm64(Apple Silicon)에서 느리다


def _docker_available() -> bool:
    try:
        import docker

        docker.from_env().ping()
    except Exception:  # noqa: BLE001
        return False
    return True


@pytest.fixture(scope="session")
def postgis():
    if not _docker_available():
        pytest.skip("Docker가 없어 DB 테스트를 건너뜁니다")
    from testcontainers.community.postgres import PostgresContainer

    with PostgresContainer(POSTGIS_IMAGE, driver="asyncpg") as pg:
        yield pg.get_connection_url()


@pytest.fixture(scope="session")
def redis_url():
    if not _docker_available():
        pytest.skip("Docker가 없어 Redis 테스트를 건너뜁니다")
    from testcontainers.community.redis import RedisContainer

    with RedisContainer("redis:7-alpine") as r:
        yield f"redis://{r.get_container_host_ip()}:{r.get_exposed_port(6379)}/0"


def create_database(server_url: str, name: str) -> str:
    """같은 컨테이너에 빈 DB를 하나 더 만들어 그 URL을 돌려준다. 테스트 파일끼리 데이터가 섞이지 않게 한다."""
    import asyncio

    from sqlalchemy import text

    from app.db.engine import make_engine

    async def run():
        engine = make_engine(server_url).execution_options(isolation_level="AUTOCOMMIT")
        try:
            async with engine.connect() as conn:
                await conn.execute(text(f'CREATE DATABASE "{name}"'))
        finally:
            await engine.dispose()

    asyncio.run(run())
    return server_url.rsplit("/", 1)[0] + f"/{name}"

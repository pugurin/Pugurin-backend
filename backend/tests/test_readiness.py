"""Docker 없이 확인할 수 있는 readiness 동작. 실제 DB·Redis로 확인하는 것은 test_db.py."""

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from tests.conftest import API, FIXED_NOW

READY = "/internal/health/ready"
INTERNAL = ("127.0.0.1", 50000)


def client(addr=INTERNAL, **settings) -> TestClient:
    return TestClient(create_app(settings=Settings(**settings), clock=lambda: FIXED_NOW), client=addr)


def test_without_database_or_redis_the_checks_are_skipped_and_ready():
    with client() as c:
        r = c.get(READY)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    assert body["data_mode"] == "sample"
    assert {name: check["status"] for name, check in body["checks"].items()} == {
        "database": "skipped",
        "redis": "skipped",
    }


def test_unreachable_database_is_not_ready():
    with client(database_url="postgresql+asyncpg://nobody:x@127.0.0.1:1/none") as c:
        r = c.get(READY)
    assert r.status_code == 503
    assert r.json()["status"] == "not_ready"
    assert r.json()["checks"]["database"]["status"] == "error"
    assert r.json()["checks"]["database"]["detail"].startswith("연결 실패")


def test_unreachable_redis_is_not_ready():
    with client(redis_url="redis://127.0.0.1:1/0") as c:
        r = c.get(READY)
    assert r.status_code == 503
    assert r.json()["checks"]["redis"]["status"] == "error"


def test_private_network_callers_are_allowed():
    for addr in (("10.0.0.5", 1), ("172.18.0.3", 1), ("192.168.0.10", 1), ("::1", 1)):
        with client(addr) as c:
            assert c.get(READY).status_code == 200, addr


def test_public_callers_get_a_plain_404():
    with client(("8.8.8.8", 1)) as c:
        r = c.get(READY)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "RESOURCE_NOT_FOUND"


def test_readiness_is_hidden_from_the_public_api_docs():
    with client() as c:
        paths = c.get("/openapi.json").json()["paths"]
    assert READY not in paths


def test_liveness_stays_public_and_simple():
    with client(("8.8.8.8", 1)) as c:
        assert c.get(f"{API}/health").json() == {"status": "ok"}

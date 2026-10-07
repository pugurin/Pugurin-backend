from datetime import date, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.cache import TTLCache
from app.core.dates import KST, latest_etl_time, months_ago
from app.core.errors import AppError
from app.core.geo import BBox, m2_to_pyeong
from tests.conftest import API, FIXED_NOW


def test_health_needs_no_device_id():
    from app.main import create_app

    with TestClient(create_app(clock=lambda: FIXED_NOW)) as c:
        r = c.get(f"{API}/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_missing_device_id_is_400():
    from app.main import create_app

    with TestClient(create_app(clock=lambda: FIXED_NOW)) as c:
        r = c.get(f"{API}/glossary")
    assert r.status_code == 400
    assert r.json() == {
        "error": {
            "code": "VALIDATION_ERROR",
            "message": "요청 값이 올바르지 않습니다: X-Device-Id",
            "field": "X-Device-Id",
        }
    }


def test_invalid_device_id_is_400(client):
    r = client.get(f"{API}/glossary", headers={"X-Device-Id": "not-a-uuid"})
    assert r.status_code == 400
    assert r.json()["error"]["field"] == "X-Device-Id"


def test_openapi_schema_builds_for_all_endpoints(client):
    r = client.get("/openapi.json")
    assert r.status_code == 200
    assert set(r.json()["paths"]) == {
        f"{API}/health",
        f"{API}/map/markers",
        f"{API}/search",
        f"{API}/transactions",
        f"{API}/transactions/{{transaction_id}}",
        f"{API}/stats/regions/{{region_code}}",
        f"{API}/complexes/{{complex_id}}/stats",
        f"{API}/complexes/{{complex_id}}",
        f"{API}/complexes/{{complex_id}}/transactions",
        f"{API}/parcels/lookup",
        f"{API}/parcels/{{pnu}}",
        f"{API}/glossary",
        f"{API}/glossary/{{term_id}}",
    }


def test_unknown_route_uses_error_envelope(client):
    r = client.get(f"{API}/nope")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "RESOURCE_NOT_FOUND"


def test_unhandled_error_is_500_envelope_without_details():
    class Boom:
        data_as_of = FIXED_NOW

        async def get_complex(self, _):
            raise RuntimeError("secret internals")

    from app.main import create_app

    app = create_app(clock=lambda: FIXED_NOW, market_repo=Boom())
    with TestClient(app, headers={"X-Device-Id": str(uuid4())}, raise_server_exceptions=False) as c:
        r = c.get(f"{API}/complexes/{uuid4()}")
    assert r.status_code == 500
    assert r.json() == {"error": {"code": "INTERNAL_ERROR", "message": "서버 오류가 발생했습니다", "field": None}}


def test_conditional_get_returns_304_for_same_etag(client):
    first = client.get(f"{API}/glossary")
    assert first.status_code == 200
    etag = first.headers["etag"]
    assert "max-age" in first.headers["cache-control"]

    second = client.get(f"{API}/glossary", headers={"If-None-Match": etag})
    assert second.status_code == 304
    assert second.content == b""
    assert second.headers["etag"] == etag

    other = client.get(f"{API}/glossary", params={"category": "tax"}, headers={"If-None-Match": etag})
    assert other.status_code == 200


def test_errors_are_never_cached(client):
    r = client.get(f"{API}/glossary/{uuid4()}")
    assert r.status_code == 404
    assert "etag" not in r.headers


def test_m2_to_pyeong_uses_3_3058():
    assert m2_to_pyeong(84.97) == 25.7
    assert m2_to_pyeong(3.3058) == 1.0


def test_bbox_parse_ok():
    assert BBox.parse("129.1,35.1,129.2,35.2") == BBox(129.1, 35.1, 129.2, 35.2)


@pytest.mark.parametrize("raw", ["1,2,3", "a,b,c,d", "129.2,35.1,129.1,35.2", "129,95,130,96", ""])
def test_bbox_parse_rejects_bad_input(raw):
    with pytest.raises(AppError) as exc:
        BBox.parse(raw)
    assert exc.value.field == "bbox"


def test_bbox_contains_is_inclusive():
    box = BBox(129.0, 35.0, 129.1, 35.1)
    assert box.contains(35.0, 129.0) and box.contains(35.1, 129.1)
    assert not box.contains(35.11, 129.05)


def test_months_ago_clamps_day():
    assert months_ago(date(2026, 3, 31), 1) == date(2026, 2, 28)
    assert months_ago(date(2026, 1, 15), 12) == date(2025, 1, 15)
    assert months_ago(date(2026, 10, 6), 60) == date(2021, 10, 6)


def test_latest_etl_time_is_0400_kst():
    assert latest_etl_time(datetime(2026, 10, 6, 12, 0, tzinfo=KST)).isoformat() == "2026-10-06T04:00:00+09:00"
    assert latest_etl_time(datetime(2026, 10, 6, 3, 59, tzinfo=KST)).isoformat() == "2026-10-05T04:00:00+09:00"


def test_ttl_cache_expires():
    from datetime import timedelta

    now = [datetime(2026, 10, 6, tzinfo=KST)]
    cache: TTLCache[str] = TTLCache(lambda: now[0])
    cache.set("k", "v", timedelta(days=1))
    assert cache.get("k") is not None
    now[0] += timedelta(days=1, seconds=1)
    assert cache.get("k") is None

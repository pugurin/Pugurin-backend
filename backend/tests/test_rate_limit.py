from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.rate_limit import MemoryRateLimiter
from tests.conftest import API, FIXED_NOW
from tests.fakes.app import create_app

GLOSSARY = f"{API}/glossary"


class FakeTime:
    def __init__(self, start: float = 1_000_020.0):  # 분 경계(1,000,020 = 16,667분 정각)에서 시작
        self.now = start

    def __call__(self) -> float:
        return self.now


def make(time: FakeTime, **limits) -> TestClient:
    settings = Settings(
        **{"rate_limit_enabled": True, "rate_limit_device_per_minute": 3, "rate_limit_ip_per_minute": 100, **limits}
    )
    app = create_app(settings=settings, clock=lambda: FIXED_NOW, rate_limiter=MemoryRateLimiter(now=time))
    return TestClient(app)


def device() -> dict[str, str]:
    return {"X-Device-Id": str(uuid4())}


# ── 리미터 단위 ──────────────────────────────────────────────


@pytest.mark.anyio
async def test_allows_up_to_the_limit_then_blocks_until_the_window_ends():
    t = FakeTime()
    limiter = MemoryRateLimiter(now=t)
    for _ in range(3):
        assert (await limiter.hit("k", limit=3, window_seconds=60)).allowed
    t.now += 15
    blocked = await limiter.hit("k", limit=3, window_seconds=60)
    assert not blocked.allowed and blocked.retry_after == 45


@pytest.mark.anyio
async def test_next_window_starts_from_zero():
    t = FakeTime()
    limiter = MemoryRateLimiter(now=t)
    for _ in range(4):
        await limiter.hit("k", limit=3, window_seconds=60)
    t.now += 60
    assert (await limiter.hit("k", limit=3, window_seconds=60)).allowed


@pytest.mark.anyio
async def test_keys_are_counted_separately():
    limiter = MemoryRateLimiter(now=FakeTime())
    for _ in range(3):
        await limiter.hit("a", limit=3, window_seconds=60)
    assert not (await limiter.hit("a", limit=3, window_seconds=60)).allowed
    assert (await limiter.hit("b", limit=3, window_seconds=60)).allowed


@pytest.mark.anyio
async def test_retry_after_is_at_least_one_second():
    t = FakeTime()
    limiter = MemoryRateLimiter(now=t)
    await limiter.hit("k", limit=1, window_seconds=60)
    t.now += 59.6
    assert (await limiter.hit("k", limit=1, window_seconds=60)).retry_after == 1


@pytest.mark.anyio
async def test_old_windows_are_forgotten():
    t = FakeTime()
    limiter = MemoryRateLimiter(now=t)
    for i in range(50):
        await limiter.hit(f"k{i}", limit=3, window_seconds=60)
    t.now += 120
    await limiter.hit("fresh", limit=3, window_seconds=60)
    assert limiter.tracked_keys() == 1


# ── API 동작 ─────────────────────────────────────────────────


@pytest.fixture
def time() -> FakeTime:
    return FakeTime()


def test_device_over_its_limit_gets_429_with_retry_after(time):
    with make(time) as c:
        headers = device()
        for _ in range(3):
            assert c.get(GLOSSARY, headers=headers).status_code == 200
        time.now += 20
        r = c.get(GLOSSARY, headers=headers)
    assert r.status_code == 429
    assert r.headers["Retry-After"] == "40"
    assert r.json() == {
        "error": {"code": "RATE_LIMITED", "message": "요청이 너무 많습니다. 잠시 후 다시 시도해 주세요", "field": None}
    }


def test_rate_limited_responses_are_not_cached(time):
    with make(time) as c:
        headers = device()
        for _ in range(3):
            c.get(GLOSSARY, headers=headers)
        r = c.get(GLOSSARY, headers=headers)
    assert r.status_code == 429
    assert "ETag" not in r.headers
    assert r.headers["Cache-Control"] == "no-store"


def test_other_devices_are_not_affected(time):
    with make(time) as c:
        noisy = device()
        for _ in range(4):
            c.get(GLOSSARY, headers=noisy)
        assert c.get(GLOSSARY, headers=device()).status_code == 200


def test_limit_resets_in_the_next_minute(time):
    with make(time) as c:
        headers = device()
        for _ in range(4):
            c.get(GLOSSARY, headers=headers)
        time.now += 60
        assert c.get(GLOSSARY, headers=headers).status_code == 200


def test_ip_cap_applies_across_devices(time):
    # 같은 IP(통신사 NAT 등)에서 여러 기기가 몰려도 분당 상한을 넘으면 막는다
    with make(time, rate_limit_ip_per_minute=5) as c:
        codes = [c.get(GLOSSARY, headers=device()).status_code for _ in range(6)]
    assert codes == [200] * 5 + [429]


def test_health_is_never_limited(time):
    with make(time, rate_limit_ip_per_minute=2) as c:
        codes = [c.get(f"{API}/health").status_code for _ in range(5)]
    assert codes == [200] * 5


def test_missing_device_id_is_still_a_validation_error(time):
    # 기기 ID가 없으면 기기 한도는 셀 수 없으므로 IP 상한만 적용하고, 검증 오류(400)는 그대로 낸다
    with make(time) as c:
        codes = [c.get(GLOSSARY).status_code for _ in range(5)]
    assert codes == [400] * 5


def test_disabled_limiter_never_blocks(time):
    with make(time, rate_limit_enabled=False) as c:
        headers = device()
        codes = {c.get(GLOSSARY, headers=headers).status_code for _ in range(10)}
    assert codes == {200}

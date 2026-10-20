from datetime import datetime

import pytest

from app.core.dates import KST
from app.ingestion.worker import next_run_at, run_daily

pytestmark = pytest.mark.anyio


def test_next_run_is_today_before_the_hour_and_tomorrow_after_it():
    assert next_run_at(datetime(2026, 10, 6, 3, 59, tzinfo=KST), 4) == datetime(2026, 10, 6, 4, 0, tzinfo=KST)
    assert next_run_at(datetime(2026, 10, 6, 4, 0, tzinfo=KST), 4) == datetime(2026, 10, 7, 4, 0, tzinfo=KST)
    assert next_run_at(datetime(2026, 10, 6, 23, 0, tzinfo=KST), 4) == datetime(2026, 10, 7, 4, 0, tzinfo=KST)


async def test_worker_runs_once_at_start_then_sleeps_until_the_next_run():
    now = datetime(2026, 10, 6, 12, 0, tzinfo=KST)
    runs: list[datetime] = []
    sleeps: list[float] = []

    async def job() -> int:
        runs.append(now)
        return 0

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    await run_daily(job, hour=4, clock=lambda: now, sleep=sleep, out=lambda _: None, max_runs=2)
    assert len(runs) == 2
    assert sleeps == [16 * 3600]  # 12:00 → 다음 날 04:00


async def test_a_failing_run_does_not_stop_the_worker():
    calls = 0

    async def job() -> int:
        nonlocal calls
        calls += 1
        raise RuntimeError("원천 장애")

    async def sleep(_: float) -> None:
        return None

    lines: list[str] = []
    await run_daily(
        job, hour=4, clock=lambda: datetime(2026, 10, 6, tzinfo=KST), sleep=sleep, out=lines.append, max_runs=3
    )
    assert calls == 3
    assert any("원천 장애" in line for line in lines)

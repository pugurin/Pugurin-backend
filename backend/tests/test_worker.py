from datetime import datetime, timedelta

import pytest

from app.core.dates import KST
from app.ingestion.worker import next_run_at, next_weekly_at, run_daily, run_weekly

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


def test_next_weekly_at_is_monday_eight_kst():
    mon = datetime(2026, 11, 23, tzinfo=KST)  # 월요일
    assert next_weekly_at(mon.replace(hour=7, minute=59)) == mon.replace(hour=8)
    assert next_weekly_at(mon.replace(hour=8)) == datetime(2026, 11, 30, 8, 0, tzinfo=KST)
    assert next_weekly_at(datetime(2026, 11, 25, 12, 0, tzinfo=KST)) == datetime(2026, 11, 30, 8, 0, tzinfo=KST)
    assert next_weekly_at(datetime(2026, 11, 22, 23, 0, tzinfo=KST)) == datetime(2026, 11, 23, 8, 0, tzinfo=KST)


async def test_weekly_loop_runs_at_start_then_waits_for_next_monday():
    now = [datetime(2026, 11, 25, 12, 0, tzinfo=KST)]  # 수요일 낮(재시작·배포 시나리오)
    sleeps: list[float] = []
    runs: list[datetime] = []

    async def job() -> str:
        runs.append(now[0])
        return "ok"

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] = now[0] + timedelta(seconds=seconds)

    await run_weekly(job, clock=lambda: now[0], sleep=sleep, out=lambda _: None, max_runs=2)
    assert runs == [datetime(2026, 11, 25, 12, 0, tzinfo=KST), datetime(2026, 11, 30, 8, 0, tzinfo=KST)]
    assert sleeps == [5 * 24 * 3600 - 4 * 3600]  # 수 12:00 -> 다음 월 08:00


async def test_a_failing_weekly_run_does_not_stop_the_weekly_loop():
    calls = 0

    async def job() -> str:
        nonlocal calls
        calls += 1
        raise ValueError("아직 끝나지 않은 주")

    async def sleep(_: float) -> None:
        return None

    lines: list[str] = []
    await run_weekly(
        job, clock=lambda: datetime(2026, 11, 23, 8, 0, tzinfo=KST), sleep=sleep, out=lines.append, max_runs=3
    )
    assert calls == 3
    assert any("아직 끝나지 않은 주" in line for line in lines)


async def test_a_failing_weekly_run_is_retried_after_an_hour_not_a_week():
    now = [datetime(2026, 11, 23, 8, 0, tzinfo=KST)]
    sleeps: list[float] = []
    calls = 0

    async def job() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("일시 장애")
        return "ok"

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] = now[0] + timedelta(seconds=seconds)

    await run_weekly(job, clock=lambda: now[0], sleep=sleep, out=lambda _: None, max_runs=2)
    assert calls == 2
    assert sleeps == [3600]

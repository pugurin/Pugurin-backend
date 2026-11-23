"""수집 worker: 시작하자마자 한 번 돌고, 이후 매일 정해진 시각(KST)에 다시 돈다."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, time, timedelta

from app.core.dates import KST, Clock, kst_now


def next_run_at(now: datetime, hour: int) -> datetime:
    now = now.astimezone(KST)
    today = datetime.combine(now.date(), time(hour, 0), tzinfo=KST)
    return today if now < today else today + timedelta(days=1)


async def run_daily(
    job: Callable[[], Awaitable[int]],
    *,
    hour: int,
    clock: Clock = kst_now,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    out: Callable[[str], None] = print,
    max_runs: int | None = None,
) -> None:
    """한 번의 실패로 worker가 죽지 않게 예외를 기록하고 다음 차례를 기다린다. `max_runs`는 테스트용."""
    runs = 0
    while True:
        try:
            code = await job()
            out(f"수집·적재 끝 (종료 코드 {code})")
        except Exception as e:  # noqa: BLE001
            out(f"수집·적재 실패: {type(e).__name__}: {e}")
        runs += 1
        if max_runs is not None and runs >= max_runs:
            return
        now = clock()
        wake = next_run_at(now, hour)
        out(f"다음 실행 {wake.isoformat()}")
        await sleep((wake - now).total_seconds())


def next_weekly_at(now: datetime, weekday: int = 0, hour: int = 8) -> datetime:
    """다음 `weekday`(월=0) `hour`시 KST. 정각이면 다음 주."""
    now = now.astimezone(KST)
    day = datetime.combine(now.date() + timedelta(days=(weekday - now.weekday()) % 7), time(hour, 0), tzinfo=KST)
    return day if now < day else day + timedelta(days=7)


async def run_weekly(
    job: Callable[[], Awaitable[object]],
    *,
    weekday: int = 0,
    hour: int = 8,
    clock: Clock = kst_now,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    out: Callable[[str], None] = print,
    max_runs: int | None = None,
) -> None:
    """매주 정해진 요일·시각(KST)에 job을 돈다. 시작하자마자 돌지는 않는다.
    실패해도 죽지 않고 기록한 뒤 다음 주를 기다린다(일일 수집 루프와 서로 영향 없음). `max_runs`는 테스트용."""
    runs = 0
    while True:
        now = clock()
        wake = next_weekly_at(now, weekday, hour)
        out(f"다음 주간 실행 {wake.isoformat()}")
        await sleep((wake - now).total_seconds())
        try:
            result = await job()
            out(f"주간 작업 끝: {result}")
        except Exception as e:  # noqa: BLE001
            out(f"주간 작업 실패: {type(e).__name__}: {e}")
        runs += 1
        if max_runs is not None and runs >= max_runs:
            return

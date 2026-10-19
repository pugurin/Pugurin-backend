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

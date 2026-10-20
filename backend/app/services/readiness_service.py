import asyncio
from dataclasses import dataclass
from typing import Literal

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db import migrate
from app.repositories.ports import MarketRepository

CHECK_TIMEOUT_SECONDS = 2.0
CheckStatus = Literal["ok", "warning", "error", "skipped"]  # warning은 준비 상태를 막지 않는다


@dataclass(frozen=True)
class Check:
    status: CheckStatus
    detail: str | None = None


@dataclass(frozen=True)
class Readiness:
    ready: bool
    checks: dict[str, Check]


class ReadinessService:
    """DB·Redis가 붙어 있으면 실제로 닿는지, DB 스키마가 최신인지 본다. 설정하지 않은 것은 건너뛴다."""

    def __init__(self, engine: AsyncEngine | None, redis: Redis | None, market: MarketRepository | None = None):
        self._engine = engine
        self._redis = redis
        self._market = market

    async def check(self) -> Readiness:
        database, redis = await asyncio.gather(self._database(), self._redis_check())
        checks = {"database": database, "redis": redis}
        if self._market is not None:
            checks["data"] = await self._data()
        return Readiness(all(c.status != "error" for c in checks.values()), checks)

    async def _data(self) -> Check:
        """실거래 데이터가 있는지. 없으면 API가 '데이터 없음'으로 답할 뿐 서버는 정상이라 준비 상태는 막지 않는다."""
        refresh = getattr(self._market, "refresh", None)
        try:
            if refresh is not None:
                async with asyncio.timeout(CHECK_TIMEOUT_SECONDS):
                    await refresh()
        except Exception as e:  # noqa: BLE001
            return Check("error", f"조회 실패: {type(e).__name__}")
        if not getattr(self._market, "has_data", True):
            return Check("warning", "실거래 데이터 없음 → `python -m app.ingestion load`(DB) 또는 `refresh`(수집)")
        return Check("ok", f"데이터 기준 {self._market.data_as_of.isoformat()}")

    async def _database(self) -> Check:
        if self._engine is None:
            return Check("skipped", "PUGURIN_DATABASE_URL 미설정")
        try:
            async with asyncio.timeout(CHECK_TIMEOUT_SECONDS), self._engine.connect() as conn:
                current = await conn.run_sync(migrate.current_revision)
        except Exception as e:  # noqa: BLE001 - 어떤 이유로든 닿지 않으면 준비 안 됨
            return Check("error", f"연결 실패: {type(e).__name__}")
        head = migrate.head_revision()
        if current != head:
            return Check("error", f"마이그레이션 필요: 현재 {current or '없음'} / 최신 {head}")
        return Check("ok", f"스키마 {current}")

    async def _redis_check(self) -> Check:
        if self._redis is None:
            return Check("skipped", "PUGURIN_REDIS_URL 미설정")
        try:
            async with asyncio.timeout(CHECK_TIMEOUT_SECONDS):
                await self._redis.ping()
        except Exception as e:  # noqa: BLE001
            return Check("error", f"연결 실패: {type(e).__name__}")
        return Check("ok")

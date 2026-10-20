from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI

from app.core.config import Settings
from app.core.dates import Clock, kst_now
from app.core.errors import install_error_handlers
from app.core.http_cache import NO_CACHE_PATHS, ConditionalGetMiddleware
from app.core.rate_limit import MemoryRateLimiter, RateLimiter, RateLimitMiddleware
from app.repositories.ports import (
    AddressSearch,
    GlossaryRepository,
    MarketRepository,
    ParcelSource,
    ZoningRuleRepository,
)
from app.routers import analysis, complexes, glossary, health, internal, map, parcels, search, stats, transactions
from app.routers.deps import require_device_id
from app.wiring import build_container

API_PREFIX = "/api/v1"


def create_app(
    settings: Settings | None = None,
    clock: Clock | None = None,
    market_repo: MarketRepository | None = None,
    glossary_repo: GlossaryRepository | None = None,
    parcel_source: ParcelSource | None = None,
    zoning_rules: ZoningRuleRepository | None = None,
    address_search: AddressSearch | None = None,
    rate_limiter: RateLimiter | None = None,
) -> FastAPI:
    settings = settings or Settings()
    container = build_container(
        settings, clock or kst_now, market_repo, glossary_repo, parcel_source, zoning_rules, address_search
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await container.startup()
        yield
        await container.aclose()

    app = FastAPI(title="Pugurin Backend", version="0.1.0", lifespan=lifespan)
    app.state.container = container

    install_error_handlers(app)
    app.add_middleware(ConditionalGetMiddleware, max_age=settings.cache_max_age_seconds)
    data_mode = app.state.container.data_mode

    @app.middleware("http")
    async def add_data_mode_header(request, call_next):
        response = await call_next(request)
        response.headers["X-Data-Mode"] = data_mode
        return response

    # 가장 바깥에서 먼저 세어, 막힌 요청은 라우터·캐시 계산까지 가지 않게 한다
    if settings.rate_limit_enabled:
        app.add_middleware(
            RateLimitMiddleware,
            limiter=rate_limiter or MemoryRateLimiter(),
            device_per_minute=settings.rate_limit_device_per_minute,
            ip_per_minute=settings.rate_limit_ip_per_minute,
            exempt_paths=frozenset(NO_CACHE_PATHS),
        )

    app.include_router(health.router, prefix=API_PREFIX)
    app.include_router(internal.router)
    public = APIRouter(prefix=API_PREFIX, dependencies=[Depends(require_device_id)])
    # 고정 경로(/parcels/lookup)는 경로 변수(/parcels/{pnu})보다 먼저 등록되도록 각 라우터 안에서 선언 순서를 지켰다
    for module in (map, search, stats, transactions, complexes, parcels, analysis, glossary):
        public.include_router(module.router)
    app.include_router(public)
    return app

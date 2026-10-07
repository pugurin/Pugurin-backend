from fastapi import APIRouter, Depends, FastAPI

from app.core.config import Settings
from app.core.dates import Clock, kst_now
from app.core.errors import install_error_handlers
from app.core.http_cache import ConditionalGetMiddleware
from app.repositories.ports import GlossaryRepository, MarketRepository, ParcelSource, ZoningRuleRepository
from app.routers import complexes, glossary, health, map, parcels
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
) -> FastAPI:
    settings = settings or Settings()
    app = FastAPI(title="Pugurin Backend", version="0.1.0")
    app.state.container = build_container(
        settings, clock or kst_now, market_repo, glossary_repo, parcel_source, zoning_rules
    )

    install_error_handlers(app)
    app.add_middleware(ConditionalGetMiddleware, max_age=settings.cache_max_age_seconds)
    data_mode = app.state.container.data_mode

    @app.middleware("http")
    async def add_data_mode_header(request, call_next):
        response = await call_next(request)
        response.headers["X-Data-Mode"] = data_mode
        return response

    app.include_router(health.router, prefix=API_PREFIX)
    public = APIRouter(prefix=API_PREFIX, dependencies=[Depends(require_device_id)])
    # 고정 경로(/parcels/lookup)는 경로 변수(/parcels/{pnu})보다 먼저 등록되도록 각 라우터 안에서 선언 순서를 지켰다
    for module in (map, complexes, parcels, glossary):
        public.include_router(module.router)
    app.include_router(public)
    return app

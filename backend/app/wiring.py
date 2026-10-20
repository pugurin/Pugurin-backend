import logging
from dataclasses import dataclass

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from app.adapters.kakao import KakaoGeocoder
from app.adapters.mock.parcel_source import MockParcelSource
from app.adapters.unconfigured import UnconfiguredParcelSource
from app.core.config import Settings
from app.core.dates import Clock, latest_etl_time
from app.db.engine import make_engine
from app.ingestion.real import load_real_data, resolve_data_mode
from app.repositories.memory.glossary import InMemoryGlossaryRepository
from app.repositories.memory.market import InMemoryMarketRepository
from app.repositories.memory.zoning import SampleZoningRules
from app.repositories.ports import (
    AddressSearch,
    GlossaryRepository,
    MarketRepository,
    ParcelSource,
    ZoningRuleRepository,
)
from app.services.analysis_service import AnalysisService
from app.services.complex_service import ComplexService
from app.services.glossary_service import GlossaryService
from app.services.map_service import MapService
from app.services.parcel_service import ParcelService
from app.services.readiness_service import ReadinessService
from app.services.search_service import SearchService
from app.services.stats_service import StatsService
from app.services.transaction_service import TransactionService

# uvicorn이 기본으로 출력하는 로거에 남겨야 서버 로그에서 보인다
logger = logging.getLogger("uvicorn.error")


@dataclass
class Container:
    settings: Settings
    data_mode: str
    map_service: MapService
    complex_service: ComplexService
    parcel_service: ParcelService
    glossary_service: GlossaryService
    search_service: SearchService
    stats_service: StatsService
    transaction_service: TransactionService
    analysis_service: AnalysisService
    readiness_service: ReadinessService
    address_search: AddressSearch | None = None
    db_engine: AsyncEngine | None = None
    redis: Redis | None = None

    async def aclose(self) -> None:
        close = getattr(self.address_search, "aclose", None)
        if close is not None:
            await close()
        if self.db_engine is not None:
            await self.db_engine.dispose()
        if self.redis is not None:
            await self.redis.aclose()


def build_container(
    settings: Settings,
    clock: Clock,
    market_repo: MarketRepository | None = None,
    glossary_repo: GlossaryRepository | None = None,
    parcel_source: ParcelSource | None = None,
    zoning_rules: ZoningRuleRepository | None = None,
    address_search: AddressSearch | None = None,
) -> Container:
    as_of = latest_etl_time(clock())
    mode = resolve_data_mode(settings)
    logger.info("데이터 모드: %s", mode)
    if market_repo is None and mode == "real":
        real = load_real_data(settings)
        market_repo = InMemoryMarketRepository(real.as_of, real.dataset, real.regions)
        # 토지 정보 공급처가 정해지기 전에는 가짜 필지를 실제처럼 내려보내지 않고 '불러올 수 없음'으로 응답한다
        parcel_source = parcel_source or UnconfiguredParcelSource()
    market_repo = market_repo or InMemoryMarketRepository(as_of)
    glossary_repo = glossary_repo or InMemoryGlossaryRepository()
    parcel_source = parcel_source or MockParcelSource(land_price_year=as_of.year)
    zoning_rules = zoning_rules or SampleZoningRules()
    # 주소 검색은 카카오 REST 키가 있을 때만 켠다
    if address_search is None and settings.kakao_rest_key:
        address_search = KakaoGeocoder(settings.kakao_rest_key)
    # DB·Redis는 아직 조회에 쓰지 않고 연결 상태만 확인한다(#38에서 저장소를 옮긴다)
    db_engine = make_engine(settings.database_url) if settings.database_url else None
    redis = Redis.from_url(settings.redis_url) if settings.redis_url else None
    parcel_service = ParcelService(parcel_source, glossary_repo, zoning_rules, settings, clock)
    return Container(
        settings=settings,
        data_mode="real" if isinstance(market_repo, InMemoryMarketRepository) and mode == "real" else "sample",
        map_service=MapService(market_repo, settings),
        complex_service=ComplexService(market_repo),
        parcel_service=parcel_service,
        glossary_service=GlossaryService(glossary_repo),
        search_service=SearchService(market_repo, address_search),
        stats_service=StatsService(market_repo),
        transaction_service=TransactionService(market_repo),
        analysis_service=AnalysisService(market_repo, parcel_service),
        readiness_service=ReadinessService(db_engine, redis),
        address_search=address_search,
        db_engine=db_engine,
        redis=redis,
    )

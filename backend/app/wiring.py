import logging
from dataclasses import dataclass

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from app.adapters.kakao import KakaoGeocoder
from app.adapters.unconfigured import UnconfiguredParcelSource
from app.core.config import Settings
from app.core.dates import Clock, latest_etl_time
from app.db.engine import make_engine
from app.ingestion.rawcache import RawCache
from app.ingestion.real import load_real_data
from app.repositories.memory.dataset import MarketDataset
from app.repositories.memory.glossary import InMemoryGlossaryRepository
from app.repositories.memory.market import InMemoryMarketRepository
from app.repositories.memory.zoning import NoZoningRules
from app.repositories.ports import (
    AddressSearch,
    GlossaryRepository,
    MarketRepository,
    ParcelSource,
    ZoningRuleRepository,
)
from app.repositories.postgres.market import PostgresMarketRepository
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
    market_repo: MarketRepository | None = None

    async def has_market_data(self) -> bool:
        """실거래 데이터가 있는지. DB면 새 적재가 있는지 확인한 뒤 답한다."""
        repo = self.market_repo
        if isinstance(repo, PostgresMarketRepository):
            await repo.refresh()
        return bool(getattr(repo, "has_data", True))

    async def startup(self) -> None:
        """DB에서 조회하면 지역·단지 스냅샷을 미리 읽는다. DB가 아직 안 떠 있어도 서버는 뜨고 readiness가 알린다."""
        if isinstance(self.market_repo, PostgresMarketRepository):
            try:
                await self.market_repo.refresh()
            except Exception as e:  # noqa: BLE001
                logger.warning("DB 스냅샷을 읽지 못했습니다: %s: %s", type(e).__name__, e)

    async def aclose(self) -> None:
        close = getattr(self.address_search, "aclose", None)
        if close is not None:
            await close()
        if self.db_engine is not None:
            await self.db_engine.dispose()
        if self.redis is not None:
            await self.redis.aclose()


def _real_market(settings: Settings, db_engine: AsyncEngine | None, as_of) -> MarketRepository:
    """실데이터만 쓴다: DB → 수집 캐시(.cache) 순서. 둘 다 없으면 빈 저장소이고 API는 '데이터 없음'으로 답한다."""
    if db_engine is not None:
        logger.info("데이터: DB")
        return PostgresMarketRepository(
            db_engine,
            fallback_as_of=as_of,
            exclude_share_deals=settings.exclude_share_deals,
            exclude_road_land=settings.exclude_road_land,
        )
    if RawCache(settings.data_dir).latest_fetch() is not None:
        logger.info("데이터: 수집 캐시 %s", settings.data_dir)
        real = load_real_data(settings)
        return InMemoryMarketRepository(real.as_of, real.dataset, real.regions)
    logger.warning("실데이터가 없습니다. PUGURIN_DATABASE_URL을 넣거나 `python -m app.ingestion refresh`로 수집하세요")
    return InMemoryMarketRepository(as_of, MarketDataset((), ()), {})


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
    db_engine = make_engine(settings.database_url) if settings.database_url else None
    if market_repo is None:
        market_repo = _real_market(settings, db_engine, as_of)
    glossary_repo = glossary_repo or InMemoryGlossaryRepository()
    # 토지 정보 공급처와 부산시 조례 표가 정해지기 전에는 추정값을 내지 않고 '불러올 수 없음'으로 응답한다
    parcel_source = parcel_source or UnconfiguredParcelSource()
    zoning_rules = zoning_rules or NoZoningRules()
    # 주소 검색은 카카오 REST 키가 있을 때만 켠다
    if address_search is None and settings.kakao_rest_key:
        address_search = KakaoGeocoder(settings.kakao_rest_key)
    redis = Redis.from_url(settings.redis_url) if settings.redis_url else None
    parcel_service = ParcelService(parcel_source, glossary_repo, zoning_rules, settings, clock)
    return Container(
        settings=settings,
        map_service=MapService(market_repo, settings),
        complex_service=ComplexService(market_repo),
        parcel_service=parcel_service,
        glossary_service=GlossaryService(glossary_repo),
        search_service=SearchService(market_repo, address_search),
        stats_service=StatsService(market_repo),
        transaction_service=TransactionService(market_repo),
        analysis_service=AnalysisService(market_repo, parcel_service),
        readiness_service=ReadinessService(db_engine, redis, market_repo),
        address_search=address_search,
        db_engine=db_engine,
        redis=redis,
        market_repo=market_repo,
    )

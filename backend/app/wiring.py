from dataclasses import dataclass

from app.adapters.mock.parcel_source import MockParcelSource
from app.adapters.unconfigured import UnconfiguredParcelSource
from app.core.config import Settings
from app.core.dates import Clock, latest_etl_time
from app.ingestion.real import load_real_data
from app.repositories.memory.glossary import InMemoryGlossaryRepository
from app.repositories.memory.market import InMemoryMarketRepository
from app.repositories.memory.zoning import SampleZoningRules
from app.repositories.ports import GlossaryRepository, MarketRepository, ParcelSource, ZoningRuleRepository
from app.services.complex_service import ComplexService
from app.services.glossary_service import GlossaryService
from app.services.map_service import MapService
from app.services.parcel_service import ParcelService


@dataclass
class Container:
    settings: Settings
    map_service: MapService
    complex_service: ComplexService
    parcel_service: ParcelService
    glossary_service: GlossaryService


def build_container(
    settings: Settings,
    clock: Clock,
    market_repo: MarketRepository | None = None,
    glossary_repo: GlossaryRepository | None = None,
    parcel_source: ParcelSource | None = None,
    zoning_rules: ZoningRuleRepository | None = None,
) -> Container:
    as_of = latest_etl_time(clock())
    if market_repo is None and settings.data_mode == "real":
        real = load_real_data(settings)
        market_repo = InMemoryMarketRepository(real.as_of, real.dataset, real.regions)
        # 토지 정보 공급처가 정해지기 전에는 가짜 필지를 실제처럼 내려보내지 않고 '불러올 수 없음'으로 응답한다
        parcel_source = parcel_source or UnconfiguredParcelSource()
    market_repo = market_repo or InMemoryMarketRepository(as_of)
    glossary_repo = glossary_repo or InMemoryGlossaryRepository()
    parcel_source = parcel_source or MockParcelSource(land_price_year=as_of.year)
    zoning_rules = zoning_rules or SampleZoningRules()
    return Container(
        settings=settings,
        map_service=MapService(market_repo, settings),
        complex_service=ComplexService(market_repo),
        parcel_service=ParcelService(parcel_source, glossary_repo, zoning_rules, settings, clock),
        glossary_service=GlossaryService(glossary_repo),
    )

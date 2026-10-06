from dataclasses import dataclass

from app.adapters.mock.parcel_source import MockParcelSource
from app.core.config import Settings
from app.core.dates import Clock, latest_etl_time
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

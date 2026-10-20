"""테스트용 앱: 서버는 가짜 데이터를 쓰지 않으므로, 테스트가 가짜 시장·필지·조례 표를 직접 끼워 넣는다."""

from datetime import datetime

from fastapi import FastAPI

from app import main
from app.core.dates import kst_now, latest_etl_time
from app.repositories.memory.market import InMemoryMarketRepository
from tests.fakes.market import build_sample_market
from tests.fakes.parcel_source import MockParcelSource
from tests.fakes.regions import sample_regions
from tests.fakes.zoning import SampleZoningRules


def sample_repo(as_of: datetime) -> InMemoryMarketRepository:
    """가짜 단지 190곳·거래 약 10만 건(5년치). 실제 데이터가 아니다."""
    return InMemoryMarketRepository(as_of, build_sample_market(as_of.date()), sample_regions())


def create_app(**kwargs) -> FastAPI:
    """app.main.create_app과 같되, 넘기지 않은 시장·필지·조례 표는 가짜로 채운다."""
    as_of = latest_etl_time((kwargs.get("clock") or kst_now)())
    kwargs.setdefault("market_repo", sample_repo(as_of))
    kwargs.setdefault("parcel_source", MockParcelSource(land_price_year=as_of.year))
    kwargs.setdefault("zoning_rules", SampleZoningRules())
    return main.create_app(**kwargs)

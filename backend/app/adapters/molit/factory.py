import logging

from app.adapters.molit.client import MolitClient
from app.adapters.molit.mock import FixtureTradeSource
from app.adapters.molit.types import TradeSource
from app.core.config import Settings

logger = logging.getLogger(__name__)


def create_trade_source(settings: Settings) -> TradeSource:
    """키가 둘 다 있으면 실제 API, 아니면 저장된 샘플 응답을 쓴다."""
    trade, rent = settings.data_go_kr_trade_key, settings.data_go_kr_rent_key
    if trade and rent:
        return MolitClient(trade, rent, base_url=settings.molit_base_url, daily_limit=settings.molit_daily_call_limit)
    if trade or rent:
        logger.warning("실거래가 API 키가 하나만 설정되어 샘플 응답을 사용합니다 (매매·전월세 키가 모두 필요)")
    return FixtureTradeSource()

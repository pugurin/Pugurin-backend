from pathlib import Path

from app.adapters.molit.parser import assign_ordinals, parse_page
from app.adapters.molit.types import RawTrade, SourceKind

SAMPLE_DIR = Path(__file__).resolve().parent.parent.parent / "sample_data" / "molit"


class FixtureTradeSource:
    """저장해 둔 실제 응답 샘플(해운대구·수영구, 2025-09)을 돌려준다. 그 밖의 지역·월은 거래가 없는 것으로 본다."""

    def __init__(self, directory: Path = SAMPLE_DIR):
        self._dir = directory

    @property
    def calls_made(self) -> int:
        return 0

    async def fetch_month(self, kind: SourceKind, lawd_cd: str, deal_ymd: str) -> list[RawTrade]:
        path = self._dir / f"{kind.value}_{lawd_cd}_{deal_ymd}.xml"
        if not path.exists():
            return []
        return assign_ordinals(parse_page(path.read_bytes(), kind).rows)

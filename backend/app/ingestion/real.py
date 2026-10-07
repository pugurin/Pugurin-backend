from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from app.core.config import Settings
from app.ingestion.build import BuildOptions, BuildReport, GeoIndex, build_dataset
from app.ingestion.geocode import GeocodeCache
from app.ingestion.rawcache import RawCache
from app.repositories.memory.dataset import MarketDataset, RegionDirectory


def resolve_data_mode(settings: Settings) -> Literal["sample", "real"]:
    if settings.data_mode == "auto":
        return "real" if RawCache(settings.data_dir).latest_fetch() is not None else "sample"
    return settings.data_mode


class RealDataMissing(RuntimeError):
    pass


@dataclass
class RealData:
    dataset: MarketDataset
    regions: RegionDirectory
    as_of: datetime
    report: BuildReport


def build_options(settings: Settings) -> BuildOptions:
    return BuildOptions(settings.exclude_share_deals, settings.exclude_road_land)


def load_real_data(settings: Settings) -> RealData:
    """수집해 둔 캐시(.cache)만으로 데이터셋을 만든다. 네트워크는 쓰지 않는다."""
    raw = RawCache(settings.data_dir)
    as_of = raw.latest_fetch()
    if as_of is None:
        raise RealDataMissing(
            f"실데이터 캐시가 없습니다({settings.data_dir}). 먼저 `uv run python -m app.ingestion refresh`를 실행하세요"
        )
    trades = (trade for window in raw.windows() for trade in raw.load(*window))
    geo = GeoIndex(GeocodeCache(settings.data_dir / "geocode.json"))
    dataset, regions, report = build_dataset(trades, geo, build_options(settings))
    return RealData(dataset, regions, as_of, report)

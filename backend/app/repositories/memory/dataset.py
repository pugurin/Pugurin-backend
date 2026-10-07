from dataclasses import dataclass
from typing import Protocol

from app.core.busan import SIGUNGU
from app.repositories.types import Complex, RegionRef, Transaction
from app.sample_data.regions import DONGS


class MarketData(Protocol):
    complexes: tuple[Complex, ...]
    transactions: tuple[Transaction, ...]


@dataclass(frozen=True)
class MarketDataset:
    complexes: tuple[Complex, ...]
    transactions: tuple[Transaction, ...]


RegionDirectory = dict[str, RegionRef]


def sample_regions() -> RegionDirectory:
    directory: RegionDirectory = {s.code: RegionRef(s.code, s.name, s.lat, s.lng) for s in SIGUNGU}
    directory.update({d.code: RegionRef(d.code, d.name, d.lat, d.lng) for d in DONGS})
    return directory

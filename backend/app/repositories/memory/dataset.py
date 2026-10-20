from dataclasses import dataclass
from typing import Protocol

from app.repositories.types import Complex, RegionRef, Transaction


class MarketData(Protocol):
    complexes: tuple[Complex, ...]
    transactions: tuple[Transaction, ...]


@dataclass(frozen=True)
class MarketDataset:
    complexes: tuple[Complex, ...]
    transactions: tuple[Transaction, ...]


RegionDirectory = dict[str, RegionRef]

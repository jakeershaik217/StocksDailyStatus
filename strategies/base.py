from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Literal, Sequence

Side = Literal["BUY", "SELL"]


@dataclass(frozen=True)
class Bar:
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


@dataclass(frozen=True)
class Signal:
    symbol: str
    side: Side
    strength: Decimal
    reason: str
    strategy: str
    reference_price: Decimal
    metadata: dict[str, str] = field(default_factory=dict)


class Strategy(ABC):
    """A deterministic rule set: bars in, signals out.

    Strategies must be pure functions of their inputs so they can be
    backtested and unit-tested without a broker or network.
    """

    name: str = "strategy"

    @property
    @abstractmethod
    def min_bars(self) -> int:
        """Smallest history length needed before a signal can be produced."""

    @abstractmethod
    def evaluate(self, symbol: str, bars: Sequence[Bar], position_qty: int) -> Signal | None:
        """Return a Signal for the latest bar, or None to hold."""

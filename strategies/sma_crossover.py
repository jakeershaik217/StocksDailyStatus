from __future__ import annotations

from decimal import Decimal
from typing import Sequence

from strategies.base import Bar, Signal, Strategy


def simple_moving_average(bars: Sequence[Bar], window: int) -> Decimal:
    if window <= 0:
        raise ValueError("window must be positive")
    if len(bars) < window:
        raise ValueError(f"need {window} bars, got {len(bars)}")
    return sum((b.close for b in bars[-window:]), Decimal(0)) / window


class SmaCrossover(Strategy):
    """Buy when the fast SMA crosses above the slow SMA; sell on the reverse cross."""

    name = "sma_crossover"

    def __init__(self, fast: int = 10, slow: int = 30) -> None:
        if fast >= slow:
            raise ValueError("fast window must be shorter than slow window")
        self.fast = fast
        self.slow = slow

    @property
    def min_bars(self) -> int:
        return self.slow + 1

    def evaluate(self, symbol: str, bars: Sequence[Bar], position_qty: int) -> Signal | None:
        if len(bars) < self.min_bars:
            return None
        prev = bars[:-1]
        fast_now = simple_moving_average(bars, self.fast)
        slow_now = simple_moving_average(bars, self.slow)
        fast_prev = simple_moving_average(prev, self.fast)
        slow_prev = simple_moving_average(prev, self.slow)

        crossed_up = fast_prev <= slow_prev and fast_now > slow_now
        crossed_down = fast_prev >= slow_prev and fast_now < slow_now
        spread = abs(fast_now - slow_now) / slow_now if slow_now else Decimal(0)
        meta = {
            "fast_sma": f"{fast_now:.2f}",
            "slow_sma": f"{slow_now:.2f}",
        }

        if crossed_up and position_qty <= 0:
            return Signal(
                symbol=symbol,
                side="BUY",
                strength=min(Decimal(1), spread * 100),
                reason=f"SMA{self.fast} crossed above SMA{self.slow}",
                strategy=self.name,
                reference_price=bars[-1].close,
                metadata=meta,
            )
        if crossed_down and position_qty > 0:
            return Signal(
                symbol=symbol,
                side="SELL",
                strength=min(Decimal(1), spread * 100),
                reason=f"SMA{self.fast} crossed below SMA{self.slow}",
                strategy=self.name,
                reference_price=bars[-1].close,
                metadata=meta,
            )
        return None

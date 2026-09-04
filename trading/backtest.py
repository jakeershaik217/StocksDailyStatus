from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Sequence

from strategies.base import Bar, Strategy


@dataclass
class Trade:
    side: str
    timestamp: str
    price: Decimal
    quantity: int
    reason: str


@dataclass
class BacktestResult:
    symbol: str
    strategy: str
    starting_cash: Decimal
    ending_equity: Decimal
    trades: list[Trade] = field(default_factory=list)

    @property
    def return_pct(self) -> Decimal:
        if self.starting_cash == 0:
            return Decimal(0)
        return (self.ending_equity - self.starting_cash) / self.starting_cash * 100

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "strategy": self.strategy,
            "starting_cash": str(self.starting_cash),
            "ending_equity": str(self.ending_equity),
            "return_pct": f"{self.return_pct:.2f}",
            "trade_count": len(self.trades),
            "trades": [
                {"side": t.side, "timestamp": t.timestamp, "price": str(t.price), "quantity": t.quantity, "reason": t.reason}
                for t in self.trades
            ],
        }


def run_backtest(
    strategy: Strategy,
    symbol: str,
    bars: Sequence[Bar],
    starting_cash: Decimal = Decimal("100000"),
    target_notional: Decimal | None = None,
) -> BacktestResult:
    """Walk bars forward, filling each signal at the next bar's open (no look-ahead)."""
    cash = starting_cash
    qty = 0
    trades: list[Trade] = []
    budget = target_notional or starting_cash

    for i in range(strategy.min_bars, len(bars)):
        history = bars[:i]
        signal = strategy.evaluate(symbol, history, qty)
        if signal is None:
            continue
        fill = bars[i]
        if signal.side == "BUY" and qty == 0:
            size = int(min(budget, cash) // fill.open)
            if size <= 0:
                continue
            qty = size
            cash -= fill.open * size
            trades.append(Trade("BUY", fill.timestamp.isoformat(), fill.open, size, signal.reason))
        elif signal.side == "SELL" and qty > 0:
            cash += fill.open * qty
            trades.append(Trade("SELL", fill.timestamp.isoformat(), fill.open, qty, signal.reason))
            qty = 0

    ending = cash + qty * bars[-1].close
    return BacktestResult(symbol, strategy.name, starting_cash, ending, trades)

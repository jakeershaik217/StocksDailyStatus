import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from strategies import build_strategy, list_strategies
from strategies.base import Bar
from strategies.sma_crossover import SmaCrossover, simple_moving_average
from trading.backtest import run_backtest
from trading.broker import _bar_from_row

FIXTURE = Path(__file__).parent / "fixtures" / "strategy_bars.json"


def _bars(closes):
    start = datetime(2026, 1, 1)
    return [
        Bar(start + timedelta(days=i), Decimal(c), Decimal(c) + 1, Decimal(c) - 1, Decimal(c), 1000)
        for i, c in enumerate(closes)
    ]


def fixture_bars():
    return [_bar_from_row(r) for r in json.loads(FIXTURE.read_text())["DEMO"]]


def test_registry_builds_known_strategy():
    assert "sma_crossover" in list_strategies()
    strategy = build_strategy("sma_crossover", {"fast": 2, "slow": 3})
    assert isinstance(strategy, SmaCrossover)
    with pytest.raises(ValueError):
        build_strategy("does_not_exist")


def test_sma_rejects_bad_windows():
    with pytest.raises(ValueError):
        SmaCrossover(fast=5, slow=5)
    with pytest.raises(ValueError):
        simple_moving_average(_bars([1, 2]), 3)


def test_sma_crossover_emits_buy_on_upward_cross_only_when_flat():
    strategy = SmaCrossover(fast=2, slow=3)
    # fast(2) <= slow(3) at bar 4, then fast > slow at bar 5
    bars = _bars([10, 9, 8, 8, 12])
    signal = strategy.evaluate("X", bars, position_qty=0)
    assert signal is not None and signal.side == "BUY"
    assert signal.reference_price == Decimal(12)
    assert strategy.evaluate("X", bars, position_qty=10) is None


def test_sma_crossover_emits_sell_when_holding():
    strategy = SmaCrossover(fast=2, slow=3)
    bars = _bars([10, 11, 12, 12, 8])
    signal = strategy.evaluate("X", bars, position_qty=5)
    assert signal is not None and signal.side == "SELL"
    assert strategy.evaluate("X", bars, position_qty=0) is None


def test_no_signal_without_enough_history():
    assert SmaCrossover(fast=2, slow=3).evaluate("X", _bars([1, 2, 3]), 0) is None


def test_backtest_on_fixture_round_trips_without_lookahead():
    bars = fixture_bars()
    result = run_backtest(SmaCrossover(fast=5, slow=15), "DEMO", bars, Decimal("100000"))
    assert result.trades, "fixture should produce at least one trade"
    first = result.trades[0]
    assert first.side == "BUY"
    # fill happens at the open of the bar after the signal, never at the signal bar's close
    signal_idx = next(i for i, b in enumerate(bars) if b.timestamp.isoformat() == first.timestamp)
    assert first.price == bars[signal_idx].open
    assert result.to_dict()["trade_count"] == len(result.trades)

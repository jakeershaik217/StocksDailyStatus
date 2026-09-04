from __future__ import annotations

import json
import os
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from strategies import build_strategy
from trading.backtest import run_backtest
from trading.broker import KiteBroker, _bar_from_row


def main() -> int:
    name = os.environ.get("STRATEGY_NAME", "sma_crossover")
    params = json.loads(os.environ.get("STRATEGY_PARAMS", "{}"))
    symbol = os.environ["BACKTEST_SYMBOL"]
    exchange = os.environ.get("BACKTEST_EXCHANGE", "NSE")
    interval = os.environ.get("BACKTEST_INTERVAL", "day")
    lookback = int(os.environ.get("BACKTEST_LOOKBACK", "250"))
    fixture = os.environ.get("STRATEGY_FIXTURE_FILE", "").strip()

    if fixture:
        rows = json.loads(Path(fixture).read_text())[symbol]
        bars = [_bar_from_row(r) for r in rows]
    else:
        broker = KiteBroker(os.environ.get("KITE_API_KEY", ""), os.environ.get("KITE_ACCESS_TOKEN", ""))
        bars = broker.historical_bars(symbol, exchange, interval, lookback)

    strategy = build_strategy(name, params)
    result = run_backtest(strategy, symbol, bars, Decimal(os.environ.get("BACKTEST_CASH", "100000")))
    Path("backtest.json").write_text(json.dumps(result.to_dict(), indent=2))
    print(json.dumps({k: v for k, v in result.to_dict().items() if k != "trades"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

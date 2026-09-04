# AI-assisted strategy automation (Zerodha Kite)

Deterministic strategies decide, a code-level risk gate validates, an optional
LLM reviewer can only veto, and the broker adapter executes. The LLM never
creates or sizes orders.

```
Kite (bars, LTP, holdings) -> strategies/* -> Signal -> trading/risk.py -> trading/ai.py (veto only)
                                                                   -> trading/broker.py (Paper | Kite)
                                                                   -> strategy-run.{json,md}
```

## Layout

| Path | Purpose |
|---|---|
| `strategies/base.py` | `Bar`, `Signal`, `Strategy` ABC (`evaluate(symbol, bars, position_qty)`) |
| `strategies/sma_crossover.py` | Example strategy; copy it to add your own, then register in `strategies/registry.py` |
| `trading/broker.py` | `Broker` interface, `PaperBroker` (JSON ledger), `KiteBroker` (Kite Connect v3 REST) |
| `trading/risk.py` | `RiskLimits` + pure `validate_order()`; market hours, notional, exposure, kill switch |
| `trading/ai.py` | `SignalReviewer`: OpenAI second opinion, fails closed, disabled without `OPENAI_API_KEY` |
| `trading/backtest.py` | Walk-forward backtester, fills at next bar open |
| `trading/runner.py` | Loads `strategies.json`, runs every strategy/symbol, writes reports |
| `scripts/run_strategies.py` | Entry point (`TRADING_MODE=paper|live`) |
| `scripts/backtest_strategy.py` | Backtest one strategy on Kite or fixture data |
| `scripts/kite_login.py` | Exchange `request_token` for the daily `access_token` |
| `workflows/strategy-runner.yml` | PR smoke tests; weekday scheduled paper run; manual live run. Move to `.github/workflows/` to activate (Devin's token lacks the `workflow` scope to push there) |

## Configuration (`strategies.json`)

```json
{
  "exchange": "NSE", "interval": "day", "lookback": 60,
  "target_notional": "20000", "product": "CNC",
  "risk": {"max_order_notional": "25000", "max_position_notional": "50000",
           "max_gross_exposure": "200000", "max_orders_per_run": 5,
           "require_market_hours": true},
  "strategies": [{"name": "sma_crossover", "params": {"fast": 10, "slow": 30},
                  "symbols": ["INFY", "TCS", "HINDCOPPER"]}]
}
```

`risk.allowed_symbols` defaults to the union of configured symbols.

## Environment variables

| Variable | Meaning |
|---|---|
| `TRADING_MODE` | `paper` (default) or `live` |
| `TRADING_LIVE_CONFIRM` | must be `I_UNDERSTAND_REAL_MONEY` for live mode |
| `TRADING_KILL_SWITCH` | `true` evaluates signals but blocks every order |
| `TRADING_REQUIRE_MARKET_HOURS` | override the config flag (`false` for after-hours dry runs) |
| `KITE_API_KEY`, `KITE_ACCESS_TOKEN` | Kite Connect credentials; token expires daily |
| `OPENAI_API_KEY`, `OPENAI_MODEL` | enables the AI reviewer (default model `gpt-4o-mini`) |
| `STRATEGY_CONFIG` | config path (default `strategies.json`) |
| `STRATEGY_FIXTURE_FILE` | `{symbol: [[ts,o,h,l,c,v],...]}` JSON; replaces Kite data for offline runs |
| `PAPER_LEDGER` | paper ledger path (default `paper-ledger.json`) |

## Daily workflow

1. Get a Kite access token (expires every day):
   open `https://kite.zerodha.com/connect/login?v=3&api_key=<KITE_API_KEY>`, log in,
   copy `request_token` from the redirect URL, then
   `KITE_API_KEY=... KITE_API_SECRET=... KITE_REQUEST_TOKEN=... python scripts/kite_login.py`
   and store the output as the `KITE_ACCESS_TOKEN` repository secret.
2. Paper run against live Kite data: `python scripts/run_strategies.py`
3. Backtest: `BACKTEST_SYMBOL=INFY BACKTEST_LOOKBACK=250 python scripts/backtest_strategy.py`
4. Offline smoke (no credentials):
   `STRATEGY_CONFIG=tests/fixtures/strategies_smoke.json STRATEGY_FIXTURE_FILE=tests/fixtures/strategy_bars.json python scripts/run_strategies.py`

The scheduled GitHub Action runs paper mode at 14:45 IST on weekdays and keeps
the paper ledger in the Actions cache. Live mode is manual only
(`workflow_dispatch` with `mode=live` and the confirmation phrase) and uses the
`trading` GitHub environment so you can add required reviewers.

## Adding a strategy with Devin

Describe the rule in plain English (e.g. "buy when RSI(14) < 30 and close is
above the 200-day SMA, exit when RSI > 55"). Devin implements it as a
`Strategy` subclass, registers it, adds unit tests plus a backtest fixture, and
opens a PR. Merge, add it to `strategies.json`, and the scheduled paper run picks
it up.

## Safety notes

- Start in paper mode; only flip to live after weeks of paper reports.
- Limits live in code (`RiskLimits`), not in prompts. AI can only veto.
- Kite tokens expire daily; the runner fails closed when credentials are missing.
- SEBI algo rules apply to API order flow; keep order rates modest and follow
  your broker's approval requirements.

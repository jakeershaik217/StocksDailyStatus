import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from strategies.base import Signal
from trading.ai import SignalReviewer, parse_review
from trading.broker import OrderRequest, PaperBroker, Portfolio, Position
from trading.risk import IST, RiskLimits, size_order, validate_order
from trading.runner import RunnerConfig, run_once, write_report

FIXTURE = Path(__file__).parent / "fixtures" / "strategy_bars.json"
MARKET_OPEN = datetime(2026, 9, 3, 10, 0, tzinfo=IST)  # Thursday


def _order(side="BUY", qty=10, symbol="DEMO"):
    return OrderRequest(symbol, "NSE", side, qty)


def test_risk_gate_blocks_kill_switch_and_market_hours():
    pf = Portfolio(cash=Decimal("100000"))
    limits = RiskLimits(kill_switch=True)
    assert not validate_order(_order(), Decimal(100), pf, limits, now=MARKET_OPEN).approved
    weekend = datetime(2026, 9, 5, 10, 0, tzinfo=IST)
    decision = validate_order(_order(), Decimal(100), pf, RiskLimits(), now=weekend)
    assert not decision.approved and "market hours" in decision.reason


def test_risk_gate_enforces_notional_and_symbol_limits():
    pf = Portfolio(cash=Decimal("100000"))
    limits = RiskLimits(max_order_notional=Decimal(500), allowed_symbols=frozenset({"DEMO"}))
    assert not validate_order(_order(qty=10), Decimal(100), pf, limits, now=MARKET_OPEN).approved
    assert validate_order(_order(qty=5), Decimal(100), pf, limits, now=MARKET_OPEN).approved
    assert not validate_order(_order(symbol="OTHER", qty=1), Decimal(1), pf, limits, now=MARKET_OPEN).approved
    assert not validate_order(_order(qty=1), Decimal(1), pf, limits, orders_already_approved=5, now=MARKET_OPEN).approved


def test_risk_gate_blocks_oversell_and_overspend():
    pf = Portfolio(cash=Decimal("500"), positions={"DEMO": Position("DEMO", 3, Decimal(100))})
    limits = RiskLimits(require_market_hours=False)
    assert not validate_order(_order("SELL", qty=5), Decimal(100), pf, limits).approved
    assert validate_order(_order("SELL", qty=3), Decimal(100), pf, limits).approved
    assert not validate_order(_order("BUY", qty=10), Decimal(100), pf, limits).approved


def test_size_order_rounds_to_lot():
    assert size_order(Decimal("101.5"), Decimal("20000")) == 197
    assert size_order(Decimal("101.5"), Decimal("20000"), lot_size=50) == 150
    assert size_order(Decimal(0), Decimal("20000")) == 0


def test_paper_broker_fills_and_persists_ledger(tmp_path):
    fixture = json.loads(FIXTURE.read_text())
    ledger = tmp_path / "ledger.json"
    broker = PaperBroker(ledger, Decimal("10000"), fixture=fixture)
    last = broker.last_price("DEMO", "NSE")
    result = broker.place_order(_order("BUY", 10))
    assert result.status == "COMPLETE"
    assert broker.portfolio().quantity("DEMO") == 10
    assert broker.portfolio().cash == Decimal("10000") - last * 10

    rejected = broker.place_order(_order("SELL", 11))
    assert rejected.status == "REJECTED"

    reloaded = PaperBroker(ledger, Decimal("999"), fixture=fixture)
    assert reloaded.portfolio().quantity("DEMO") == 10
    assert reloaded.portfolio().cash == broker.portfolio().cash


def test_ai_reviewer_disabled_without_key_and_fails_closed_on_garbage():
    reviewer = SignalReviewer(api_key=None)
    assert not reviewer.enabled
    sig = Signal("DEMO", "BUY", Decimal(1), "r", "s", Decimal(1))
    assert reviewer.review(sig, []) is None
    assert parse_review("not json", "m").veto is True
    ok = parse_review('{"veto": false, "confidence": 0.8, "rationale": "fine"}', "m")
    assert ok.veto is False and ok.confidence == Decimal("0.8")


def _config(tmp_path, **risk):
    cfg = {
        "exchange": "NSE",
        "interval": "day",
        "lookback": 60,
        "target_notional": "5000",
        "risk": {"require_market_hours": False, **risk},
        "strategies": [{"name": "sma_crossover", "params": {"fast": 5, "slow": 15}, "symbols": ["DEMO"]}],
    }
    path = tmp_path / "strategies.json"
    path.write_text(json.dumps(cfg))
    return RunnerConfig.from_file(path)


def test_runner_end_to_end_on_fixture_paper_broker(tmp_path, monkeypatch):
    monkeypatch.delenv("TRADING_KILL_SWITCH", raising=False)
    monkeypatch.delenv("TRADING_REQUIRE_MARKET_HOURS", raising=False)
    config = _config(tmp_path)
    fixture = json.loads(FIXTURE.read_text())
    # Trim to the bar right after the upward cross so the latest bar carries a BUY signal.
    rows = fixture["DEMO"]
    from strategies.sma_crossover import SmaCrossover
    from trading.broker import _bar_from_row

    bars = [_bar_from_row(r) for r in rows]
    strat = SmaCrossover(5, 15)
    cut = next(i for i in range(strat.min_bars, len(bars) + 1) if strat.evaluate("DEMO", bars[:i], 0) is not None)
    broker = PaperBroker(tmp_path / "ledger.json", Decimal("100000"), fixture={"DEMO": rows[:cut]})

    outcomes = run_once(config, broker, SignalReviewer(None), mode="paper")
    assert len(outcomes) == 1
    o = outcomes[0]
    assert o.signal.side == "BUY" and o.risk.approved and o.result.status == "COMPLETE"
    assert broker.portfolio().quantity("DEMO") == o.order.quantity > 0

    write_report(outcomes, "paper", tmp_path / "r.json", tmp_path / "r.md")
    report = json.loads((tmp_path / "r.json").read_text())
    assert report["outcomes"][0]["order"]["status"] == "COMPLETE"
    assert "| DEMO | BUY |" in (tmp_path / "r.md").read_text()


def test_runner_kill_switch_env_blocks_orders(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_KILL_SWITCH", "true")
    config = _config(tmp_path)
    assert config.risk.kill_switch is True
    fixture = json.loads(FIXTURE.read_text())
    broker = PaperBroker(tmp_path / "ledger.json", Decimal("100000"), fixture=fixture)
    for o in run_once(config, broker, SignalReviewer(None), mode="paper"):
        assert o.result is None and not o.risk.approved


def test_config_defaults_allowed_symbols_to_configured_universe(tmp_path, monkeypatch):
    monkeypatch.delenv("TRADING_KILL_SWITCH", raising=False)
    config = _config(tmp_path)
    assert config.risk.allowed_symbols == frozenset({"DEMO"})
    with pytest.raises(FileNotFoundError):
        RunnerConfig.from_file(tmp_path / "missing.json")

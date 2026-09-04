from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from strategies import build_strategy
from strategies.base import Bar, Signal, Strategy
from trading.ai import AiReview, SignalReviewer
from trading.broker import Broker, KiteBroker, OrderRequest, OrderResult, PaperBroker
from trading.risk import RiskDecision, RiskLimits, size_order, validate_order


@dataclass(frozen=True)
class StrategyConfig:
    name: str
    symbols: tuple[str, ...]
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RunnerConfig:
    exchange: str
    interval: str
    lookback: int
    target_notional: Decimal
    product: str
    risk: RiskLimits
    strategies: tuple[StrategyConfig, ...]

    @classmethod
    def from_file(cls, path: Path) -> "RunnerConfig":
        raw = json.loads(path.read_text())
        risk_raw = raw.get("risk", {})
        all_symbols = {s for st in raw["strategies"] for s in st["symbols"]}
        risk = RiskLimits(
            max_order_notional=Decimal(str(risk_raw.get("max_order_notional", "25000"))),
            max_position_notional=Decimal(str(risk_raw.get("max_position_notional", "50000"))),
            max_gross_exposure=Decimal(str(risk_raw.get("max_gross_exposure", "200000"))),
            max_orders_per_run=int(risk_raw.get("max_orders_per_run", 5)),
            allowed_symbols=frozenset(risk_raw.get("allowed_symbols", sorted(all_symbols))),
            require_market_hours=_env_bool("TRADING_REQUIRE_MARKET_HOURS", risk_raw.get("require_market_hours", True)),
            kill_switch=_env_bool("TRADING_KILL_SWITCH", False),
        )
        return cls(
            exchange=raw.get("exchange", "NSE"),
            interval=raw.get("interval", "day"),
            lookback=int(raw.get("lookback", 60)),
            target_notional=Decimal(str(raw.get("target_notional", "20000"))),
            product=raw.get("product", "CNC"),
            risk=risk,
            strategies=tuple(
                StrategyConfig(st["name"], tuple(st["symbols"]), st.get("params", {})) for st in raw["strategies"]
            ),
        )


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return bool(default)
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class SignalOutcome:
    signal: Signal
    order: OrderRequest | None
    risk: RiskDecision | None
    ai: AiReview | None
    result: OrderResult | None
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "symbol": self.signal.symbol,
            "side": self.signal.side,
            "strategy": self.signal.strategy,
            "reason": self.signal.reason,
            "strength": str(self.signal.strength),
            "reference_price": str(self.signal.reference_price),
            "quantity": self.order.quantity if self.order else 0,
            "risk": {"approved": self.risk.approved, "reason": self.risk.reason} if self.risk else None,
            "ai": (
                {"veto": self.ai.veto, "confidence": str(self.ai.confidence), "rationale": self.ai.rationale, "model": self.ai.model}
                if self.ai
                else None
            ),
            "order": (
                {"order_id": self.result.order_id, "status": self.result.status, "message": self.result.message}
                if self.result
                else None
            ),
            "note": self.note,
        }


def build_broker(mode: str, config: RunnerConfig, ledger_path: Path, fixture_path: Path | None) -> Broker:
    if mode == "live":
        return KiteBroker(os.environ.get("KITE_API_KEY", ""), os.environ.get("KITE_ACCESS_TOKEN", ""))
    if mode != "paper":
        raise ValueError(f"TRADING_MODE must be 'paper' or 'live', got {mode!r}")
    if fixture_path is not None:
        return PaperBroker(ledger_path, fixture=json.loads(fixture_path.read_text()))
    api_key, token = os.environ.get("KITE_API_KEY", ""), os.environ.get("KITE_ACCESS_TOKEN", "")
    data_source = KiteBroker(api_key, token) if api_key and token else None
    if data_source is None:
        raise ValueError("paper mode needs KITE_API_KEY/KITE_ACCESS_TOKEN for market data or STRATEGY_FIXTURE_FILE")
    return PaperBroker(ledger_path, data_source=data_source)


def run_once(
    config: RunnerConfig,
    broker: Broker,
    reviewer: SignalReviewer,
    *,
    mode: str,
    now: datetime | None = None,
) -> list[SignalOutcome]:
    portfolio = broker.portfolio()
    outcomes: list[SignalOutcome] = []
    approved = 0
    prices: dict[str, Decimal] = {}

    strategies: list[tuple[Strategy, StrategyConfig]] = [
        (build_strategy(st.name, st.params), st) for st in config.strategies
    ]

    for strategy, st_cfg in strategies:
        for symbol in st_cfg.symbols:
            bars: list[Bar] = broker.historical_bars(symbol, config.exchange, config.interval, config.lookback)
            if len(bars) < strategy.min_bars:
                continue
            held = portfolio.quantity(symbol)
            signal = strategy.evaluate(symbol, bars, held)
            if signal is None:
                continue
            price = bars[-1].close
            prices[symbol] = price

            if signal.side == "BUY":
                qty = size_order(price, config.target_notional)
            else:
                qty = held
            order = OrderRequest(symbol, config.exchange, signal.side, qty, product=config.product, tag=strategy.name)
            outcome = SignalOutcome(signal, order, None, None, None)

            outcome.risk = validate_order(
                order, price, portfolio, config.risk, orders_already_approved=approved, now=now, prices=prices
            )
            if not outcome.risk.approved:
                outcome.note = "rejected by risk gate"
                outcomes.append(outcome)
                continue

            outcome.ai = reviewer.review(signal, bars)
            if outcome.ai is not None and outcome.ai.veto:
                outcome.note = "vetoed by AI reviewer"
                outcomes.append(outcome)
                continue

            approved += 1
            outcome.result = broker.place_order(order)
            outcome.note = f"order sent ({mode})"
            outcomes.append(outcome)
    return outcomes


def write_report(outcomes: list[SignalOutcome], mode: str, json_path: Path, md_path: Path) -> None:
    generated = datetime.now(timezone.utc).isoformat()
    json_path.write_text(
        json.dumps({"generated_at": generated, "mode": mode, "outcomes": [o.to_dict() for o in outcomes]}, indent=2)
    )
    lines = [f"# Strategy run ({mode})", "", f"Generated: {generated}", ""]
    if not outcomes:
        lines.append("No signals generated.")
    else:
        lines.append("| Symbol | Side | Qty | Strategy | Reason | Risk | AI | Outcome |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for o in outcomes:
            risk = "approved" if o.risk and o.risk.approved else (o.risk.reason if o.risk else "-")
            ai = "-" if o.ai is None else ("VETO: " if o.ai.veto else "pass: ") + o.ai.rationale
            result = f"{o.result.status} {o.result.order_id}".strip() if o.result else o.note
            lines.append(
                f"| {o.signal.symbol} | {o.signal.side} | {o.order.quantity if o.order else 0} | {o.signal.strategy} "
                f"| {o.signal.reason} | {risk} | {ai} | {result} |"
            )
    md_path.write_text("\n".join(lines) + "\n")

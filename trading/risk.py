from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal

from trading.broker import OrderRequest, Portfolio

IST = timezone(timedelta(hours=5, minutes=30))
NSE_OPEN = time(9, 15)
NSE_CLOSE = time(15, 30)


@dataclass(frozen=True)
class RiskLimits:
    max_order_notional: Decimal = Decimal("25000")
    max_position_notional: Decimal = Decimal("50000")
    max_gross_exposure: Decimal = Decimal("200000")
    max_orders_per_run: int = 5
    allowed_symbols: frozenset[str] = field(default_factory=frozenset)
    require_market_hours: bool = True
    kill_switch: bool = False


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reason: str


def validate_order(
    order: OrderRequest,
    price: Decimal,
    portfolio: Portfolio,
    limits: RiskLimits,
    *,
    orders_already_approved: int = 0,
    now: datetime | None = None,
    prices: dict[str, Decimal] | None = None,
) -> RiskDecision:
    """Pure, deterministic gate every order must pass before reaching a broker."""
    if limits.kill_switch:
        return RiskDecision(False, "kill switch engaged")
    if order.quantity <= 0:
        return RiskDecision(False, "quantity must be positive")
    if limits.allowed_symbols and order.symbol not in limits.allowed_symbols:
        return RiskDecision(False, f"{order.symbol} not in allowed symbols")
    if orders_already_approved >= limits.max_orders_per_run:
        return RiskDecision(False, f"max {limits.max_orders_per_run} orders per run reached")

    if limits.require_market_hours:
        current = (now or datetime.now(IST)).astimezone(IST)
        if current.weekday() >= 5 or not (NSE_OPEN <= current.time() <= NSE_CLOSE):
            return RiskDecision(False, f"outside NSE market hours ({current:%a %H:%M} IST)")

    notional = price * order.quantity
    if notional > limits.max_order_notional:
        return RiskDecision(False, f"order notional {notional} > {limits.max_order_notional}")

    held = portfolio.quantity(order.symbol)
    if order.side == "BUY":
        resulting = (held + order.quantity) * price
        if resulting > limits.max_position_notional:
            return RiskDecision(False, f"position notional {resulting} > {limits.max_position_notional}")
        gross = portfolio.notional(prices or {}) + notional
        if gross > limits.max_gross_exposure:
            return RiskDecision(False, f"gross exposure {gross} > {limits.max_gross_exposure}")
        if notional > portfolio.cash:
            return RiskDecision(False, f"insufficient cash {portfolio.cash} for {notional}")
    elif order.quantity > held:
        return RiskDecision(False, f"sell {order.quantity} exceeds holding {held}")

    return RiskDecision(True, "ok")


def size_order(signal_price: Decimal, target_notional: Decimal, lot_size: int = 1) -> int:
    if signal_price <= 0:
        return 0
    qty = int(target_notional // signal_price)
    return (qty // lot_size) * lot_size

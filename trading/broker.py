from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Sequence

import requests

from strategies.base import Bar, Side


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    exchange: str
    side: Side
    quantity: int
    order_type: str = "MARKET"
    product: str = "CNC"
    limit_price: Decimal | None = None
    tag: str = "auto"


@dataclass(frozen=True)
class OrderResult:
    order_id: str
    status: str
    request: OrderRequest
    message: str = ""


@dataclass
class Position:
    symbol: str
    quantity: int
    average_price: Decimal


@dataclass
class Portfolio:
    cash: Decimal
    positions: dict[str, Position] = field(default_factory=dict)

    def quantity(self, symbol: str) -> int:
        pos = self.positions.get(symbol)
        return pos.quantity if pos else 0

    def notional(self, prices: dict[str, Decimal]) -> Decimal:
        return sum((p.quantity * prices.get(s, p.average_price) for s, p in self.positions.items()), Decimal(0))


class Broker(ABC):
    @abstractmethod
    def historical_bars(self, symbol: str, exchange: str, interval: str, lookback: int) -> list[Bar]: ...

    @abstractmethod
    def last_price(self, symbol: str, exchange: str) -> Decimal: ...

    @abstractmethod
    def portfolio(self) -> Portfolio: ...

    @abstractmethod
    def place_order(self, order: OrderRequest) -> OrderResult: ...


def _bar_from_row(row: Sequence) -> Bar:
    ts = row[0]
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts)
    return Bar(
        timestamp=ts,
        open=Decimal(str(row[1])),
        high=Decimal(str(row[2])),
        low=Decimal(str(row[3])),
        close=Decimal(str(row[4])),
        volume=int(row[5]),
    )


class PaperBroker(Broker):
    """Simulated broker with a JSON-file ledger.

    Historical data comes from a caller-supplied ``data_source`` (any Broker
    or object exposing ``historical_bars`` / ``last_price``), or from a
    fixture dict ``{symbol: [[ts, o, h, l, c, v], ...]}``. Fills are
    immediate at the reference price.
    """

    def __init__(
        self,
        ledger_path: Path,
        starting_cash: Decimal = Decimal("100000"),
        data_source: Broker | None = None,
        fixture: dict[str, list] | None = None,
    ) -> None:
        self.ledger_path = ledger_path
        self.data_source = data_source
        self.fixture = fixture or {}
        self._portfolio = self._load(starting_cash)

    def _load(self, starting_cash: Decimal) -> Portfolio:
        if not self.ledger_path.exists():
            return Portfolio(cash=starting_cash)
        raw = json.loads(self.ledger_path.read_text())
        return Portfolio(
            cash=Decimal(raw["cash"]),
            positions={
                s: Position(s, int(p["quantity"]), Decimal(p["average_price"]))
                for s, p in raw.get("positions", {}).items()
            },
        )

    def _save(self) -> None:
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        self.ledger_path.write_text(
            json.dumps(
                {
                    "cash": str(self._portfolio.cash),
                    "positions": {
                        s: {"quantity": p.quantity, "average_price": str(p.average_price)}
                        for s, p in self._portfolio.positions.items()
                        if p.quantity
                    },
                },
                indent=2,
            )
        )

    def historical_bars(self, symbol: str, exchange: str, interval: str, lookback: int) -> list[Bar]:
        if self.data_source is not None:
            return self.data_source.historical_bars(symbol, exchange, interval, lookback)
        rows = self.fixture.get(symbol)
        if rows is None:
            raise LookupError(f"No fixture bars for {symbol}")
        return [_bar_from_row(r) for r in rows[-lookback:]]

    def last_price(self, symbol: str, exchange: str) -> Decimal:
        if self.data_source is not None:
            return self.data_source.last_price(symbol, exchange)
        bars = self.historical_bars(symbol, exchange, "day", 1)
        return bars[-1].close

    def portfolio(self) -> Portfolio:
        return self._portfolio

    def place_order(self, order: OrderRequest) -> OrderResult:
        price = order.limit_price or self.last_price(order.symbol, order.exchange)
        pf = self._portfolio
        pos = pf.positions.get(order.symbol) or Position(order.symbol, 0, Decimal(0))
        if order.side == "BUY":
            cost = price * order.quantity
            if cost > pf.cash:
                return OrderResult("", "REJECTED", order, f"insufficient paper cash {pf.cash} < {cost}")
            new_qty = pos.quantity + order.quantity
            pos.average_price = ((pos.average_price * pos.quantity) + cost) / new_qty
            pos.quantity = new_qty
            pf.cash -= cost
        else:
            if order.quantity > pos.quantity:
                return OrderResult("", "REJECTED", order, f"cannot sell {order.quantity}, holding {pos.quantity}")
            pos.quantity -= order.quantity
            pf.cash += price * order.quantity
        pf.positions[order.symbol] = pos
        self._save()
        digest = hashlib.sha1(f"{order}{datetime.now().isoformat()}".encode()).hexdigest()[:12]
        return OrderResult(f"paper-{digest}", "COMPLETE", order, f"filled @ {price}")


class KiteBroker(Broker):
    """Zerodha Kite Connect v3 REST adapter (https://kite.trade/docs/connect/v3/).

    Requires ``api_key`` and a same-day ``access_token`` (Kite tokens expire
    daily; see scripts/kite_login.py). Instrument tokens for historical data
    are resolved from the instruments dump on first use.
    """

    BASE_URL = "https://api.kite.trade"

    def __init__(self, api_key: str, access_token: str, session: requests.Session | None = None) -> None:
        if not api_key or not access_token:
            raise ValueError("KiteBroker needs api_key and access_token")
        self.session = session or requests.Session()
        self.session.headers.update(
            {"X-Kite-Version": "3", "Authorization": f"token {api_key}:{access_token}"}
        )
        self._instrument_tokens: dict[str, int] = {}

    def _get(self, path: str, **params) -> dict:
        resp = self.session.get(f"{self.BASE_URL}{path}", params=params, timeout=15)
        return self._unwrap(resp)

    def _post(self, path: str, data: dict) -> dict:
        resp = self.session.post(f"{self.BASE_URL}{path}", data=data, timeout=15)
        return self._unwrap(resp)

    @staticmethod
    def _unwrap(resp: requests.Response) -> dict:
        payload = resp.json()
        if resp.status_code >= 400 or payload.get("status") != "success":
            raise RuntimeError(f"Kite API error {resp.status_code}: {payload.get('message', payload)}")
        return payload["data"]

    def instrument_token(self, symbol: str, exchange: str) -> int:
        key = f"{exchange}:{symbol}"
        if key not in self._instrument_tokens:
            resp = self.session.get(f"{self.BASE_URL}/instruments/{exchange}", timeout=30)
            resp.raise_for_status()
            lines = resp.text.splitlines()
            header = lines[0].split(",")
            tok_idx, sym_idx = header.index("instrument_token"), header.index("tradingsymbol")
            for line in lines[1:]:
                cols = line.split(",")
                if len(cols) > max(tok_idx, sym_idx):
                    self._instrument_tokens[f"{exchange}:{cols[sym_idx]}"] = int(cols[tok_idx])
        if key not in self._instrument_tokens:
            raise LookupError(f"Instrument {key} not found")
        return self._instrument_tokens[key]

    def historical_bars(self, symbol: str, exchange: str, interval: str, lookback: int) -> list[Bar]:
        token = self.instrument_token(symbol, exchange)
        to_dt = datetime.now()
        days = lookback * 2 if interval == "day" else max(5, lookback // 75 + 2)
        from_dt = to_dt - timedelta(days=days)
        data = self._get(
            f"/instruments/historical/{token}/{interval}",
            **{"from": from_dt.strftime("%Y-%m-%d %H:%M:%S"), "to": to_dt.strftime("%Y-%m-%d %H:%M:%S")},
        )
        return [_bar_from_row(row) for row in data["candles"]][-lookback:]

    def last_price(self, symbol: str, exchange: str) -> Decimal:
        key = f"{exchange}:{symbol}"
        data = self._get("/quote/ltp", i=key)
        return Decimal(str(data[key]["last_price"]))

    def portfolio(self) -> Portfolio:
        margins = self._get("/user/margins/equity")
        holdings = self._get("/portfolio/holdings")
        positions: dict[str, Position] = {}
        for h in holdings:
            qty = int(h["quantity"]) + int(h.get("t1_quantity", 0))
            if qty:
                positions[h["tradingsymbol"]] = Position(h["tradingsymbol"], qty, Decimal(str(h["average_price"])))
        return Portfolio(cash=Decimal(str(margins["available"]["live_balance"])), positions=positions)

    def place_order(self, order: OrderRequest) -> OrderResult:
        data = {
            "tradingsymbol": order.symbol,
            "exchange": order.exchange,
            "transaction_type": order.side,
            "order_type": order.order_type,
            "quantity": order.quantity,
            "product": order.product,
            "validity": "DAY",
            "tag": order.tag[:20],
        }
        if order.order_type == "LIMIT" and order.limit_price is not None:
            data["price"] = str(order.limit_price)
        result = self._post("/orders/regular", data)
        return OrderResult(str(result["order_id"]), "PLACED", order)

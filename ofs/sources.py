from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable
import time

import requests

from .engine import BidLevel


@dataclass(frozen=True)
class RawOFS:
    exchange: str
    symbol: str
    category: str
    offered_quantity: int
    floor_price: float | None
    bids: list[BidLevel]
    source_url: str
    fetched_at: float


def _request_json(url: str, *, headers: dict[str, str], timeout: float = 8.0) -> Any:
    response = requests.get(url, headers=headers, timeout=timeout)
    response.raise_for_status()
    return response.json()


def parse_nse_market_by_price(payload: Any, *, symbol: str, category: str = "GENERAL") -> list[BidLevel]:
    """Parse the documented NSE e-OFS market-by-price response."""
    rows = payload if isinstance(payload, list) else payload.get("data", payload.get("records", []))
    levels: list[BidLevel] = []
    for row in rows or []:
        if row.get("price") in (None, "", 0) or row.get("quantity") in (None, "", 0):
            continue
        levels.append(
            BidLevel(
                price=row["price"],
                quantity=int(row["quantity"]),
                exchange="NSE",
                category=category,
            )
        )
    return levels


def fetch_nse_market_by_price(
    symbol: str,
    *,
    series: str = "IS",
    base_url: str = "https://eofs.nseindia.com/api",
    category: str = "GENERAL",
) -> list[BidLevel]:
    url = f"{base_url.rstrip('/')}/query/marketByPrice"
    payload = _request_json(
        url,
        headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
    )
    # The deployed endpoint may require query parameters. Keep the public parser
    # separate so a broker/member-authenticated endpoint can be injected later.
    if isinstance(payload, dict) and payload.get("symbol") not in (None, symbol):
        return []
    return parse_nse_market_by_price(payload, symbol=symbol, category=category)


def parse_generic_bse_levels(payload: Any, *, category: str = "GENERAL") -> list[BidLevel]:
    """Best-effort parser for BSE's published OFS depth variants."""
    rows: list[dict[str, Any]] = []
    if isinstance(payload, list):
        rows = [r for r in payload if isinstance(r, dict)]
    elif isinstance(payload, dict):
        for key in ("Table", "table", "data", "Data", "records", "Record"):
            candidate = payload.get(key)
            if isinstance(candidate, list):
                rows = [r for r in candidate if isinstance(r, dict)]
                if rows:
                    break
    levels: list[BidLevel] = []
    for row in rows:
        price = next((row.get(k) for k in ("price", "Price", "BIDPRICE", "BidPrice", "bidPrice")), None)
        qty = next((row.get(k) for k in ("quantity", "Quantity", "BIDQTY", "BidQty", "bidQty")), None)
        if price in (None, "", 0) or qty in (None, "", 0):
            continue
        levels.append(BidLevel(price=price, quantity=int(qty), exchange="BSE", category=category))
    return levels


def fetch_bse_levels(url: str, *, category: str = "GENERAL") -> list[BidLevel]:
    payload = _request_json(
        url,
        headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json, text/plain, */*"},
    )
    return parse_generic_bse_levels(payload, category=category)


def retry(fn: Callable[[], Any], *, attempts: int = 3, delay: float = 0.8) -> Any:
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - CLI should retry transient exchange failures
            last = exc
            if attempt + 1 < attempts:
                time.sleep(delay * (2**attempt))
    raise RuntimeError("Exchange fetch failed after retries") from last

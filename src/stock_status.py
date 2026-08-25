#!/usr/bin/env python3
"""Fetch a simple daily stock status report.

The application uses Stooq's free CSV endpoint by default, so it can run
without an API key. Symbols are configurable through environment variables
or the command line.
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import sys
from dataclasses import dataclass
from datetime import date
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


DEFAULT_SYMBOLS = "RELIANCE.NS,TCS.NS,INFY.NS,HDFCBANK.NS,ICICIBANK.NS"
STOOQ_URL = "https://stooq.com/q/d/l/?s={symbol}&d1={start}&d2={end}&i=d"


@dataclass(frozen=True)
class Quote:
    symbol: str
    trading_date: str
    close: float
    previous_close: float | None

    @property
    def change(self) -> float | None:
        if self.previous_close in (None, 0):
            return None
        return self.close - self.previous_close

    @property
    def change_pct(self) -> float | None:
        if self.previous_close in (None, 0):
            return None
        return ((self.close / self.previous_close) - 1.0) * 100.0


def fetch_csv(symbol: str) -> str:
    today = date.today().isoformat()
    request = Request(
        STOOQ_URL.format(symbol=quote(symbol), start="2000-01-01", end=today),
        headers={"User-Agent": "StocksDailyStatus/1.0"},
    )
    try:
        with urlopen(request, timeout=20) as response:
            return response.read().decode("utf-8")
    except (HTTPError, URLError, TimeoutError) as exc:
        raise RuntimeError(f"Unable to fetch {symbol}: {exc}") from exc


def parse_quote(symbol: str, csv_text: str) -> Quote:
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    rows = [row for row in rows if row.get("Close") not in (None, "", "N/D")]
    if not rows:
        raise ValueError(f"No quote data returned for {symbol}")

    latest = rows[-1]
    previous = rows[-2] if len(rows) > 1 else None
    return Quote(
        symbol=symbol,
        trading_date=latest["Date"],
        close=float(latest["Close"]),
        previous_close=float(previous["Close"]) if previous else None,
    )


def get_quotes(symbols: Iterable[str]) -> list[Quote]:
    quotes: list[Quote] = []
    errors: list[str] = []
    for symbol in symbols:
        normalized = symbol.strip().upper()
        if not normalized:
            continue
        try:
            quotes.append(parse_quote(normalized, fetch_csv(normalized)))
        except (RuntimeError, ValueError) as exc:
            errors.append(str(exc))

    if errors and not quotes:
        raise RuntimeError("No market data could be loaded:\n- " + "\n- ".join(errors))
    return quotes


def render_report(quotes: Iterable[Quote]) -> str:
    lines = [
        "Stocks Daily Status",
        "===================",
        "Symbol        Date          Close      Change     Change %",
        "------------  ----------  ---------  ---------  ---------",
    ]
    for quote in quotes:
        change = "N/A" if quote.change is None else f"{quote.change:>9.2f}"
        pct = "N/A" if quote.change_pct is None else f"{quote.change_pct:>8.2f}%"
        lines.append(
            f"{quote.symbol:<12}  {quote.trading_date:<10}  "
            f"{quote.close:>9.2f}  {change}  {pct}"
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a daily stock status report.")
    parser.add_argument(
        "--symbols",
        default=os.getenv("STOCK_SYMBOLS", DEFAULT_SYMBOLS),
        help="Comma-separated symbols, e.g. AAPL,MSFT",
    )
    args = parser.parse_args()

    try:
        quotes = get_quotes(args.symbols.split(","))
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(render_report(quotes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

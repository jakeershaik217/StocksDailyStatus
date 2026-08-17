# StocksDailyStatus

A lightweight daily stock-status reporter for watchlists.

## What it does

- Fetches daily historical prices from Stooq's public CSV endpoint.
- Calculates the latest close, previous close, absolute change, and percentage change.
- Works without an API key.
- Accepts a custom comma-separated watchlist.
- Returns a non-zero exit code when no market data can be loaded.

## Requirements

- Python 3.11+
- Internet access

No third-party Python package is required by the current implementation.

## Run

```bash
python run.py
```

Use a custom watchlist:

```bash
python run.py --symbols AAPL,MSFT,NVDA
```

Or set the `STOCK_SYMBOLS` environment variable:

```bash
STOCK_SYMBOLS=AAPL,MSFT python run.py
```

## Test

```bash
python -m unittest discover -s tests -v
```

## Data source

The current implementation uses Stooq's daily CSV endpoint. Treat the output as market-data reporting, not investment advice. Verify prices against your preferred broker/exchange source before making trading decisions.

## Roadmap

- Add a configurable portfolio/watchlist file.
- Add structured CSV/JSON output.
- Add scheduled daily execution through GitHub Actions.
- Add optional email notification without storing credentials in source control.

# Live OFS Cutoff Analyzer

This module estimates the live OFS cutoff by combining visible bid levels and sorting demand from the highest bid price downward until the category's offered quantity is saturated.

## Important market-structure notes

- NSE defines the OFS cut-off price as the lowest price at which an investor is allocated shares.
- NSE says the indicative price is a VWAP of valid/confirmed bids and that demand by price point is displayed during the OFS.
- BSE says its OFS page displays BSE bid price/quantity in real time and that indicative price is consolidated from both exchanges.
- The estimated cutoff from a live order book is not a guaranteed allotment price. The final price/quantity is determined after the offer closes and allocation methodology is applied.

## Local run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pytest -q
```

Example calculation:

```bash
python -m ofs.cli \
  --offered 1000 \
  --category GENERAL \
  --bid NSE:GENERAL:105:200 \
  --bid BSE:GENERAL:104:300 \
  --bid NSE:GENERAL:103:600
```

## GitHub Actions

Go to **Actions → OFS Cutoff Analyzer → Run workflow**, enter the symbol, offered quantity, and category.

For a production deployment, configure the exchange endpoint/credential layer appropriate to your member/broker access. NSE's e-OFS protocol documents a Market By Price endpoint for authenticated member connectivity; a public website scrape should not be treated as an equivalent guaranteed feed.

## Recommended output

The live workflow should show:

1. Highest-to-lowest bid ladder from NSE and BSE.
2. Combined demand at each price.
3. Cumulative quantity.
4. Estimated saturation/cutoff price.
5. A conservative working-bid suggestion based on the tick size.
6. A warning when visible demand is below supply or when only one exchange is available.

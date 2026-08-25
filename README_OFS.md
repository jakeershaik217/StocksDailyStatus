# Live OFS cutoff analyzer

This project estimates the **non-retail marginal allocation price**: the lowest
price level at which cumulative eligible demand consumes the available shares.
It does not substitute NSE's indicative price or VWAP for that cutoff.

## What the workflow now does

1. Reads the issue date, tick size, allocation method, and exchange list from
   [NSE OFS issue details](https://www.nseindia.com/market-data/ofs-information).
2. Reads NSE's non-retail summary and exact-price ladder from the official NSE
   public OFS endpoints.
3. Reads BSE's exact-price non-retail ladder from BSE's public OFS API using the
   supplied numeric BSE scrip code.
4. Adds the **incremental quantity at each price** across NSE and BSE, sorts
   prices from high to low, and finds the first level where cumulative demand
   reaches the selected offer quantity.
5. Publishes a cutoff only if the NSE and BSE exact-price totals exactly
   reconcile to NSE's published control total and the live sources are fresh.

This last check is intentional. When demand has reached supply, an NSE-only or
asynchronous ladder can produce a believable but wrong allocation boundary.

## Inputs to provide

Go to **Actions → OFS Cutoff Analyzer → Run workflow** and enter:

| Input | HINDCOPPER value | Why it is needed |
|---|---|---|
| NSE symbol | `HINDCOPPER` | Selects the NSE issue and ladder |
| BSE scrip code | `513599` | Selects the matching BSE ladder |
| Non-retail offer date | `25-Aug-2026` | Selects the T-day book; it can also be discovered from NSE |
| NSE series | `IS` | NSE non-retail OFS series |
| Final offer quantity override | leave blank intraday | Use only after an exercised green-shoe/final quantity is officially known |

The workflow automatically uses NSE's **Total Issue Size** when NSE publishes
it. Until then it evaluates the base non-retail offer and labels the result
provisional. Do not guess the green-shoe quantity.

## Interpreting the result

- `NO_CUTOFF`: fresh consolidated demand is below the available quantity. No
  allocation-stopping price exists yet. The floor is the current minimum
  eligible bid, not a computed cutoff.
- `ESTIMATED`: both price ladders are present, fresh, and exactly reconciled.
  The report shows the marginal cutoff, residual shares at that price, and a
  one-tick-above working price. That working price improves current price
  priority but never guarantees allotment.
- `UNSAFE`: supply is saturated but an exchange is missing, timestamps are too
  far apart, or totals do not reconcile. The workflow fails rather than publish
  an NSE-only guess.

At the cutoff itself, allocation can be partial or proportionate under the
offer terms. A bid below it is outside the current successful price range; a bid
above it has price priority but can still be affected by later bids and the
final offer size.

## Local use

```bash
pip install -r requirements.txt
OFS_SYMBOL=HINDCOPPER \
OFS_SERIES=IS \
OFS_OFFER_DATE=25-Aug-2026 \
BSE_SCRIP_CODE=513599 \
python scripts/live_ofs.py
```

The command writes `ofs-report.md` and `ofs-live-snapshot.json`. The JSON keeps
the raw exchange payloads and all reconciliation checks so a result can be
audited later.

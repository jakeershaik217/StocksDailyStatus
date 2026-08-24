# Live OFS Cutoff Analyzer

This module is designed for the **T-day final non-retail/HNI cutoff**, while still supporting intraday estimates.

## Correct final-day methodology

NSE defines OFS cut-off price as the lowest price at which an investor is allocated shares. The exchange also states that, after OFS closure, allocation follows the offer's stated methodology such as proportionate basis or price-time priority. citeturn438652search0

For the common non-retail **price-priority / multiple-clearing-price** methodology, the engine:

1. Collects all valid T-day non-retail bids from NSE and BSE.
2. Combines NSE + BSE quantities at the same price.
3. Sorts price levels from highest to lowest.
4. Uses the **final quantity actually available for non-retail allocation**. This must include any exercised oversubscription option disclosed after market close.
5. Walks down the price ladder until cumulative demand reaches that final quantity.
6. Identifies that price as the estimated/final non-retail cutoff.
7. Separately reports the residual quantity available at the cutoff price, because the cutoff level can receive only a partial allocation.

The seller's notice can also specify allocation methodology, and recent NSE OFS notices state that non-retail allocation is at the Cut-Off Price or higher as per bids. citeturn438652search12

### Example

```text
₹520   8,00,000
₹519   7,00,000
₹518   9,00,000
₹517   6,00,000
₹516   12,00,000

Final non-retail shares available = 30,00,000

Demand > ₹517 = 30,00,000
Therefore cutoff = ₹517

At ₹517 itself:
required = 6,00,000
requested at ₹517 = 6,00,000
```

If the cumulative quantity above the cutoff is 28,00,000 and the cutoff level contains 6,00,000 shares, only 2,00,000 of that cutoff level is needed. The remaining requests at ₹517 are subject to the applicable allocation rule rather than being treated as fully allotted.

## Important NSE/BSE distinction

The cutoff is a **category-level clearing result**, not an exchange-by-exchange cutoff. Where an OFS is conducted through both NSE and BSE, the engine must use the valid bids considered across the exchanges as specified by the offer terms. NSE's current OFS page provides separate General and Retail reporting, while BSE's operating guidelines describe cross-exchange/category treatment. citeturn438652search1turn123119search16

The exchange website's intraday indicative price is not the final cutoff. Do not use VWAP/indicative price as a substitute for the cutoff calculation.

## GitHub Actions

Go to **Actions → OFS Cutoff Analyzer → Run workflow**. The workflow now asks for:

- NSE symbol
- Final non-retail offer quantity
- Category (`NON_RETAIL`, `GENERAL`, or `HNI`)
- NSE market-by-price endpoint
- BSE OFS depth endpoint
- Optional final consolidated bid JSON fallback

The **final offer quantity** must reflect the quantity actually available to non-retail investors after any oversubscription option is exercised. Recent OFS notices explicitly state that a seller may exercise an oversubscription option and that final allocation is then based on the resulting offer size. citeturn438652search12

## Pre-close use

Before the OFS closes, the same engine can be run against the latest NSE/BSE book to produce a moving cutoff estimate. That number is only an estimate because bids can change until the window closes.

## Safety / accuracy

A public-web scrape is not guaranteed to be a complete authoritative trading feed. NSE documents e-OFS API access for members and publishes market/OFS reporting separately. The analyzer therefore reports missing-source warnings rather than fabricating a cutoff when one or both feeds are unavailable. citeturn438652search0turn123119search1

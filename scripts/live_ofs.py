from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from ofs.engine import BidLevel, calculate_non_retail_cutoff, _dec, _int
from ofs.sources import fetch_bse_levels, fetch_nse_market_by_price, retry


def main() -> int:
    symbol = os.environ["OFS_SYMBOL"].upper().strip()
    offered = int(os.environ["OFS_OFFERED"])
    category = os.getenv("OFS_CATEGORY", "NON_RETAIL").upper()
    nse_url = os.getenv("NSE_MBP_URL", "").strip()
    bse_url = os.getenv("BSE_DEPTH_URL", "").strip()
    final_json = os.getenv("OFS_FINAL_BID_JSON", "").strip()

    levels: list[BidLevel] = []
    errors: list[str] = []

    if final_json:
        try:
            rows = json.loads(final_json)
            for row in rows:
                levels.append(BidLevel(price=_dec(row["price"]), quantity=_int(row["quantity"]), exchange=str(row.get("exchange", "CONSOLIDATED")).upper(), category=category))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"FINAL_BID_JSON: {exc}")

    if nse_url:
        try:
            levels.extend(retry(lambda: fetch_nse_market_by_price(symbol, endpoint_url=nse_url, category=category)))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"NSE: {exc}")

    if bse_url:
        try:
            levels.extend(retry(lambda: fetch_bse_levels(bse_url, category=category)))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"BSE: {exc}")

    print(f"OFS FINAL CUTOFF ANALYZER | {symbol} | {category} | {datetime.now(timezone.utc).isoformat()}")
    print("Method: T-day final valid bids, combined across NSE/BSE, price priority")
    print(f"Bid levels received: {len(levels)}")

    if not levels:
        print("ERROR: No final bid levels were retrieved.")
        for err in errors:
            print(f"  {err}")
        return 1

    for level in sorted(levels, key=lambda x: _dec(x.price), reverse=True):
        print(f"{level.exchange:12} ₹{level.price} {level.quantity:,}")

    result = calculate_non_retail_cutoff(levels, offered, category=category)
    print(f"Final offer quantity: {result.offered_quantity:,}")
    print(f"Demand above cutoff: {result.cumulative_quantity_above_cutoff:,}")
    print(f"Demand at cutoff price: {result.quantity_at_cutoff_price:,}")
    print(f"Quantity needed at cutoff: {result.quantity_needed_at_cutoff:,}")

    if result.cutoff_price is None:
        print("CUTOFF: NOT OBSERVABLE — final valid demand is below the final offer quantity.")
        return 2

    print(f"Demand at/above cutoff: {result.cumulative_quantity_at_cutoff:,}")
    print(f"Saturation ratio: {result.saturation_ratio:.4f}")
    print(f"FINAL ESTIMATED NON-RETAIL CUTOFF: ₹{result.cutoff_price}")
    print("ALLOCATION NOTE: bids above the cutoff receive priority; the cutoff level may receive only the residual quantity and can therefore be partially/proportionately allocated under the applicable OFS terms.")
    print("WORKING BID: for a pre-close estimate, bidding at/above the estimated cutoff may improve price eligibility; it does not guarantee allotment.")
    if errors:
        print("WARNINGS:")
        for err in errors:
            print(f"  {err}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

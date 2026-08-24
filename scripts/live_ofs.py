from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

from ofs.engine import BidLevel, estimate_cutoff
from ofs.sources import fetch_bse_levels, fetch_nse_market_by_price, retry


def main() -> int:
    symbol = os.environ["OFS_SYMBOL"].upper().strip()
    offered = int(os.environ["OFS_OFFERED"])
    category = os.getenv("OFS_CATEGORY", "GENERAL").upper()
    nse_url = os.getenv("NSE_MBP_URL", "").strip()
    bse_url = os.getenv("BSE_DEPTH_URL", "").strip()

    levels: list[BidLevel] = []
    errors: list[str] = []

    if nse_url:
        try:
            levels.extend(retry(lambda: fetch_nse_market_by_price(symbol, category=category)))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"NSE: {exc}")

    if bse_url:
        try:
            levels.extend(retry(lambda: fetch_bse_levels(bse_url, category=category)))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"BSE: {exc}")

    print(f"OFS Cutoff Analyzer | {symbol} | {category} | {datetime.now(timezone.utc).isoformat()}")
    print(f"Visible bid levels: {len(levels)}")
    for level in sorted(levels, key=lambda x: x.price, reverse=True):
        print(f"{level.exchange:3} {level.category:8} ₹{level.price} {level.quantity:,}")

    if not levels:
        print("ERROR: No bid levels were retrieved. Configure exchange endpoints/credentials.")
        for err in errors:
            print(f"  {err}")
        return 1

    result = estimate_cutoff(levels, offered, category=category)
    print(f"Offered quantity: {result.offered_quantity:,}")
    print(f"Cumulative at estimated cutoff: {result.cumulative_quantity_at_cutoff:,}")
    print(f"Saturation ratio: {result.saturation_ratio}")

    if result.cutoff_price is None:
        print("CUTOFF: NOT YET OBSERVABLE — visible demand is below supply.")
        return 2

    print(f"ESTIMATED CUTOFF: ₹{result.cutoff_price}")
    print("WORKING BID: cutoff or one valid tick above can improve price-priority, but it does not guarantee allotment.")
    if errors:
        print("WARNINGS:")
        for err in errors:
            print(f"  {err}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

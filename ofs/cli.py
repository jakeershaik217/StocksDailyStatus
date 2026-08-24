from __future__ import annotations

import argparse
import json
from decimal import Decimal
from typing import Any

from .engine import BidLevel, estimate_cutoff


def _parse_bid(value: str) -> BidLevel:
    # exchange:category:price:quantity
    parts = value.split(":")
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("Bid must be EXCHANGE:CATEGORY:PRICE:QUANTITY")
    exchange, category, price, qty = parts
    return BidLevel(Decimal(price), int(qty), exchange.upper(), category.upper())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Estimate OFS cutoff from a visible bid ladder")
    parser.add_argument("--offered", required=True, type=int, help="Shares offered in the relevant category")
    parser.add_argument("--category", default="GENERAL")
    parser.add_argument("--bid", action="append", type=_parse_bid, required=True,
                        help="EXCHANGE:CATEGORY:PRICE:QUANTITY; repeat for each visible level")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    result = estimate_cutoff(args.bid, args.offered, category=args.category)
    print(json.dumps(result.to_dict(), indent=2))
    if result.cutoff_price is None:
        print("WARNING: visible demand does not reach offered supply; cutoff is not observable yet.")
        return 2
    print(f"ESTIMATED CUTOFF: ₹{result.cutoff_price}")
    print(f"Suggested working bid: ₹{result.cutoff_price} or one valid tick above, subject to broker/exchange rules.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

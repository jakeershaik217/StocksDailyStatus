from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from ofs.retail import assess_retail
from ofs.sources import fetch_nse_issue, fetch_nse_market_by_price, parse_bse_ladder, retry

BSE_URL = "https://api.bseindia.com/BseIndiaAPI/api/bsebidofs_details/w"


def _positive_int(value: str | None) -> int | None:
    if not value or not value.strip():
        return None
    parsed = int(value.replace(",", "").strip())
    if parsed <= 0:
        raise ValueError("quantity must be positive")
    return parsed


def _fetch_bse_retail(scrip: str, symbol: str):
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/151 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.bseindia.com/markets/PublicIssues/OFSIssuse_new?expandable=0",
        "Origin": "https://www.bseindia.com",
    }
    params = {"scripcode": scrip, "strflag": "R"}
    response = requests.get(BSE_URL, headers=headers, params=params, timeout=25)
    response.raise_for_status()
    return parse_bse_ladder(response.json(), symbol=symbol, category="RETAIL", source_url=response.url)


def main() -> int:
    symbol = os.getenv("OFS_SYMBOL", "").upper().strip()
    if not symbol:
        print("ERROR: OFS_SYMBOL is required", file=sys.stderr)
        return 1

    offer_date = os.getenv("OFS_OFFER_DATE", "").strip() or None
    bse_scrip = os.getenv("BSE_SCRIP_CODE", "").strip()
    reference_text = os.getenv("OFS_REFERENCE_CUTOFF", "").strip()
    if not reference_text:
        print("ERROR: RETAIL mode requires the confirmed T-day non-retail cutoff in OFS_REFERENCE_CUTOFF", file=sys.stderr)
        print("Example for HINDCOPPER: OFS_REFERENCE_CUTOFF=520.00", file=sys.stderr)
        return 2

    reference_cutoff = Decimal(reference_text)
    issue = retry(lambda: fetch_nse_issue(symbol, series="IS"))
    resolved_date = offer_date or issue.offer_date
    if not resolved_date:
        print("ERROR: could not determine offer date", file=sys.stderr)
        return 1

    # NSE public ladder is reused with the category label switched to RETAIL.
    nse = retry(lambda: fetch_nse_market_by_price(symbol, offer_date=resolved_date, category="RETAIL"))
    bse = retry(lambda: _fetch_bse_retail(bse_scrip, symbol))
    bids = [*nse.bids, *bse.bids]

    final_offer = _positive_int(os.getenv("OFS_FINAL_OFFER_QUANTITY"))
    if final_offer is None:
        raise_value = "NSE issue details do not expose final green-shoe size; provide OFS_FINAL_OFFER_QUANTITY"
        # Base issue size is safer than inventing a final size.
        raise RuntimeError(raise_value)

    pct = Decimal(os.getenv("OFS_RETAIL_RESERVED_PCT", "10")) / Decimal("100")
    reserved = int(Decimal(final_offer) * pct)

    assessment = assess_retail(
        bids,
        reference_cutoff=reference_cutoff,
        reserved_quantity=reserved,
    )

    print(f"RETAIL OFS | {symbol} | {datetime.now(timezone.utc).isoformat()}")
    print(f"Confirmed T-day HNI cutoff: ₹{assessment.reference_cutoff}")
    print(f"Retail reserved quantity (configured {pct * 100}%): {assessment.reserved_quantity:,}")
    print(f"Total retail visible demand: {assessment.total_demand:,}")
    print(f"Eligible retail demand at/above ₹{assessment.reference_cutoff}: {assessment.eligible_demand:,}")
    print(f"Retail subscription: {assessment.subscription:.4f}x")
    print(f"Estimated proportionate allocation ratio: {assessment.estimated_allocation_ratio:.4%}")
    print(f"WORKING BID: ₹{assessment.working_bid}")
    print("NOTE: Retail does not establish a new HNI-style cutoff; ₹520 is used only because it is the confirmed prior-day non-retail cutoff for this example.")

    Path("ofs-report.md").write_text(
        "\n".join([
            f"# Retail OFS — {symbol}",
            "",
            f"Confirmed T-day cutoff: **₹{assessment.reference_cutoff}**",
            f"Retail reserved quantity: **{assessment.reserved_quantity:,}**",
            f"Eligible retail demand: **{assessment.eligible_demand:,}**",
            f"Retail subscription: **{assessment.subscription:.4f}x**",
            f"Estimated proportionate allocation: **{assessment.estimated_allocation_ratio:.4%}**",
            f"Working bid: **₹{assessment.working_bid}**",
            "",
            "The working bid is based on the confirmed prior-day cutoff. It is not a newly discovered retail cutoff.",
        ]) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from ofs.retail import (
    RetailLadderSnapshot,
    assess_retail,
    derive_retail_quantity,
    parse_retail_ladder,
)
from ofs.sources import fetch_nse_issue, fetch_nse_summary, retry

NSE_RETAIL_URL = "https://www.nseindia.com/api/ofs-activeissues-dd"
BSE_RETAIL_URL = "https://api.bseindia.com/BseIndiaAPI/api/bsebidofs_details/w"


def _optional_positive_int(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    parsed = int(value.replace(",", "").strip())
    if parsed <= 0:
        raise ValueError("quantity inputs must be positive")
    return parsed


def _headers(*, referer: str) -> dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-IN,en;q=0.9",
        "Referer": referer,
    }


def _request_json(
    url: str,
    *,
    params: dict[str, str],
    headers: dict[str, str],
    use_browser_session: bool = False,
) -> tuple[Any, str]:
    session: Any = requests
    owned_session = None
    if use_browser_session:
        try:
            from curl_cffi import requests as curl_requests

            owned_session = curl_requests.Session(impersonate="chrome")
            session = owned_session
        except ImportError:
            pass
    try:
        response = session.get(url, params=params, headers=headers, timeout=25)
        response.raise_for_status()
        if response.text.lstrip().startswith("<"):
            raise ValueError(f"Expected JSON but received HTML from {response.url}")
        return response.json(), response.url
    finally:
        if owned_session is not None:
            owned_session.close()


def _fetch_nse_retail(
    symbol: str,
    retail_date: str,
) -> RetailLadderSnapshot:
    page_url = "https://www.nseindia.com/market-data/ofs-information?" + urlencode(
        {
            "symbol": symbol,
            "series": "RS",
            "type": "Active",
            "offerDate": retail_date,
        }
    )
    payload, source_url = _request_json(
        NSE_RETAIL_URL,
        params={"symbol": symbol, "offerdate": retail_date},
        headers=_headers(referer=page_url),
    )
    return parse_retail_ladder(
        payload,
        exchange="NSE",
        symbol=symbol,
        source_url=source_url,
        expected_series="RS",
    )


def _fetch_bse_retail(
    scrip_code: str,
    symbol: str,
) -> RetailLadderSnapshot:
    headers = _headers(
        referer=(
            "https://www.bseindia.com/markets/PublicIssues/"
            "OFSIssuse_new?expandable=0"
        )
    )
    headers["Origin"] = "https://www.bseindia.com"
    payload, source_url = _request_json(
        BSE_RETAIL_URL,
        params={"scripcode": scrip_code, "strflag": "R"},
        headers=headers,
        use_browser_session=True,
    )
    return parse_retail_ladder(
        payload,
        exchange="BSE",
        symbol=symbol,
        source_url=source_url,
    )


def _build_report(
    *,
    symbol: str,
    retrieved_at: datetime,
    reference_basis: str,
    quantity_basis: str,
    nse: RetailLadderSnapshot,
    bse: RetailLadderSnapshot,
    assessment: Any,
) -> str:
    return "\n".join(
        [
            f"# Retail OFS assessment — {symbol}",
            "",
            f"Retrieved: `{retrieved_at.isoformat()}`",
            "",
            "| Metric | Value |",
            "|---|---:|",
            f"| Confirmed T-day non-retail cutoff | ₹{assessment.reference_cutoff} |",
            f"| Cutoff basis | {reference_basis} |",
            f"| Retail reserved quantity | {assessment.reserved_quantity:,} |",
            f"| Quantity basis | {quantity_basis} |",
            f"| NSE numeric-price demand | {nse.numeric_bid_quantity:,} |",
            f"| NSE Cut-off-order demand | {nse.cutoff_bid_quantity:,} |",
            f"| BSE numeric-price demand | {bse.numeric_bid_quantity:,} |",
            f"| BSE Cut-off-order demand | {bse.cutoff_bid_quantity:,} |",
            f"| Total visible retail demand | {assessment.total_demand:,} |",
            f"| Eligible numeric-price demand | {assessment.eligible_numeric_demand:,} |",
            f"| Eligible Cut-off-order demand | {assessment.cutoff_bid_quantity:,} |",
            f"| Total eligible retail demand | {assessment.eligible_demand:,} |",
            f"| Retail subscription | {assessment.subscription:.4f}x |",
            (
                "| Estimated proportionate allocation | "
                f"{assessment.estimated_allocation_ratio:.4%} |"
            ),
            "",
            "## Decision",
            "",
            f"Working bid/reference price: **₹{assessment.working_bid}**.",
            "",
            (
                "Literal Cut-off orders from both exchanges are counted as eligible. "
                "This is an allocation estimate, not a newly discovered retail cutoff; "
                "final allocation remains exchange-controlled."
            ),
        ]
    ) + "\n"


def main() -> int:
    symbol = os.getenv("OFS_SYMBOL", "").upper().strip()
    bse_scrip = os.getenv("BSE_SCRIP_CODE", "").strip()
    offer_date = os.getenv("OFS_OFFER_DATE", "").strip() or None
    if not symbol or not bse_scrip:
        print("ERROR: OFS_SYMBOL and BSE_SCRIP_CODE are required", file=sys.stderr)
        return 1

    retrieved_at = datetime.now(timezone.utc)
    try:
        issue = retry(lambda: fetch_nse_issue(symbol, series="IS"))
        retail_issue = retry(lambda: fetch_nse_issue(symbol, series="RS"))
        resolved_date = offer_date or issue.offer_date
        if not resolved_date:
            raise RuntimeError("OFS offer date could not be determined")
        retail_date = (
            os.getenv("OFS_RETAIL_DATE", "").strip()
            or retail_issue.offer_date
        )
        if not retail_date:
            raise RuntimeError("Retail OFS session date could not be determined")

        final_non_retail = retry(
            lambda: fetch_nse_summary(
                symbol,
                offer_date=resolved_date,
                series="IS",
            )
        )
        reference_text = os.getenv("OFS_REFERENCE_CUTOFF", "").strip()
        if reference_text:
            reference_cutoff = Decimal(reference_text)
            reference_basis = "USER_CONFIRMED_OVERRIDE"
        elif final_non_retail.cutoff_price is not None:
            reference_cutoff = final_non_retail.cutoff_price
            reference_basis = "NSE_FINAL_NON_RETAIL_CUTOFF"
        else:
            raise RuntimeError(
                "NSE has not published the final non-retail cutoff; provide "
                "OFS_REFERENCE_CUTOFF only from a confirmed exchange result"
            )

        gross_final = _optional_positive_int(
            os.getenv("OFS_GROSS_FINAL_OFFER_QUANTITY")
        )
        explicit_retail = _optional_positive_int(
            os.getenv("OFS_RETAIL_RESERVED_QUANTITY")
        )
        reserved_pct = Decimal(
            os.getenv("OFS_RETAIL_RESERVED_PCT", "10")
        ) / Decimal("100")
        reserved_quantity, quantity_basis = derive_retail_quantity(
            gross_final_offer_quantity=gross_final,
            final_non_retail_quantity=final_non_retail.total_offer_quantity,
            reserved_percentage=reserved_pct,
            explicit_retail_quantity=explicit_retail,
        )

        with ThreadPoolExecutor(max_workers=2) as executor:
            nse_future = executor.submit(
                retry,
                lambda: _fetch_nse_retail(symbol, retail_date),
            )
            bse_future = executor.submit(
                retry,
                lambda: _fetch_bse_retail(bse_scrip, symbol),
            )
            nse = nse_future.result()
            bse = bse_future.result()

        bids = (*nse.bids, *bse.bids)
        cutoff_bid_quantity = nse.cutoff_bid_quantity + bse.cutoff_bid_quantity
        assessment = assess_retail(
            bids,
            reference_cutoff=reference_cutoff,
            reserved_quantity=reserved_quantity,
            cutoff_bid_quantity=cutoff_bid_quantity,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: unable to build retail OFS assessment: {exc}", file=sys.stderr)
        return 1

    report = _build_report(
        symbol=symbol,
        retrieved_at=retrieved_at,
        reference_basis=reference_basis,
        quantity_basis=quantity_basis,
        nse=nse,
        bse=bse,
        assessment=assessment,
    )
    print(report)
    Path(os.getenv("OFS_REPORT_PATH", "ofs-report.md")).write_text(
        report,
        encoding="utf-8",
    )
    snapshot = {
        "retrieved_at": retrieved_at.isoformat(),
        "input": {
            "symbol": symbol,
            "offer_date": resolved_date,
            "retail_date": retail_date,
            "bse_scrip_code": bse_scrip,
            "gross_final_offer_quantity": gross_final,
            "explicit_retail_quantity": explicit_retail,
        },
        "reference_cutoff": str(reference_cutoff),
        "reference_basis": reference_basis,
        "final_non_retail_summary": final_non_retail.raw_payload,
        "retail_reserved_quantity": reserved_quantity,
        "quantity_basis": quantity_basis,
        "nse_retail": {
            "source_url": nse.source_url,
            "as_of": nse.as_of.isoformat() if nse.as_of else None,
            "numeric_bid_quantity": nse.numeric_bid_quantity,
            "cutoff_bid_quantity": nse.cutoff_bid_quantity,
            "raw": nse.raw_payload,
        },
        "bse_retail": {
            "source_url": bse.source_url,
            "as_of": bse.as_of.isoformat() if bse.as_of else None,
            "numeric_bid_quantity": bse.numeric_bid_quantity,
            "cutoff_bid_quantity": bse.cutoff_bid_quantity,
            "raw": bse.raw_payload,
        },
        "assessment": {
            "eligible_demand": assessment.eligible_demand,
            "total_demand": assessment.total_demand,
            "subscription": str(assessment.subscription),
            "estimated_allocation_ratio": str(
                assessment.estimated_allocation_ratio
            ),
        },
    }
    Path(os.getenv("OFS_SNAPSHOT_PATH", "ofs-live-snapshot.json")).write_text(
        json.dumps(snapshot, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    github_summary = os.getenv("GITHUB_STEP_SUMMARY")
    if github_summary:
        with Path(github_summary).open("a", encoding="utf-8") as stream:
            stream.write(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

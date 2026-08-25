from __future__ import annotations

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ofs.live import LiveCutoffAssessment, assess_live_cutoff
from ofs.sources import (
    LadderSnapshot,
    OFSIssue,
    OFSSummary,
    fetch_bse_levels,
    fetch_nse_issue,
    fetch_nse_market_by_price,
    fetch_nse_summary,
    parse_bse_ladder,
    parse_nse_issue,
    parse_nse_ladder,
    parse_nse_summary,
    retry,
)


def _optional_positive_int(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    parsed = int(value.replace(",", "").strip())
    if parsed <= 0:
        raise ValueError("Quantity inputs must be positive")
    return parsed


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _level_rows(snapshot: LadderSnapshot | None) -> list[dict[str, Any]] | None:
    if snapshot is None:
        return None
    return [
        {
            "price": str(level.price),
            "quantity": level.quantity,
            "exchange": level.exchange,
            "category": level.category,
        }
        for level in snapshot.bids
    ]


def _assessment_dict(assessment: LiveCutoffAssessment) -> dict[str, Any]:
    return {
        "status": assessment.status,
        "message": assessment.message,
        "offer_basis": assessment.offer_basis,
        "offer_quantity": assessment.offer_quantity,
        "base_offer_quantity": assessment.base_offer_quantity,
        "total_offer_quantity": assessment.total_offer_quantity,
        "summary_total_demand": assessment.summary_total_demand,
        "nse_ladder_demand": assessment.nse_ladder_demand,
        "bse_ladder_demand": assessment.bse_ladder_demand,
        "combined_ladder_demand": assessment.combined_ladder_demand,
        "reconciliation_gap": assessment.reconciliation_gap,
        "cutoff": assessment.cutoff.to_dict() if assessment.cutoff else None,
        "working_bid": str(assessment.working_bid) if assessment.working_bid else None,
        "warnings": list(assessment.warnings),
        "errors": list(assessment.errors),
    }


def _load_fixture(
    path: Path,
    *,
    symbol: str,
    series: str,
    category: str,
    offer_date: str | None,
) -> tuple[OFSIssue, OFSSummary, LadderSnapshot, LadderSnapshot]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    issue = parse_nse_issue(payload["issue"], symbol=symbol, series=series)
    resolved_date = offer_date or issue.offer_date
    if not resolved_date:
        raise ValueError("Fixture does not contain an OFS offer date")
    summary = parse_nse_summary(
        payload["summary"],
        symbol=symbol,
        series=series,
        offer_date=resolved_date,
        source_url="fixture:nse-summary",
    )
    fetched_at = summary.as_of or datetime.now(timezone.utc)
    nse = parse_nse_ladder(
        payload["nse_ladder"],
        symbol=symbol,
        category=category,
        source_url="fixture:nse-ladder",
        fetched_at=fetched_at,
    )
    bse = parse_bse_ladder(
        payload["bse_ladder"],
        symbol=symbol,
        category=category,
        source_url="fixture:bse-ladder",
        fetched_at=fetched_at,
    )
    return issue, summary, nse, bse


def _fetch_live(
    *,
    symbol: str,
    series: str,
    category: str,
    offer_date: str | None,
    bse_scrip_code: str | None,
) -> tuple[
    OFSIssue | None,
    OFSSummary,
    LadderSnapshot | None,
    LadderSnapshot | None,
    list[str],
]:
    source_errors: list[str] = []
    issue: OFSIssue | None = None
    try:
        issue = retry(lambda: fetch_nse_issue(symbol, series=series))
    except Exception as exc:  # noqa: BLE001
        source_errors.append(f"NSE issue details: {exc}")

    resolved_date = offer_date or (issue.offer_date if issue else None)
    if not resolved_date:
        raise RuntimeError(
            "Offer date could not be discovered. Set OFS_OFFER_DATE, for example "
            "25-Aug-2026."
        )

    with ThreadPoolExecutor(max_workers=3) as executor:
        summary_future = executor.submit(
            retry,
            lambda: fetch_nse_summary(
                symbol,
                offer_date=resolved_date,
                series=series,
            ),
        )
        nse_future = executor.submit(
            retry,
            lambda: fetch_nse_market_by_price(
                symbol,
                offer_date=resolved_date,
                category=category,
            ),
        )
        bse_future = (
            executor.submit(
                retry,
                lambda: fetch_bse_levels(
                    bse_scrip_code or "",
                    symbol=symbol,
                    category=category,
                ),
            )
            if bse_scrip_code
            else None
        )

        # The consolidated summary is mandatory: it is the control total that
        # proves both whether supply is saturated and whether ladders are complete.
        summary = summary_future.result()

        try:
            nse = nse_future.result()
        except Exception as exc:  # noqa: BLE001
            nse = None
            source_errors.append(f"NSE price ladder: {exc}")

        if bse_future is None:
            bse = None
            source_errors.append("BSE price ladder: BSE scrip code was not provided")
        else:
            try:
                bse = bse_future.result()
            except Exception as exc:  # noqa: BLE001
                bse = None
                source_errors.append(f"BSE price ladder: {exc}")

    return issue, summary, nse, bse, source_errors


def _fmt_price(value: Decimal | None) -> str:
    return f"₹{value}" if value is not None else "not shown"


def _build_report(
    *,
    issue: OFSIssue | None,
    summary: OFSSummary,
    assessment: LiveCutoffAssessment,
    source_errors: list[str],
    retrieved_at: datetime,
) -> str:
    subscription = Decimal(summary.total_demand) / Decimal(assessment.offer_quantity)
    lines = [
        f"# OFS cutoff assessment — {summary.company_name} ({summary.symbol})",
        "",
        f"Retrieved: `{retrieved_at.isoformat()}`",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Status | **{assessment.status}** |",
        f"| NSE summary time | {summary.as_of.isoformat() if summary.as_of else 'not shown'} |",
        f"| Floor price | {_fmt_price(summary.floor_price)} |",
        f"| NSE indicative price | {_fmt_price(summary.indicative_price)} |",
        f"| LTP | {_fmt_price(summary.ltp)} |",
        f"| Selected offer quantity | {assessment.offer_quantity:,} |",
        f"| Offer basis | {assessment.offer_basis} |",
        f"| Consolidated demand | {summary.total_demand:,} |",
        f"| Subscription | {subscription:.4f}x |",
        f"| NSE exact-price demand | {assessment.nse_ladder_demand if assessment.nse_ladder_demand is not None else 'unavailable'} |",
        f"| BSE exact-price demand | {assessment.bse_ladder_demand if assessment.bse_ladder_demand is not None else 'unavailable'} |",
        f"| Reconciliation gap | {assessment.reconciliation_gap if assessment.reconciliation_gap is not None else 'not tested'} |",
        "",
        "## Decision",
        "",
        assessment.message,
    ]

    if assessment.status == "ESTIMATED" and assessment.cutoff:
        lines.extend(
            [
                "",
                f"**Estimated marginal non-retail cutoff: {_fmt_price(assessment.cutoff.cutoff_price)}**",
                "",
                (
                    f"One-tick-above working price: **{_fmt_price(assessment.working_bid)}**. "
                    "This only improves price priority against the current snapshot; it "
                    "does not guarantee allotment and can become obsolete as bids change."
                ),
                "",
                f"Demand above cutoff: {assessment.cutoff.cumulative_quantity_above_cutoff:,}",
                f"Demand at cutoff: {assessment.cutoff.quantity_at_cutoff_price:,}",
                f"Residual shares available at cutoff: {assessment.cutoff.quantity_needed_at_cutoff:,}",
            ]
        )
    elif assessment.status == "NO_CUTOFF":
        lines.extend(
            [
                "",
                "**Expected cutoff now: not formed.**",
                "",
                (
                    f"The floor price ({_fmt_price(summary.floor_price)}) is currently the "
                    "minimum eligible non-retail bid, but it is not a calculated cutoff. "
                    "The marginal price can rise if demand increases before the book closes."
                ),
            ]
        )
    else:
        lines.extend(
            [
                "",
                (
                    "**Cutoff withheld:** an NSE-only or unreconciled number could be on the "
                    "wrong side of the true cross-exchange allocation boundary."
                ),
            ]
        )

    warnings = [*assessment.warnings]
    if source_errors:
        warnings.extend(source_errors)
    if warnings:
        lines.extend(["", "## Warnings", ""])
        lines.extend(f"- {warning}" for warning in warnings)
    if assessment.errors:
        lines.extend(["", "## Blocking checks", ""])
        lines.extend(f"- {error}" for error in assessment.errors)

    lines.extend(
        [
            "",
            "## Method",
            "",
            (
                "Exact-price (not cumulative) quantities are aggregated across NSE and BSE, "
                "sorted from highest price to lowest, and walked until the selected offer "
                "quantity is consumed. The engine will not publish that price unless the "
                "combined ladder exactly matches the consolidated NSE control total."
            ),
        ]
    )
    if issue and issue.allocation_methodology:
        lines.append(f"NSE issue methodology: {issue.allocation_methodology}.")
    return "\n".join(lines) + "\n"


def main() -> int:
    symbol = os.getenv("OFS_SYMBOL", "").upper().strip()
    if not symbol:
        print("ERROR: OFS_SYMBOL is required", file=sys.stderr)
        return 1
    series = os.getenv("OFS_SERIES", "IS").upper().strip()
    category = os.getenv("OFS_CATEGORY", "NON_RETAIL").upper().strip()
    if category != "NON_RETAIL":
        print(
            "ERROR: This live cutoff workflow supports NON_RETAIL only", file=sys.stderr
        )
        return 1
    offer_date = os.getenv("OFS_OFFER_DATE", "").strip() or None
    bse_scrip_code = os.getenv("BSE_SCRIP_CODE", "").strip() or None
    fixture_path = os.getenv("OFS_SNAPSHOT_FILE", "").strip()
    offer_override = _optional_positive_int(os.getenv("OFS_FINAL_OFFER_QUANTITY"))
    max_age_minutes = float(os.getenv("OFS_MAX_LIVE_AGE_MINUTES", "15"))
    max_skew_minutes = float(os.getenv("OFS_MAX_SOURCE_SKEW_MINUTES", "10"))

    retrieved_at = datetime.now(timezone.utc)
    source_errors: list[str] = []
    try:
        if fixture_path:
            issue, summary, nse, bse = _load_fixture(
                Path(fixture_path),
                symbol=symbol,
                series=series,
                category=category,
                offer_date=offer_date,
            )
        else:
            issue, summary, nse, bse, source_errors = _fetch_live(
                symbol=symbol,
                series=series,
                category=category,
                offer_date=offer_date,
                bse_scrip_code=bse_scrip_code,
            )
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: unable to build OFS snapshot: {exc}", file=sys.stderr)
        return 1

    tick_size = issue.tick_size if issue else Decimal("0.05")
    assessment = assess_live_cutoff(
        summary,
        nse,
        bse,
        offer_override=offer_override,
        tick_size=tick_size,
        now=retrieved_at,
        max_live_age=timedelta(minutes=max_age_minutes),
        max_source_skew=timedelta(minutes=max_skew_minutes),
    )

    report = _build_report(
        issue=issue,
        summary=summary,
        assessment=assessment,
        source_errors=source_errors,
        retrieved_at=retrieved_at,
    )
    print(report)

    report_path = Path(os.getenv("OFS_REPORT_PATH", "ofs-report.md"))
    snapshot_path = Path(os.getenv("OFS_SNAPSHOT_PATH", "ofs-live-snapshot.json"))
    report_path.write_text(report, encoding="utf-8")
    snapshot = {
        "retrieved_at": retrieved_at.isoformat(),
        "input": {
            "symbol": symbol,
            "series": series,
            "category": category,
            "offer_date": offer_date or summary.offer_date,
            "bse_scrip_code": bse_scrip_code,
            "offer_override": offer_override,
        },
        "issue": issue.raw_payload if issue else None,
        "nse_summary": summary.raw_payload,
        "nse_ladder": {
            "as_of": _iso(nse.as_of) if nse else None,
            "fetched_at": _iso(nse.fetched_at) if nse else None,
            "source_url": nse.source_url if nse else None,
            "levels": _level_rows(nse),
            "raw": nse.raw_payload if nse else None,
        },
        "bse_ladder": {
            "as_of": _iso(bse.as_of) if bse else None,
            "fetched_at": _iso(bse.fetched_at) if bse else None,
            "source_url": bse.source_url if bse else None,
            "levels": _level_rows(bse),
            "raw": bse.raw_payload if bse else None,
        },
        "source_errors": source_errors,
        "assessment": _assessment_dict(assessment),
    }
    snapshot_path.write_text(
        json.dumps(snapshot, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    github_summary = os.getenv("GITHUB_STEP_SUMMARY")
    if github_summary:
        with Path(github_summary).open("a", encoding="utf-8") as stream:
            stream.write(report)

    return 1 if assessment.status == "UNSAFE" else 0


if __name__ == "__main__":
    raise SystemExit(main())

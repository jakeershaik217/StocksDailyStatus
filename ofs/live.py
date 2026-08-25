from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal

from .engine import CutoffEstimate, calculate_non_retail_cutoff
from .sources import IST, LadderSnapshot, OFSSummary


@dataclass(frozen=True)
class LiveCutoffAssessment:
    status: str
    message: str
    offer_basis: str
    offer_quantity: int
    base_offer_quantity: int
    total_offer_quantity: int | None
    summary_total_demand: int
    nse_ladder_demand: int | None
    bse_ladder_demand: int | None
    combined_ladder_demand: int | None
    reconciliation_gap: int | None
    cutoff: CutoffEstimate | None
    working_bid: Decimal | None
    warnings: tuple[str, ...]
    errors: tuple[str, ...]


def _is_live_session(summary: OFSSummary, now: datetime) -> bool:
    offer_day = summary.as_of.astimezone(IST).date() if summary.as_of else None
    current = now.astimezone(IST)
    return (
        offer_day == current.date()
        and time(9, 15) <= current.time() <= time(15, 40)
        and summary.status.upper() == "ACTIVE"
    )


def _age_error(
    label: str,
    timestamp: datetime | None,
    *,
    now: datetime,
    max_age: timedelta,
) -> str | None:
    if timestamp is None:
        return f"{label} does not expose an exchange timestamp"
    age = now.astimezone(timezone.utc) - timestamp.astimezone(timezone.utc)
    if age < timedelta(minutes=-1):
        return f"{label} timestamp is in the future ({timestamp.isoformat()})"
    if age > max_age:
        return f"{label} is stale by {age.total_seconds() / 60:.1f} minutes"
    return None


def assess_live_cutoff(
    summary: OFSSummary,
    nse_ladder: LadderSnapshot | None,
    bse_ladder: LadderSnapshot | None,
    *,
    offer_override: int | None = None,
    tick_size: Decimal = Decimal("0.05"),
    now: datetime | None = None,
    max_live_age: timedelta = timedelta(minutes=15),
    max_source_skew: timedelta = timedelta(minutes=10),
) -> LiveCutoffAssessment:
    """Assess whether a live cross-exchange cutoff can be stated safely.

    NSE's summary total is treated as the consolidated control total. A
    marginal cutoff is emitted only when the exact-price NSE and BSE ladders
    add to that control total. This deliberately fails closed: a plausible
    NSE-only price is not presented as a cross-exchange cutoff.
    """
    now = now or datetime.now(timezone.utc)
    warnings: list[str] = []
    errors: list[str] = []

    if offer_override is not None:
        if offer_override <= 0:
            raise ValueError("offer_override must be positive")
        offer_quantity = offer_override
        offer_basis = "USER_FINAL_OVERRIDE"
    elif summary.total_offer_quantity:
        offer_quantity = summary.total_offer_quantity
        offer_basis = "NSE_TOTAL_ISSUE"
    else:
        offer_quantity = summary.base_offer_quantity
        offer_basis = "NSE_BASE_ISSUE_PROVISIONAL"
        warnings.append(
            "The seller's total/green-shoe issue size is not yet shown; this is a "
            "base-offer scenario, not a guaranteed final allocation cutoff."
        )

    if _is_live_session(summary, now):
        stale_summary = _age_error(
            "NSE consolidated summary",
            summary.as_of,
            now=now,
            max_age=max_live_age,
        )
        if stale_summary:
            errors.append(stale_summary)

    if errors:
        return LiveCutoffAssessment(
            status="UNSAFE",
            message="Current total demand is not fresh enough to assess a live cutoff.",
            offer_basis=offer_basis,
            offer_quantity=offer_quantity,
            base_offer_quantity=summary.base_offer_quantity,
            total_offer_quantity=summary.total_offer_quantity,
            summary_total_demand=summary.total_demand,
            nse_ladder_demand=nse_ladder.total_quantity if nse_ladder else None,
            bse_ladder_demand=bse_ladder.total_quantity if bse_ladder else None,
            combined_ladder_demand=None,
            reconciliation_gap=None,
            cutoff=None,
            working_bid=None,
            warnings=tuple(warnings),
            errors=tuple(errors),
        )

    # A fresh consolidated total below supply proves that no marginal cutoff
    # exists yet. Missing exchange-level detail does not change that conclusion.
    if summary.total_demand < offer_quantity:
        if bse_ladder is None:
            warnings.append(
                "BSE price detail was unavailable, but the fresh consolidated total is "
                "below supply, so a marginal cutoff cannot exist yet."
            )
        return LiveCutoffAssessment(
            status="NO_CUTOFF",
            message=(
                "Demand has not consumed the selected offer quantity; no allocation-"
                "stopping price has formed."
            ),
            offer_basis=offer_basis,
            offer_quantity=offer_quantity,
            base_offer_quantity=summary.base_offer_quantity,
            total_offer_quantity=summary.total_offer_quantity,
            summary_total_demand=summary.total_demand,
            nse_ladder_demand=nse_ladder.total_quantity if nse_ladder else None,
            bse_ladder_demand=bse_ladder.total_quantity if bse_ladder else None,
            combined_ladder_demand=(
                nse_ladder.total_quantity + bse_ladder.total_quantity
                if nse_ladder and bse_ladder
                else None
            ),
            reconciliation_gap=None,
            cutoff=None,
            working_bid=None,
            warnings=tuple(warnings),
            errors=(),
        )

    if nse_ladder is None:
        errors.append("NSE exact-price ladder is missing")
    if bse_ladder is None:
        errors.append("BSE exact-price ladder is missing")
    if errors:
        return LiveCutoffAssessment(
            status="UNSAFE",
            message=(
                "Demand has reached supply, but both exchange ladders are required "
                "to locate the marginal price."
            ),
            offer_basis=offer_basis,
            offer_quantity=offer_quantity,
            base_offer_quantity=summary.base_offer_quantity,
            total_offer_quantity=summary.total_offer_quantity,
            summary_total_demand=summary.total_demand,
            nse_ladder_demand=nse_ladder.total_quantity if nse_ladder else None,
            bse_ladder_demand=bse_ladder.total_quantity if bse_ladder else None,
            combined_ladder_demand=None,
            reconciliation_gap=None,
            cutoff=None,
            working_bid=None,
            warnings=tuple(warnings),
            errors=tuple(errors),
        )

    assert nse_ladder is not None and bse_ladder is not None
    for ladder in (nse_ladder, bse_ladder):
        if ladder.symbol.upper() != summary.symbol.upper():
            errors.append(
                f"{ladder.exchange} ladder symbol {ladder.symbol} does not match "
                f"{summary.symbol}"
            )
        if ladder.category.upper() != "NON_RETAIL":
            errors.append(f"{ladder.exchange} ladder is not NON_RETAIL")

    if _is_live_session(summary, now):
        for ladder in (nse_ladder, bse_ladder):
            # BSE does not always publish an exchange timestamp in the JSON.
            # The successful HTTP retrieval time is then the conservative proxy.
            timestamp = ladder.as_of or ladder.fetched_at
            stale = _age_error(
                f"{ladder.exchange} ladder",
                timestamp,
                now=now,
                max_age=max_live_age,
            )
            if stale:
                errors.append(stale)

    source_times = [summary.as_of, nse_ladder.as_of]
    source_times.append(bse_ladder.as_of or bse_ladder.fetched_at)
    comparable_times = [stamp for stamp in source_times if stamp is not None]
    if len(comparable_times) >= 2:
        skew = max(comparable_times) - min(comparable_times)
        if skew > max_source_skew:
            errors.append(
                f"Exchange snapshots are {skew.total_seconds() / 60:.1f} minutes apart"
            )

    combined_total = nse_ladder.total_quantity + bse_ladder.total_quantity
    reconciliation_gap = combined_total - summary.total_demand
    if reconciliation_gap != 0:
        errors.append(
            "NSE+BSE exact-price ladders do not reconcile to the consolidated "
            f"summary (gap {reconciliation_gap:+,})"
        )

    if errors:
        return LiveCutoffAssessment(
            status="UNSAFE",
            message=(
                "The cross-exchange book is incomplete or asynchronous; no cutoff "
                "price is safe to publish."
            ),
            offer_basis=offer_basis,
            offer_quantity=offer_quantity,
            base_offer_quantity=summary.base_offer_quantity,
            total_offer_quantity=summary.total_offer_quantity,
            summary_total_demand=summary.total_demand,
            nse_ladder_demand=nse_ladder.total_quantity,
            bse_ladder_demand=bse_ladder.total_quantity,
            combined_ladder_demand=combined_total,
            reconciliation_gap=reconciliation_gap,
            cutoff=None,
            working_bid=None,
            warnings=tuple(warnings),
            errors=tuple(errors),
        )

    cutoff = calculate_non_retail_cutoff(
        (*nse_ladder.bids, *bse_ladder.bids),
        offer_quantity,
        category="NON_RETAIL",
    )
    if cutoff.cutoff_price is None:
        errors.append(
            "Reconciled demand reached supply, but the cutoff engine returned no price"
        )
        return LiveCutoffAssessment(
            status="UNSAFE",
            message="The reconciled book is internally inconsistent.",
            offer_basis=offer_basis,
            offer_quantity=offer_quantity,
            base_offer_quantity=summary.base_offer_quantity,
            total_offer_quantity=summary.total_offer_quantity,
            summary_total_demand=summary.total_demand,
            nse_ladder_demand=nse_ladder.total_quantity,
            bse_ladder_demand=bse_ladder.total_quantity,
            combined_ladder_demand=combined_total,
            reconciliation_gap=reconciliation_gap,
            cutoff=None,
            working_bid=None,
            warnings=tuple(warnings),
            errors=tuple(errors),
        )

    return LiveCutoffAssessment(
        status="ESTIMATED",
        message=(
            "The cross-exchange exact-price book reconciles to the consolidated "
            "total; the marginal allocation price is observable."
        ),
        offer_basis=offer_basis,
        offer_quantity=offer_quantity,
        base_offer_quantity=summary.base_offer_quantity,
        total_offer_quantity=summary.total_offer_quantity,
        summary_total_demand=summary.total_demand,
        nse_ladder_demand=nse_ladder.total_quantity,
        bse_ladder_demand=bse_ladder.total_quantity,
        combined_ladder_demand=combined_total,
        reconciliation_gap=reconciliation_gap,
        cutoff=cutoff,
        working_bid=cutoff.cutoff_price + tick_size,
        warnings=tuple(warnings),
        errors=(),
    )

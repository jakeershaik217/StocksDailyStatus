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
    demand_basis: str
    decision_demand: int
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


def _ladder_time(ladder: LadderSnapshot) -> datetime:
    return ladder.as_of or ladder.fetched_at


def assess_live_cutoff(
    summary: OFSSummary,
    nse_ladder: LadderSnapshot | None,
    bse_ladder: LadderSnapshot | None,
    *,
    offer_override: int | None = None,
    tick_size: Decimal = Decimal("0.05"),
    now: datetime | None = None,
    max_live_age: timedelta = timedelta(minutes=15),
    max_source_skew: timedelta = timedelta(minutes=3),
) -> LiveCutoffAssessment:
    """Assess a live cross-exchange marginal OFS price without mixing timestamps.

    Each exchange ladder must reconcile its incremental quantities to its own
    cumulative totals. When fresh NSE and BSE ladders are close in time, their
    combined exact-price book is the decision source. NSE's slower summary is a
    timestamped control; it blocks on a same-time mismatch but is only diagnostic
    when it describes an older or newer snapshot.
    """
    now = now or datetime.now(timezone.utc)
    warnings: list[str] = []

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

    nse_total = nse_ladder.total_quantity if nse_ladder else None
    bse_total = bse_ladder.total_quantity if bse_ladder else None
    combined_total = (
        nse_total + bse_total
        if nse_total is not None and bse_total is not None
        else None
    )
    reconciliation_gap = (
        combined_total - summary.total_demand if combined_total is not None else None
    )
    live_session = _is_live_session(summary, now)

    summary_freshness_error = (
        _age_error(
            "NSE published summary",
            summary.as_of,
            now=now,
            max_age=max_live_age,
        )
        if live_session
        else None
    )

    ladder_errors: list[str] = []
    if nse_ladder is None:
        ladder_errors.append("NSE exact-price ladder is missing")
    if bse_ladder is None:
        ladder_errors.append("BSE exact-price ladder is missing")

    if nse_ladder and bse_ladder:
        for ladder in (nse_ladder, bse_ladder):
            if ladder.symbol.upper() != summary.symbol.upper():
                ladder_errors.append(
                    f"{ladder.exchange} ladder symbol {ladder.symbol} does not match "
                    f"{summary.symbol}"
                )
            if ladder.category.upper() != "NON_RETAIL":
                ladder_errors.append(f"{ladder.exchange} ladder is not NON_RETAIL")
            if live_session:
                stale = _age_error(
                    f"{ladder.exchange} ladder",
                    _ladder_time(ladder),
                    now=now,
                    max_age=max_live_age,
                )
                if stale:
                    ladder_errors.append(stale)

        exchange_skew = abs(_ladder_time(nse_ladder) - _ladder_time(bse_ladder))
        if exchange_skew > max_source_skew:
            ladder_errors.append(
                "NSE and BSE ladders are "
                f"{exchange_skew.total_seconds() / 60:.1f} minutes apart"
            )

    same_time_control_conflict = False
    if nse_ladder and bse_ladder and reconciliation_gap and summary.as_of is not None:
        all_times = [
            summary.as_of,
            _ladder_time(nse_ladder),
            _ladder_time(bse_ladder),
        ]
        control_skew = max(all_times) - min(all_times)
        if control_skew <= timedelta(seconds=30):
            same_time_control_conflict = True
            ladder_errors.append(
                "Same-time NSE+BSE ladders conflict with the published summary "
                f"(gap {reconciliation_gap:+,})"
            )
        else:
            warnings.append(
                "NSE's published summary and the exchange ladders have different "
                f"timestamps, so their {reconciliation_gap:+,} gap is diagnostic, "
                "not double-counted into the cutoff."
            )

    ladders_usable = (
        nse_ladder is not None
        and bse_ladder is not None
        and not ladder_errors
        and not same_time_control_conflict
    )

    if ladders_usable:
        assert combined_total is not None
        demand_basis = "NSE_BSE_EXACT_PRICE_LADDERS"
        decision_demand = combined_total
        if summary_freshness_error:
            warnings.append(
                f"{summary_freshness_error}; the decision uses the fresher exchange ladders."
            )
    else:
        demand_basis = "NSE_PUBLISHED_SUMMARY"
        decision_demand = summary.total_demand

    def result(
        *,
        status: str,
        message: str,
        cutoff: CutoffEstimate | None = None,
        working_bid: Decimal | None = None,
        errors: tuple[str, ...] = (),
    ) -> LiveCutoffAssessment:
        return LiveCutoffAssessment(
            status=status,
            message=message,
            offer_basis=offer_basis,
            offer_quantity=offer_quantity,
            base_offer_quantity=summary.base_offer_quantity,
            total_offer_quantity=summary.total_offer_quantity,
            demand_basis=demand_basis,
            decision_demand=decision_demand,
            summary_total_demand=summary.total_demand,
            nse_ladder_demand=nse_total,
            bse_ladder_demand=bse_total,
            combined_ladder_demand=combined_total,
            reconciliation_gap=reconciliation_gap,
            cutoff=cutoff,
            working_bid=working_bid,
            warnings=tuple(warnings),
            errors=errors,
        )

    if same_time_control_conflict:
        return result(
            status="UNSAFE",
            message="Simultaneous exchange totals conflict, so no cutoff is safe.",
            errors=tuple(ladder_errors),
        )

    if not ladders_usable and summary_freshness_error:
        return result(
            status="UNSAFE",
            message="No fresh, complete source can establish current demand.",
            errors=(summary_freshness_error, *ladder_errors),
        )

    if decision_demand < offer_quantity:
        if not ladders_usable:
            warnings.append(
                "Exchange price detail is incomplete, but the fresh NSE published total "
                "is below supply, so a marginal cutoff has not formed at that timestamp."
            )
        return result(
            status="NO_CUTOFF",
            message=(
                "Demand in the selected, timestamped source has not consumed the offer "
                "quantity; no allocation-stopping price has formed."
            ),
        )

    if not ladders_usable:
        return result(
            status="UNSAFE",
            message=(
                "Demand has reached supply, but fresh near-synchronous NSE and BSE "
                "ladders are required to locate the marginal price."
            ),
            errors=tuple(ladder_errors),
        )

    assert nse_ladder is not None and bse_ladder is not None
    cutoff = calculate_non_retail_cutoff(
        (*nse_ladder.bids, *bse_ladder.bids),
        offer_quantity,
        category="NON_RETAIL",
    )
    if cutoff.cutoff_price is None:
        return result(
            status="UNSAFE",
            message="The internally validated cross-exchange book is inconsistent.",
            errors=(
                "Combined exact-price demand reached supply, but the engine returned no price",
            ),
        )

    return result(
        status="ESTIMATED",
        message=(
            "Fresh, internally reconciled NSE and BSE ladders form a complete "
            "cross-exchange book; the marginal allocation price is observable."
        ),
        cutoff=cutoff,
        working_bid=cutoff.cutoff_price + tick_size,
    )

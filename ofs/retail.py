from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from .engine import BidLevel


@dataclass(frozen=True)
class RetailAssessment:
    reference_cutoff: Decimal
    reserved_quantity: int
    eligible_demand: int
    total_demand: int
    subscription: Decimal
    estimated_allocation_ratio: Decimal
    working_bid: Decimal
    methodology: str


def assess_retail(
    bids: Iterable[BidLevel],
    *,
    reference_cutoff: Decimal,
    reserved_quantity: int,
) -> RetailAssessment:
    """Analyze T+1 retail demand against the confirmed T-day cutoff.

    Retail does not discover a new HNI cutoff. Eligible retail demand is the
    quantity bid at or above the confirmed non-retail cutoff. When that demand
    exceeds the retail reservation, this returns the simple proportionate ratio
    implied by demand versus reserved quantity. Final settlement remains subject
    to the issue-specific exchange allocation methodology.
    """
    if reserved_quantity <= 0:
        raise ValueError("reserved_quantity must be positive")

    levels = [b for b in bids if int(b.quantity) > 0]
    total = sum(int(b.quantity) for b in levels)
    eligible = sum(int(b.quantity) for b in levels if Decimal(str(b.price)) >= reference_cutoff)
    subscription = Decimal(eligible) / Decimal(reserved_quantity)
    ratio = min(Decimal(1), Decimal(reserved_quantity) / Decimal(eligible)) if eligible else Decimal(0)

    return RetailAssessment(
        reference_cutoff=reference_cutoff,
        reserved_quantity=reserved_quantity,
        eligible_demand=eligible,
        total_demand=total,
        subscription=subscription,
        estimated_allocation_ratio=ratio,
        working_bid=reference_cutoff,
        methodology="t_plus_1_retail_against_confirmed_non_retail_cutoff",
    )

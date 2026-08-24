from __future__ import annotations

from dataclasses import dataclass, asdict
from decimal import Decimal, InvalidOperation
from typing import Iterable


@dataclass(frozen=True)
class BidLevel:
    price: Decimal
    quantity: int
    exchange: str = "UNKNOWN"
    category: str = "GENERAL"


@dataclass(frozen=True)
class CutoffEstimate:
    category: str
    offered_quantity: int
    cumulative_quantity_above_cutoff: int
    quantity_at_cutoff_price: int
    quantity_needed_at_cutoff: int
    cutoff_price: Decimal | None
    saturation_ratio: Decimal
    methodology: str

    @property
    def cumulative_quantity_at_cutoff(self) -> int:
        """Total demand at and above the cutoff price level (full cutoff-level quantity)."""
        return self.cumulative_quantity_above_cutoff + self.quantity_at_cutoff_price

    def to_dict(self) -> dict:
        data = asdict(self)
        data["cumulative_quantity_at_cutoff"] = self.cumulative_quantity_at_cutoff
        data["cutoff_price"] = str(data["cutoff_price"]) if data["cutoff_price"] is not None else None
        data["saturation_ratio"] = str(data["saturation_ratio"])
        return data


def _int(value: object) -> int:
    if isinstance(value, int):
        return value
    try:
        return int(Decimal(str(value).replace(",", "").strip()))
    except (InvalidOperation, AttributeError) as exc:
        raise ValueError(f"Invalid quantity: {value!r}") from exc


def _dec(value: object) -> Decimal:
    try:
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, AttributeError) as exc:
        raise ValueError(f"Invalid price: {value!r}") from exc


def calculate_non_retail_cutoff(
    bids: Iterable[BidLevel],
    final_offer_quantity: int,
    *,
    category: str = "NON_RETAIL",
) -> CutoffEstimate:
    """Calculate the T-day OFS non-retail cutoff from all valid bids.

    Cutoff is the lowest price at which an investor is allocated shares. For
    price-priority/multiple-clearing-price OFS, bids are ordered high-to-low and
    the cutoff is the first price level where cumulative valid demand reaches
    the final quantity being allocated.

    At the cutoff level, only the quantity still available is allocated. The
    engine records that partial-fill quantity rather than implying every bid at
    the cutoff receives its full requested quantity.
    """
    if final_offer_quantity <= 0:
        raise ValueError("final_offer_quantity must be positive")

    normalized = [
        BidLevel(_dec(b.price), _int(b.quantity), b.exchange, b.category)
        for b in bids
        if b.price is not None and _int(b.quantity) > 0
    ]

    # Aggregate the same price across NSE/BSE before finding the clearing level.
    price_qty: dict[Decimal, int] = {}
    for level in normalized:
        price_qty[level.price] = price_qty.get(level.price, 0) + level.quantity

    cumulative = 0
    cumulative_above = 0
    cutoff: Decimal | None = None
    qty_at_cutoff = 0
    qty_needed = 0

    for price in sorted(price_qty, reverse=True):
        level_qty = price_qty[price]
        if cumulative + level_qty >= final_offer_quantity:
            cutoff = price
            qty_at_cutoff = level_qty
            qty_needed = max(final_offer_quantity - cumulative, 0)
            break
        cumulative += level_qty
        cumulative_above = cumulative

    allocated_at_cutoff = qty_needed if cutoff is not None else 0
    cumulative_at_cutoff = cumulative + allocated_at_cutoff
    ratio = Decimal(cumulative_at_cutoff) / Decimal(final_offer_quantity)

    return CutoffEstimate(
        category=category,
        offered_quantity=final_offer_quantity,
        cumulative_quantity_above_cutoff=cumulative_above,
        quantity_at_cutoff_price=qty_at_cutoff,
        quantity_needed_at_cutoff=allocated_at_cutoff,
        cutoff_price=cutoff,
        saturation_ratio=ratio,
        methodology="t_day_final_valid_bids_price_priority_multiple_clearing_prices",
    )


def estimate_cutoff(
    bids: Iterable[BidLevel],
    offered_quantity: int,
    *,
    category: str = "GENERAL",
) -> CutoffEstimate:
    """Backward-compatible alias for the final OFS cutoff calculator."""
    return calculate_non_retail_cutoff(bids, offered_quantity, category=category)

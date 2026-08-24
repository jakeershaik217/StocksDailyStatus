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
    cumulative_quantity_at_cutoff: int
    cutoff_price: Decimal | None
    saturation_ratio: Decimal
    methodology: str

    def to_dict(self) -> dict:
        data = asdict(self)
        data["cutoff_price"] = str(data["cutoff_price"]) if data["cutoff_price"] is not None else None
        data["saturation_ratio"] = str(data["saturation_ratio"])
        return data


def _dec(value: object) -> Decimal:
    try:
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, AttributeError) as exc:
        raise ValueError(f"Invalid price: {value!r}") from exc


def estimate_cutoff(
    bids: Iterable[BidLevel],
    offered_quantity: int,
    *,
    category: str = "GENERAL",
) -> CutoffEstimate:
    """Estimate the price at which cumulative demand reaches offered supply.

    For a price-priority OFS, valid bids are sorted from highest to lowest price.
    The estimated cutoff is the lowest price whose cumulative demand reaches the
    offered quantity. If the visible book never reaches supply, the estimate is
    unavailable and callers should not fabricate a cutoff.
    """
    if offered_quantity <= 0:
        raise ValueError("offered_quantity must be positive")

    normalized = [
        BidLevel(_dec(b.price), int(b.quantity), b.exchange, b.category)
        for b in bids
        if int(b.quantity) > 0
    ]
    normalized.sort(key=lambda b: b.price, reverse=True)

    cumulative = 0
    cutoff: Decimal | None = None
    for level in normalized:
        cumulative += level.quantity
        if cumulative >= offered_quantity:
            cutoff = level.price
            break

    ratio = Decimal(cumulative) / Decimal(offered_quantity)
    methodology = "price_priority_visible_book"
    return CutoffEstimate(
        category=category,
        offered_quantity=offered_quantity,
        cumulative_quantity_at_cutoff=cumulative,
        cutoff_price=cutoff,
        saturation_ratio=ratio,
        methodology=methodology,
    )

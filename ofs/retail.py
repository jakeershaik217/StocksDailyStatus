from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from .engine import BidLevel, CutoffEstimate, calculate_non_retail_cutoff
from .sources import IST, parse_exchange_timestamp


@dataclass(frozen=True)
class RetailLadderSnapshot:
    exchange: str
    symbol: str
    bids: tuple[BidLevel, ...]
    cutoff_bid_quantity: int
    as_of: datetime | None
    fetched_at: datetime
    source_url: str
    raw_payload: Any

    @property
    def numeric_bid_quantity(self) -> int:
        return sum(int(level.quantity) for level in self.bids)

    @property
    def total_quantity(self) -> int:
        return self.numeric_bid_quantity + self.cutoff_bid_quantity


@dataclass(frozen=True)
class RetailAssessment:
    status: str
    reference_cutoff: Decimal
    reserved_quantity: int
    numeric_bid_quantity: int
    cutoff_bid_quantity: int
    eligible_numeric_demand: int
    eligible_demand: int
    total_demand: int
    subscription: Decimal
    estimated_allocation_ratio: Decimal
    predicted_cutoff: Decimal
    demand_above_cutoff: int
    demand_at_cutoff: int
    shares_available_at_cutoff: int
    unallocated_demand_at_cutoff: int
    cutoff_level_allocation_ratio: Decimal
    working_bid: Decimal
    cutoff: CutoffEstimate | None
    methodology: str


def _rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("Table", "table", "data", "Data", "records", "result"):
        candidate = payload.get(key)
        if isinstance(candidate, list):
            return [row for row in candidate if isinstance(row, dict)]
    return []


def _pick(row: dict[str, Any], names: Iterable[str]) -> Any:
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return value
    return None


def _quantity(value: Any) -> int:
    return int(Decimal(str(value).replace(",", "").strip()))


def _is_cutoff_label(value: Any) -> bool:
    normalized = re.sub(r"[^a-z]", "", str(value).lower())
    return normalized == "cutoff"


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "cutoff"}


def parse_retail_ladder(
    payload: Any,
    *,
    exchange: str,
    symbol: str,
    source_url: str = "",
    fetched_at: datetime | None = None,
    expected_series: str | None = None,
) -> RetailLadderSnapshot:
    """Parse numeric and literal Cut-off retail bids without losing either."""
    rows = _rows(payload)
    bids: list[BidLevel] = []
    cutoff_quantity = 0
    timestamps: set[datetime] = set()
    reported_series = {
        str(value).strip().upper()
        for row in rows
        if (value := _pick(row, ("ser", "series", "Series")))
        not in (None, "")
    }
    if expected_series and reported_series != {expected_series.upper()}:
        shown = ", ".join(sorted(reported_series)) or "not shown"
        raise ValueError(
            f"{exchange} retail ladder series mismatch: expected "
            f"{expected_series.upper()}, received {shown}"
        )

    for row in rows:
        price_value = _pick(
            row,
            (
                "pri",
                "price",
                "Price",
                "OE_PRICE",
                "BIDPRICE",
                "BID_PRICE",
                "BidPrice",
                "bidPrice",
            ),
        )
        quantity_value = _pick(
            row,
            (
                "totQty",
                "TOTAL_QTY",
                "quantity",
                "Quantity",
                "BIDQTY",
                "BID_QTY",
                "BidQty",
                "bidQty",
            ),
        )
        timestamp_value = _pick(
            row,
            ("dat", "DTTM", "DAT", "DATE_TIME", "TIMESTAMP", "Date", "date"),
        )
        parsed_timestamp = parse_exchange_timestamp(timestamp_value)
        if parsed_timestamp is not None:
            timestamps.add(parsed_timestamp)

        if price_value in (None, "") or quantity_value in (None, ""):
            continue
        quantity = _quantity(quantity_value)
        if quantity <= 0:
            continue

        cutoff_flag = _pick(
            row,
            ("isCutOff", "isCutoff", "cutOff", "cutoff", "CUT_OFF_IND"),
        )
        if _is_cutoff_label(price_value) or (
            cutoff_flag is not None and _truthy(cutoff_flag)
        ):
            cutoff_quantity += quantity
            continue

        try:
            price = Decimal(str(price_value).replace(",", "").strip())
        except InvalidOperation as exc:
            raise ValueError(
                f"Unrecognized {exchange} retail price label: {price_value!r}"
            ) from exc
        if price <= 0:
            continue
        bids.append(
            BidLevel(
                price=price,
                quantity=quantity,
                exchange=exchange.upper(),
                category="RETAIL",
            )
        )

    if len(timestamps) > 1:
        # Multiple rows can be published a few seconds apart. The newest stamp is
        # the correct book timestamp for retail monitoring.
        as_of = max(timestamps)
    else:
        as_of = next(iter(timestamps), None)

    snapshot = RetailLadderSnapshot(
        exchange=exchange.upper(),
        symbol=symbol.upper(),
        bids=tuple(bids),
        cutoff_bid_quantity=cutoff_quantity,
        as_of=as_of,
        fetched_at=fetched_at or datetime.now(timezone.utc),
        source_url=source_url,
        raw_payload=payload,
    )

    if isinstance(payload, dict):
        totals = payload.get("Table2")
        if isinstance(totals, list) and totals:
            displayed = _pick(
                totals[0],
                ("TOTAL_CUMM", "TOTAL_QTY", "CUM_TOTAL_QTY", "totQty"),
            )
            if displayed not in (None, "") and _quantity(displayed) != snapshot.total_quantity:
                raise ValueError(
                    f"{exchange} retail total mismatch: calculated="
                    f"{snapshot.total_quantity}, displayed={_quantity(displayed)}"
                )
    return snapshot


def derive_retail_quantity(
    *,
    gross_final_offer_quantity: int | None,
    final_non_retail_quantity: int | None,
    reserved_percentage: Decimal,
    explicit_retail_quantity: int | None = None,
) -> tuple[int, str]:
    if explicit_retail_quantity is not None:
        if explicit_retail_quantity <= 0:
            raise ValueError("explicit_retail_quantity must be positive")
        return explicit_retail_quantity, "USER_RETAIL_QUANTITY"

    if gross_final_offer_quantity is None or gross_final_offer_quantity <= 0:
        raise ValueError("gross final offer quantity is required")
    if (
        final_non_retail_quantity is not None
        and 0 < final_non_retail_quantity < gross_final_offer_quantity
    ):
        return (
            gross_final_offer_quantity - final_non_retail_quantity,
            "GROSS_MINUS_NSE_FINAL_NON_RETAIL",
        )

    if not Decimal("0") < reserved_percentage <= Decimal("1"):
        raise ValueError("reserved_percentage must be between 0 and 1")
    return (
        int(Decimal(gross_final_offer_quantity) * reserved_percentage),
        "GROSS_TIMES_RESERVED_PERCENTAGE",
    )


def assess_retail(
    bids: Iterable[BidLevel],
    *,
    reference_cutoff: Decimal,
    reserved_quantity: int,
    cutoff_bid_quantity: int = 0,
    tick_size: Decimal = Decimal("0.05"),
) -> RetailAssessment:
    """Predict the T+1 retail marginal allocation price.

    A literal Cut-off order accepts the confirmed T-day non-retail cutoff, so it
    enters the retail price-priority ladder at ``reference_cutoff``. Numeric
    bids below that price are ineligible. Eligible levels from both exchanges
    are aggregated and walked high-to-low against the retail reservation.
    """
    if reserved_quantity <= 0:
        raise ValueError("reserved_quantity must be positive")
    if cutoff_bid_quantity < 0:
        raise ValueError("cutoff_bid_quantity cannot be negative")
    if tick_size <= 0:
        raise ValueError("tick_size must be positive")

    levels = [level for level in bids if int(level.quantity) > 0]
    numeric_total = sum(int(level.quantity) for level in levels)
    eligible_levels = [
        level
        for level in levels
        if Decimal(str(level.price)) >= reference_cutoff
    ]
    eligible_numeric = sum(int(level.quantity) for level in eligible_levels)
    if cutoff_bid_quantity:
        eligible_levels.append(
            BidLevel(
                price=reference_cutoff,
                quantity=cutoff_bid_quantity,
                exchange="NSE_BSE_CUTOFF",
                category="RETAIL",
            )
        )
    total = numeric_total + cutoff_bid_quantity
    eligible = eligible_numeric + cutoff_bid_quantity
    subscription = Decimal(eligible) / Decimal(reserved_quantity)

    cutoff: CutoffEstimate | None = None
    predicted_cutoff = reference_cutoff
    demand_above = sum(
        int(level.quantity)
        for level in eligible_levels
        if Decimal(str(level.price)) > reference_cutoff
    )
    demand_at = sum(
        int(level.quantity)
        for level in eligible_levels
        if Decimal(str(level.price)) == reference_cutoff
    )
    shares_at_cutoff = demand_at
    cutoff_ratio = Decimal(1) if demand_at else Decimal(0)
    unallocated_at_cutoff = 0
    working_bid = reference_cutoff

    if eligible >= reserved_quantity:
        cutoff = calculate_non_retail_cutoff(
            eligible_levels,
            reserved_quantity,
            category="RETAIL",
        )
        if cutoff.cutoff_price is None:
            raise ValueError("retail book reached supply but no cutoff was found")
        predicted_cutoff = cutoff.cutoff_price
        demand_above = cutoff.cumulative_quantity_above_cutoff
        demand_at = cutoff.quantity_at_cutoff_price
        shares_at_cutoff = cutoff.quantity_needed_at_cutoff
        unallocated_at_cutoff = max(demand_at - shares_at_cutoff, 0)
        cutoff_ratio = (
            Decimal(shares_at_cutoff) / Decimal(demand_at)
            if demand_at
            else Decimal(0)
        )
        higher_prices = sorted(
            {
                Decimal(str(level.price))
                for level in eligible_levels
                if Decimal(str(level.price)) > predicted_cutoff
            }
        )
        working_bid = higher_prices[0] if higher_prices else predicted_cutoff + tick_size
        status = (
            "PREDICTED_CUTOFF"
            if eligible > reserved_quantity
            else "FULLY_SUBSCRIBED"
        )
    else:
        status = "UNDER_SUBSCRIBED"

    overall_ratio = (
        min(Decimal(1), Decimal(reserved_quantity) / Decimal(eligible))
        if eligible
        else Decimal(0)
    )

    return RetailAssessment(
        status=status,
        reference_cutoff=reference_cutoff,
        reserved_quantity=reserved_quantity,
        numeric_bid_quantity=numeric_total,
        cutoff_bid_quantity=cutoff_bid_quantity,
        eligible_numeric_demand=eligible_numeric,
        eligible_demand=eligible,
        total_demand=total,
        subscription=subscription,
        estimated_allocation_ratio=overall_ratio,
        predicted_cutoff=predicted_cutoff,
        demand_above_cutoff=demand_above,
        demand_at_cutoff=demand_at,
        shares_available_at_cutoff=shares_at_cutoff,
        unallocated_demand_at_cutoff=unallocated_at_cutoff,
        cutoff_level_allocation_ratio=cutoff_ratio,
        working_bid=working_bid,
        cutoff=cutoff,
        methodology="retail_price_priority_with_cutoff_orders_at_t_day_cutoff",
    )

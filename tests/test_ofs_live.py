from datetime import datetime, timezone
from decimal import Decimal

import pytest

from ofs.engine import BidLevel
from ofs.live import assess_live_cutoff
from ofs.sources import (
    IST,
    LadderSnapshot,
    OFSSummary,
    parse_bse_ladder,
    parse_nse_ladder,
    parse_nse_summary,
)

NOW = datetime(2026, 8, 25, 11, 0, tzinfo=IST)


def summary(
    *,
    demand: int,
    total_offer: int | None = None,
    as_of: datetime | None = None,
) -> OFSSummary:
    return OFSSummary(
        symbol="TEST",
        company_name="Test Limited",
        series="IS",
        offer_date="25-Aug-2026",
        status="Active",
        base_offer_quantity=1000,
        total_offer_quantity=total_offer,
        total_demand=demand,
        margin_100_quantity=demand,
        margin_0_quantity=0,
        floor_price=Decimal(100),
        indicative_price=Decimal(103),
        ltp=Decimal(110),
        as_of=as_of or datetime(2026, 8, 25, 10, 59, tzinfo=IST),
        source_url="nse-summary",
        raw_payload={},
    )


def ladder(
    exchange: str,
    rows: list[tuple[str, int]],
    *,
    stamp: datetime | None = None,
) -> LadderSnapshot:
    stamp = stamp or datetime(2026, 8, 25, 10, 59, tzinfo=IST)
    return LadderSnapshot(
        exchange=exchange,
        symbol="TEST",
        category="NON_RETAIL",
        bids=tuple(
            BidLevel(Decimal(price), quantity, exchange, "NON_RETAIL")
            for price, quantity in rows
        ),
        as_of=stamp if exchange == "NSE" else None,
        fetched_at=stamp.astimezone(timezone.utc),
        source_url=f"{exchange.lower()}-ladder",
        confirmed_quantity=None,
        unconfirmed_quantity=None,
        raw_payload={},
    )


def test_nse_public_fields_are_parsed_as_incremental_levels():
    payload = [
        {
            "pri": "514",
            "totQty": "600",
            "cumTQty": "700",
            "conQty": "600",
            "uCQty": "0",
            "sym": "TEST",
            "dat": "25-Aug-2026 10:59:00 IST",
        },
        {
            "pri": "515",
            "totQty": "100",
            "cumTQty": "100",
            "conQty": "100",
            "uCQty": "0",
            "sym": "TEST",
            "dat": "25-Aug-2026 10:59:00 IST",
        },
    ]
    result = parse_nse_ladder(payload, symbol="TEST")
    assert result.total_quantity == 700
    assert result.bids[0].price == Decimal(514)
    assert result.bids[0].quantity == 600
    assert result.as_of == datetime(2026, 8, 25, 10, 59, tzinfo=IST)


def test_nse_cumulative_reconciliation_rejects_wrong_field_semantics():
    payload = [
        {"pri": "514", "totQty": "600", "cumTQty": "999", "sym": "TEST"},
        {"pri": "515", "totQty": "100", "cumTQty": "100", "sym": "TEST"},
    ]
    with pytest.raises(ValueError, match="cumulative reconciliation"):
        parse_nse_ladder(payload, symbol="TEST")


def test_bse_official_fields_are_parsed():
    payload = {
        "Table": [
            {
                "OE_PRICE": "514.05",
                "TOTAL_QTY": "2,000",
                "CONFIRMEDQTY": "1,900",
                "UNC_QTY": "100",
                "TOTAL_CUMM": "2,000",
                "DTTM": "2026-08-25T10:59:12.123",
            }
        ],
        "Table2": [{"PRICE": "Total", "TOTAL_CUMM": "2,000"}],
    }
    result = parse_bse_ladder(payload, symbol="TEST")
    assert result.total_quantity == 2000
    assert result.bids[0].price == Decimal("514.05")
    assert result.confirmed_quantity == 1900
    assert result.unconfirmed_quantity == 100
    assert result.as_of == datetime(2026, 8, 25, 10, 59, 12, 123000, tzinfo=IST)


def test_bse_cumulative_total_mismatch_is_rejected():
    payload = {
        "Table": [
            {
                "OE_PRICE": "514",
                "TOTAL_QTY": "500",
                "TOTAL_CUMM": "999",
            },
            {
                "OE_PRICE": "515",
                "TOTAL_QTY": "100",
                "TOTAL_CUMM": "100",
            },
        ]
    }
    with pytest.raises(ValueError, match="cumulative reconciliation"):
        parse_bse_ladder(payload, symbol="TEST")


def test_bse_quantity_bucket_mismatch_is_rejected():
    payload = {
        "Table": [
            {
                "OE_PRICE": "514.05",
                "TOTAL_QTY": "2,000",
                "CONFIRMEDQTY": "1,500",
                "UNC_QTY": "100",
            }
        ]
    }
    with pytest.raises(ValueError, match="confirmed/unconfirmed reconciliation"):
        parse_bse_ladder(payload, symbol="TEST")


def test_nse_summary_uses_total_issue_only_when_exchange_publishes_it():
    payload = {
        "data": [
            {
                "symbol": "TEST",
                "series": "IS",
                "company": "Test Limited",
                "offerDate": "25-Aug-2026",
                "status": "Active",
                "totissueSize": "1,000",
                "gsIssueSize": "1,500",
                "totQty": "1,100",
                "cumu_100pcQty": "700",
                "cumu_0pcQty": "400",
                "floorPrice": "100",
                "indicative_Price": "103",
                "ltp": "110",
            }
        ],
        "timestamp": "25-Aug-2026 10:59:00",
    }
    parsed = parse_nse_summary(
        payload,
        symbol="TEST",
        series="IS",
        offer_date="25-Aug-2026",
    )
    assert parsed.base_offer_quantity == 1000
    assert parsed.total_offer_quantity == 1500
    assert parsed.total_demand == 1100


def test_fresh_under_subscribed_control_total_needs_no_marginal_price():
    result = assess_live_cutoff(
        summary(demand=900),
        ladder("NSE", [("105", 500)]),
        None,
        now=NOW,
    )
    assert result.status == "UNDER_SUBSCRIBED"
    assert result.cutoff is None
    assert result.offer_quantity == 1000
    assert result.demand_basis == "NSE_PUBLISHED_SUMMARY"


def test_total_issue_size_is_used_after_green_shoe_is_published():
    result = assess_live_cutoff(
        summary(demand=1100, total_offer=1200),
        ladder("NSE", [("105", 600)]),
        None,
        now=NOW,
    )
    assert result.status == "UNDER_SUBSCRIBED"
    assert result.offer_quantity == 1200
    assert result.offer_basis == "NSE_TOTAL_ISSUE"


def test_oversubscribed_book_without_bse_is_withheld():
    result = assess_live_cutoff(
        summary(demand=1100),
        ladder("NSE", [("105", 500)]),
        None,
        now=NOW,
    )
    assert result.status == "UNSAFE"
    assert "BSE exact-price ladder is missing" in result.errors


def test_reconciled_cross_exchange_book_returns_true_marginal_price():
    result = assess_live_cutoff(
        summary(demand=1100),
        ladder("NSE", [("105", 200), ("103", 300)]),
        ladder("BSE", [("104", 400), ("102", 200)]),
        now=NOW,
    )
    assert result.status == "ESTIMATED"
    assert result.cutoff is not None
    assert result.cutoff.cutoff_price == Decimal(102)
    assert result.working_bid == Decimal("102.05")
    assert result.reconciliation_gap == 0
    assert result.demand_basis == "NSE_BSE_EXACT_PRICE_LADDERS"


def test_newer_near_synchronous_ladders_can_use_an_older_summary_as_control():
    result = assess_live_cutoff(
        summary(
            demand=900,
            as_of=datetime(2026, 8, 25, 10, 50, tzinfo=IST),
        ),
        ladder(
            "NSE",
            [("105", 500)],
            stamp=datetime(2026, 8, 25, 10, 59, tzinfo=IST),
        ),
        ladder(
            "BSE",
            [("104", 600)],
            stamp=datetime(2026, 8, 25, 11, 0, tzinfo=IST),
        ),
        now=NOW,
    )
    assert result.status == "ESTIMATED"
    assert result.decision_demand == 1100
    assert result.reconciliation_gap == 200
    assert any("different timestamps" in warning for warning in result.warnings)


def test_cross_exchange_reconciliation_gap_withholds_cutoff():
    result = assess_live_cutoff(
        summary(demand=1100),
        ladder("NSE", [("105", 500)]),
        ladder("BSE", [("104", 500)]),
        now=NOW,
    )
    assert result.status == "UNSAFE"
    assert result.reconciliation_gap == -100
    assert any("conflict" in error for error in result.errors)

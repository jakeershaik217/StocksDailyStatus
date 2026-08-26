from decimal import Decimal

from ofs.engine import BidLevel
from ofs.retail import (
    assess_retail,
    derive_retail_quantity,
    parse_retail_ladder,
)
from ofs.sources import parse_nse_summary


def test_bse_literal_cutoff_row_is_preserved_and_counted():
    payload = {
        "Table": [
            {"OE_PRICE": "Cut-off", "TOTAL_QTY": "1,200"},
            {"OE_PRICE": "521", "TOTAL_QTY": "300"},
        ],
        "Table2": [{"TOTAL_CUMM": "1,500"}],
    }
    result = parse_retail_ladder(
        payload,
        exchange="BSE",
        symbol="TEST",
    )
    assert result.cutoff_bid_quantity == 1200
    assert result.numeric_bid_quantity == 300
    assert result.total_quantity == 1500


def test_nse_cutoff_flag_is_preserved_even_when_price_is_zero():
    payload = [
        {"pri": "0", "totQty": "700", "isCutOff": "true", "ser": "RS"},
        {"pri": "522", "totQty": "200", "ser": "RS"},
    ]
    result = parse_retail_ladder(
        payload,
        exchange="NSE",
        symbol="TEST",
        expected_series="RS",
    )
    assert result.cutoff_bid_quantity == 700
    assert result.numeric_bid_quantity == 200


def test_non_retail_series_is_rejected_from_retail_book():
    payload = [{"pri": "520", "totQty": "700", "ser": "IS"}]
    try:
        parse_retail_ladder(
            payload,
            exchange="NSE",
            symbol="TEST",
            expected_series="RS",
        )
    except ValueError as exc:
        assert "series mismatch" in str(exc)
    else:
        raise AssertionError("IS ladder must not be accepted as retail")


def test_cutoff_orders_are_eligible_with_numeric_bids_at_or_above_reference():
    bids = [
        BidLevel(Decimal("519"), 100, "NSE", "RETAIL"),
        BidLevel(Decimal("520"), 200, "NSE", "RETAIL"),
        BidLevel(Decimal("522"), 300, "BSE", "RETAIL"),
    ]
    result = assess_retail(
        bids,
        reference_cutoff=Decimal("520"),
        reserved_quantity=1000,
        cutoff_bid_quantity=400,
    )
    assert result.total_demand == 1000
    assert result.eligible_numeric_demand == 500
    assert result.eligible_demand == 900
    assert result.subscription == Decimal("0.9")
    assert result.estimated_allocation_ratio == Decimal("1")


def test_exact_retail_quantity_uses_gross_minus_final_non_retail():
    quantity, basis = derive_retail_quantity(
        gross_final_offer_quantity=58_021_442,
        final_non_retail_quantity=52_219_296,
        reserved_percentage=Decimal("0.10"),
    )
    assert quantity == 5_802_146
    assert basis == "GROSS_MINUS_NSE_FINAL_NON_RETAIL"


def test_nse_final_cutoff_is_parsed_for_retail_reference():
    payload = {
        "data": [
            {
                "symbol": "HINDCOPPER",
                "series": "IS",
                "company": "Hindustan Copper Limited",
                "offerDate": "25-Aug-2026",
                "status": "Active",
                "totissueSize": "26109648",
                "gsIssueSize": "52219296",
                "totQty": "89119833",
                "cumu_100pcQty": "54711288",
                "cumu_0pcQty": "34408545",
                "floorPrice": "514",
                "indicative_Price": "520.35",
                "cutOffPrice": "520",
                "ltp": "532.95",
            }
        ],
        "timestamp": "25-Aug-2026 15:45:00",
    }
    result = parse_nse_summary(
        payload,
        symbol="HINDCOPPER",
        series="IS",
        offer_date="25-Aug-2026",
    )
    assert result.cutoff_price == Decimal("520")
    assert result.total_offer_quantity == 52_219_296

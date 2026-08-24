from decimal import Decimal

from ofs.engine import BidLevel, estimate_cutoff


def test_cutoff_reaches_supply_at_price_level():
    bids = [
        BidLevel(105, 200),
        BidLevel(104, 300),
        BidLevel(103, 600),
        BidLevel(102, 900),
    ]
    result = estimate_cutoff(bids, 1000)
    assert result.cutoff_price == Decimal("103")
    assert result.cumulative_quantity_at_cutoff == 1100


def test_cutoff_is_none_when_book_does_not_reach_supply():
    bids = [BidLevel(105, 200), BidLevel(104, 300)]
    result = estimate_cutoff(bids, 1000)
    assert result.cutoff_price is None
    assert result.cumulative_quantity_at_cutoff == 500


def test_price_levels_are_aggregated_across_exchanges_by_sorting():
    bids = [
        BidLevel(100, 400, "BSE"),
        BidLevel(101, 250, "NSE"),
        BidLevel(100, 500, "NSE"),
    ]
    result = estimate_cutoff(bids, 800)
    assert result.cutoff_price == Decimal("100")
    assert result.cumulative_quantity_at_cutoff == 1150

import unittest

from src.stock_status import parse_quote, render_report


class StockStatusTests(unittest.TestCase):
    def test_parse_quote_and_change(self):
        csv_text = "Date,Open,High,Low,Close,Volume\n2026-08-14,100,110,95,105,1000\n2026-08-15,106,115,100,110,1200\n"
        quote = parse_quote("TEST", csv_text)
        self.assertEqual(quote.trading_date, "2026-08-15")
        self.assertEqual(quote.close, 110.0)
        self.assertAlmostEqual(quote.change_pct or 0, 4.7619, places=3)

    def test_render_report(self):
        csv_text = "Date,Open,High,Low,Close,Volume\n2026-08-15,100,110,95,110,1000\n"
        report = render_report([parse_quote("TEST", csv_text)])
        self.assertIn("TEST", report)
        self.assertIn("110.00", report)
        self.assertIn("N/A", report)


if __name__ == "__main__":
    unittest.main()

import json
import time
import unittest
from pathlib import Path

from poe2arb.core import LiveQuote, find_candidates, historical_edges, simulate_exact
from poe2arb.ocr import _parse

ROOT = Path(__file__).parents[1]


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.demo = json.loads((ROOT / "poe2arb" / "demo.json").read_text(encoding="utf-8"))
        self.edges = historical_edges(self.demo["markets"], "演示联赛")

    def test_historical_cycle_is_only_reference(self):
        base = "Metadata/Items/Currency/CurrencyAddModToRare"
        three = find_candidates(self.edges, base, lengths=(3,))
        four = find_candidates(self.edges, base, lengths=(4,))
        self.assertTrue(three)
        self.assertTrue(four)
        self.assertGreater(float(three[0].reference_gain), 0)
        self.assertEqual(three[0].path[0], three[0].path[-1])

    def test_exact_orders_keep_leftovers(self):
        path = ("div", "ex", "chaos", "div")
        now = int(time.time())
        quotes = [
            LiveQuote("div", "ex", 10, 2400, 2400, 20000, now),
            LiveQuote("ex", "chaos", 2300, 100, 100, 15000, now),
            LiveQuote("chaos", "div", 100, 11, 11, 10000, now),
        ]
        result = simulate_exact(path, 10, quotes)
        self.assertEqual(result.profit, 1)
        self.assertEqual(result.holdings["ex"], 100)
        self.assertEqual(result.gold, 45000)
        self.assertAlmostEqual(result.profit_per_100k_gold, 100000 / 45000)
        self.assertAlmostEqual(result.profit_per_million_gold, 1000000 / 45000)

    def test_insufficient_stock_or_inventory_rejected(self):
        now = int(time.time())
        with self.assertRaisesRegex(ValueError, "库存"):
            simulate_exact(("a", "b", "a"), 10, [
                LiveQuote("a", "b", 10, 3, 2, 1, now),
                LiveQuote("b", "a", 3, 11, 11, 1, now),
            ])
        with self.assertRaisesRegex(ValueError, "没有足够"):
            simulate_exact(("a", "b", "a"), 10, [
                LiveQuote("a", "b", 10, 3, 3, 1, now),
                LiveQuote("b", "a", 4, 11, 11, 1, now),
            ])

    def test_merged_decimal_ocr_is_not_treated_as_trade_ratio(self):
        parsed = _parse(["1:1.251:7.29", "40", "50"], [0.9, 1.0, 1.0])
        self.assertEqual(parsed["ratios"], [])
        self.assertIn(40, parsed["numbers"])


if __name__ == "__main__":
    unittest.main()

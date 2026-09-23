import json
import time
import unittest
from pathlib import Path
from fractions import Fraction

from poe2arb.core import HistoricalEdge, LiveQuote, find_candidates, historical_edges, latest_market_hour, simulate_exact, simulate_reference_units
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

    def test_historical_routes_use_one_latest_hour_and_executed_ratio_bounds(self):
        def market(a, b, hour, low, high, volume_a=100, volume_b=100):
            return {
                "league": "Test", "_hour_id": hour, "market_pair": [a, b],
                "volume_traded": {a: volume_a, b: volume_b},
                "lowest_ratio": {a: 1, b: low},
                "highest_ratio": {a: 1, b: high},
            }

        rows = [
            market("a", "b", 100, 100, 110),  # Older apparent bargain.
            market("b", "c", 200, 2, 2),
            {**market("c", "a", 200, 1, 1),
             "lowest_ratio": {"c": 5, "a": 1},
             "highest_ratio": {"c": 5, "a": 1}},
            market("a", "b", 200, 3, 4, volume_a=1000, volume_b=2000),
            market("a", "c", 100, 1, 1),
        ]
        self.assertEqual(latest_market_hour(rows, "Test"), 200)
        edges = historical_edges(rows, "Test")
        self.assertEqual(edges["a", "b"].rate, Fraction(2) * Fraction(50, 51))
        self.assertEqual(edges["b", "a"].rate, Fraction(25, 51))
        self.assertEqual(edges["a", "b"].source_volume, 1000)
        self.assertNotIn(("a", "c"), edges)

    def test_one_sided_or_scattered_hourly_prices_are_not_candidates(self):
        base = {"league": "Test", "_hour_id": 100, "market_pair": ["a", "b"],
                "volume_traded": {"a": 100, "b": 200}}
        one_sided = {**base, "lowest_ratio": {"a": 1, "b": 2},
                     "highest_ratio": {"a": 1, "b": 2}}
        scattered = {**base, "lowest_ratio": {"a": 1, "b": 2},
                     "highest_ratio": {"a": 1, "b": 4}}
        self.assertEqual(historical_edges([one_sided], "Test"), {})
        self.assertEqual(historical_edges([scattered], "Test"), {})

    def test_missing_executed_ratio_is_not_replaced_with_volume_ratio(self):
        rows = [{"league": "Test", "_hour_id": 100, "market_pair": ["a", "b"],
                 "volume_traded": {"a": 1, "b": 1000}}]
        self.assertEqual(historical_edges(rows, "Test"), {})

    def test_extreme_hourly_disagreement_is_not_recommended(self):
        edges = {(a, b): HistoricalEdge(a, b, rate, 100, 1) for a, b, rate in (
            ("a", "b", Fraction(2)), ("b", "c", Fraction(2)),
            ("c", "a", Fraction(1)),
        )}
        self.assertEqual(find_candidates(edges, "a", lengths=(3,)), [])

    def test_hourly_reference_floors_each_leg_and_checks_hourly_volume(self):
        edges = {(a, b): HistoricalEdge(a, b, rate, volume, 1)
                 for a, b, rate, volume in (
                     ("a", "b", Fraction(1, 3), 20),
                     ("b", "c", Fraction(3), 10),
                     ("c", "a", Fraction(6, 5), 20),
                 )}
        path = ("a", "b", "c", "a")
        self.assertIsNone(simulate_reference_units(path, 2, edges))
        self.assertEqual(simulate_reference_units(path, 3, edges), 3)
        self.assertEqual(simulate_reference_units(path, 6, edges), 7)
        self.assertIsNone(simulate_reference_units(path, 21, edges))

    def test_real_hourly_example_integer_sequence(self):
        # GGG's 2026-09-23 05:00 UTC Forbidden Rites volumes for the route
        # shown by lilmarket; 90 scraps become 1 divine, 82 vaals, 546 exalted,
        # then 129 scraps when every trade is rounded down.
        amounts = (("scrap", "div", 15720, 180),
                   ("div", "vaal", 728, 60902),
                   ("vaal", "ex", 8820, 59916),
                   ("ex", "scrap", 67536, 16332))
        edges = {(a, b): HistoricalEdge(a, b, Fraction(received, paid) * Fraction(50, 51), paid, 1)
                 for a, b, paid, received in amounts}
        self.assertEqual(simulate_reference_units(("scrap", "div", "vaal", "ex", "scrap"),
                                                  90, edges), 129)

    def test_cycle_search_includes_items_outside_top_volume_rank(self):
        edges = {(a, b): HistoricalEdge(a, b, rate, 10, 1)
                 for a, b, rate in (("base", "rare", Fraction(2)),
                                    ("rare", "middle", Fraction(1)),
                                    ("middle", "base", Fraction(3, 5)))}
        for index in range(40):
            name = f"unrelated-{index}"
            edges[name, "sink"] = HistoricalEdge(name, "sink", Fraction(1), 1000, 1)
        self.assertTrue(any(row.path == ("base", "rare", "middle", "base")
                            for row in find_candidates(edges, "base", lengths=(3,))))

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

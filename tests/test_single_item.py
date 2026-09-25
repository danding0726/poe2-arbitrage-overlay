import unittest
from fractions import Fraction

from poe2arb.single_item import CHAOS, DIVINE, EXALTED, Quote, evaluate, indicative_paths
from poe2arb.core import HistoricalEdge


ITEM = "test-item"


class SingleItemTests(unittest.TestCase):
    def test_cross_currency_exact_cycle_and_gold(self):
        quotes = (
            Quote(EXALTED, ITEM, 30, 2, 10, 100, 100),
            Quote(ITEM, DIVINE, 3, 1, 4, 101, 200),
            Quote(DIVINE, EXALTED, 1, 55, 200, 102, 300),
        )
        result = evaluate(*quotes, now=110)
        self.assertEqual((result.buy_lots, result.sell_lots, result.convert_lots), (3, 2, 2))
        self.assertEqual((result.start_amount, result.item_amount, result.final_amount), (90, 6, 110))
        self.assertEqual(result.profit, 20)
        self.assertEqual(result.max_rounds, 1)
        self.assertIsNone(result.exact_gold)  # Three enlarged orders need their own fees.

    def test_exact_one_order_each_has_gold_efficiency(self):
        quotes = (
            Quote(EXALTED, ITEM, 30, 2, 10, 100, 100),
            Quote(ITEM, DIVINE, 2, 1, 4, 101, 200),
            Quote(DIVINE, EXALTED, 1, 35, 200, 102, 300),
        )
        result = evaluate(*quotes, now=110)
        self.assertEqual(result.profit, 5)
        self.assertEqual(result.exact_gold, 600)
        self.assertEqual(result.profit_per_million_gold, Fraction(25_000, 3))

    def test_expired_and_insufficient_stock_never_produce_profit(self):
        buy = Quote(EXALTED, ITEM, 30, 2, 10, 100)
        sell = Quote(ITEM, DIVINE, 2, 1, 4, 101)
        convert = Quote(DIVINE, EXALTED, 1, 35, 200, 102)
        with self.assertRaisesRegex(ValueError, "45 秒"):
            evaluate(buy, sell, convert, now=146)
        with self.assertRaisesRegex(ValueError, "库存"):
            evaluate(buy, Quote(ITEM, DIVINE, 2, 1, 0, 101), convert, now=110)

    def test_historical_paths_are_only_directions(self):
        edges = {}
        for a, b, rate in ((EXALTED, ITEM, Fraction(1, 30)),
                           (ITEM, DIVINE, Fraction(1, 2)),
                           (DIVINE, EXALTED, Fraction(70))):
            edges[a, b] = HistoricalEdge(a, b, rate, 1000, 1)
        paths = indicative_paths(edges, ITEM)
        self.assertEqual(paths[0][:2], (EXALTED, DIVINE))
        self.assertGreater(paths[0][2], 0)


if __name__ == "__main__":
    unittest.main()

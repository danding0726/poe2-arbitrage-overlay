import unittest
from fractions import Fraction

from poe2arb.single_item import (CHAOS, DIVINE, EXALTED, Quote, evaluate,
                                 indicative_paths, item_unit_gold, normalize_quote_amounts)
from poe2arb.core import HistoricalEdge


ITEM = "test-item"


class SingleItemTests(unittest.TestCase):
    def test_capture_gold_converts_to_one_item_only_when_exact(self):
        self.assertEqual(item_unit_gold(5500, 11), 500)
        self.assertIsNone(item_unit_gold(5501, 11))
        self.assertIsNone(item_unit_gold(100, 0))

    def test_decimal_ratio_normalizes_but_integer_order_stays_exact(self):
        self.assertEqual(normalize_quote_amounts("1.5", "1"), (3, 2, True))
        self.assertEqual(normalize_quote_amounts("25.5", "11"), (51, 22, True))
        self.assertEqual(normalize_quote_amounts("30", "2"), (30, 2, False))
        self.assertEqual(normalize_quote_amounts("1,000.5", "2"), (2001, 4, True))
        for pay, receive in (("0.0", "1"), ("-1", "2"), ("1e2", "1"), ("1.2.3", "2")):
            with self.assertRaises(ValueError):
                normalize_quote_amounts(pay, receive)

    def test_single_order_shortage_is_valid_reference_but_not_executable(self):
        buy = Quote(EXALTED, ITEM, 3, 2, 1, 100)
        sell = Quote(ITEM, DIVINE, 2, 1, 1, 101)
        convert = Quote(DIVINE, EXALTED, 1, 4, 4, 102)
        buy.validate()
        with self.assertRaisesRegex(ValueError, "库存"):
            evaluate(buy, sell, convert, now=110)
        self.assertEqual(evaluate(buy, sell, convert, now=110, require_stock=False).max_rounds, 0)

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
        self.assertEqual(result.exact_gold, 6 * 100 + 2 * 800 + 110 * 120)

    def test_exact_one_order_each_has_gold_efficiency(self):
        quotes = (
            Quote(EXALTED, ITEM, 30, 2, 10, 100, 100),
            Quote(ITEM, DIVINE, 2, 1, 4, 101, 200),
            Quote(DIVINE, EXALTED, 1, 35, 200, 102, 300),
        )
        result = evaluate(*quotes, now=110)
        self.assertEqual(result.profit, 5)
        self.assertEqual(result.exact_gold, 5_200)
        self.assertEqual(result.profit_per_million_gold, Fraction(12_500, 13))

    def test_core_fee_uses_received_units_even_for_multiple_lots(self):
        quotes = (
            Quote(EXALTED, ITEM, 90, 6, 6, 100, 500),
            Quote(ITEM, DIVINE, 2, 1, 3, 101, 999),
            Quote(DIVINE, EXALTED, 3, 100, 100, 102, 999),
        )
        result = evaluate(*quotes, now=110)
        self.assertEqual((result.buy_lots, result.sell_lots, result.convert_lots), (1, 3, 1))
        self.assertEqual(result.exact_gold, 6 * 500 + 3 * 800 + 100 * 120)

    def test_chaos_fee_is_160_per_received_chaos(self):
        quotes = (
            Quote(EXALTED, ITEM, 30, 2, 2, 100, 100),
            Quote(ITEM, CHAOS, 2, 10, 10, 101),
            Quote(CHAOS, EXALTED, 10, 35, 35, 102),
        )
        result = evaluate(*quotes, now=110)
        self.assertEqual(result.exact_gold, 2 * 100 + 10 * 160 + 35 * 120)

    def test_complete_prices_still_produce_theoretical_result_when_lots_exceed_stock(self):
        quotes = (
            Quote(EXALTED, ITEM, 30, 2, 2, 100),
            Quote(ITEM, DIVINE, 3, 1, 1, 101),
            Quote(DIVINE, EXALTED, 1, 55, 55, 102),
        )
        with self.assertRaisesRegex(ValueError, "库存"):
            evaluate(*quotes, now=110)
        estimate = evaluate(*quotes, now=110, require_stock=False)
        self.assertEqual(estimate.profit, 20)
        self.assertEqual(estimate.max_rounds, 0)

    def test_integer_scaling_explains_large_lot_requirement(self):
        quotes = (
            Quote(CHAOS, ITEM, 25, 11, 11, 100),
            Quote(ITEM, DIVINE, 9, 2, 62, 101),
            Quote(DIVINE, CHAOS, 8, 61, 3434, 102),
        )
        estimate = evaluate(*quotes, now=110, require_stock=False)
        self.assertEqual((estimate.buy_lots, estimate.sell_lots, estimate.convert_lots),
                         (36, 44, 11))
        self.assertEqual((estimate.start_amount, estimate.item_amount,
                          estimate.exit_amount, estimate.final_amount), (900, 396, 88, 671))
        self.assertEqual(estimate.profit, -229)
        self.assertEqual(estimate.max_rounds, 0)

    def test_expired_and_insufficient_stock_never_produce_profit(self):
        buy = Quote(EXALTED, ITEM, 30, 2, 10, 100)
        sell = Quote(ITEM, DIVINE, 2, 1, 4, 101)
        convert = Quote(DIVINE, EXALTED, 1, 35, 200, 102)
        with self.assertRaisesRegex(ValueError, "3 分钟"):
            evaluate(buy, sell, convert, now=281)
        estimate = evaluate(buy, sell, convert, now=281, require_fresh=False)
        self.assertFalse(estimate.is_fresh)
        self.assertEqual(estimate.profit, 5)
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

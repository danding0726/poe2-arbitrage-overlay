import unittest
from fractions import Fraction

from poe2arb.depth import Book, Level, captured_book, effective_levels, estimate_depth
from poe2arb.single_item import CHAOS, DIVINE, Quote

ITEM = "test-item"


class DepthTests(unittest.TestCase):
    def test_captured_ladder_keeps_selected_order_as_authoritative_first_level(self):
        selected = Quote(CHAOS, ITEM, 25, 11, 11, 100, 5500)
        ladder = {"levels": [
            {"pay": 25, "receive": 11, "stock": 11, "ratio": "11:25", "confidence": 0.99},
            {"pay": 30, "receive": 10, "stock": 400, "ratio": "10:30", "confidence": 0.98},
        ]}
        book = captured_book(selected, ladder)
        self.assertEqual(book.levels[0], Level(25, 11, 11, "11:25"))
        self.assertEqual(len(book.levels), 2)
        self.assertIsNone(captured_book(selected, {"levels": [
            {"pay": 10, "receive": 10, "stock": 11, "confidence": 0.99},
            ladder["levels"][1],
        ]}))

    def test_stock_mode_does_not_double_count_cumulative_levels(self):
        book = Book(CHAOS, ITEM, (Level(25, 11, 11), Level(30, 10, 62)), 100)
        self.assertEqual(sum(level.stock for level in effective_levels(book, "per_level")), 73)
        self.assertEqual(sum(level.stock for level in effective_levels(book, "cumulative")), 62)

    def test_later_level_enables_estimated_complete_conversion(self):
        buy = Book(CHAOS, ITEM, (Level(25, 11, 11), Level(30, 10, 400)), 100)
        sell = Book(ITEM, DIVINE, (Level(9, 2, 62),), 101)
        convert = Book(DIVINE, CHAOS, (Level(8, 61, 3434),), 102)
        plan = estimate_depth(buy, sell, convert)
        self.assertIsNotNone(plan)
        self.assertTrue(plan.used_extra_levels)
        self.assertGreaterEqual(plan.buy.levels_used, 2)
        self.assertGreater(plan.convert.received, 0)
        self.assertLess(plan.profit, 0)

    def test_profitable_route_can_require_a_second_price_level(self):
        buy = Book(CHAOS, ITEM, (Level(2, 1, 1), Level(3, 1, 2)), 100)
        sell = Book(ITEM, DIVINE, (Level(2, 1, 10),), 101)
        convert = Book(DIVINE, CHAOS, (Level(1, 10, 100),), 102)
        plan = estimate_depth(buy, sell, convert)
        self.assertTrue(plan.used_extra_levels)
        self.assertEqual((plan.buy.spent, plan.buy.received, plan.convert.received), (8, 3, 10))
        self.assertEqual(plan.profit, 2)
        self.assertEqual(plan.estimated_profit, 7)

    def test_remaining_item_and_exit_currency_count_toward_estimated_roi(self):
        buy = Book(CHAOS, ITEM, (Level(10, 3, 3),), 100)
        sell = Book(ITEM, DIVINE, (Level(2, 3, 3),), 101)
        convert = Book(DIVINE, CHAOS, (Level(2, 5, 5),), 102)
        plan = estimate_depth(buy, sell, convert)
        self.assertIsNotNone(plan)
        self.assertEqual((plan.remaining_item, plan.remaining_exit), (1, 1))
        self.assertEqual(plan.profit, -5)  # Only the whole-lot return is realized.
        self.assertEqual(plan.remaining_value, Fraction(25, 4))
        self.assertEqual(plan.estimated_profit, Fraction(5, 4))
        self.assertEqual(plan.estimated_roi, Fraction(1, 8))

    def test_zero_remainder_keeps_realized_and_estimated_profit_equal(self):
        buy = Book(CHAOS, ITEM, (Level(2, 1, 1), Level(3, 1, 1)), 100)
        sell = Book(ITEM, DIVINE, (Level(2, 1, 10),), 101)
        convert = Book(DIVINE, CHAOS, (Level(1, 10, 100),), 102)
        plan = estimate_depth(buy, sell, convert)
        self.assertEqual(plan.remaining_value, 0)
        self.assertEqual(plan.estimated_profit, plan.profit)
        self.assertEqual(plan.estimated_roi, plan.roi)


if __name__ == "__main__":
    unittest.main()

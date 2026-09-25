import unittest
from fractions import Fraction

from poe2arb.single_item import CHAOS, DIVINE, EXALTED, Quote
from poe2arb.trade_log import Trade, summarize_trades, trade_gold


ITEM = "test-item"
LEAGUE = "test-league"


def trade(role, source, target, pay, receive, gold, at=100):
    return Trade(LEAGUE, ITEM, EXALTED, DIVINE, role,
                 source, target, pay, receive, gold, at)


class TradeLogTests(unittest.TestCase):
    def test_completed_cycle_is_actual_profit_without_principal_entry(self):
        trades = [
            trade("买入", EXALTED, ITEM, 30, 2, 100),
            trade("卖出", ITEM, DIVINE, 2, 1, 200),
            trade("换回", DIVINE, EXALTED, 1, 35, 300),
        ]
        summary = summarize_trades(trades, EXALTED, {}, 110)
        self.assertTrue(summary.settled)
        self.assertEqual(summary.profit, Fraction(5))
        self.assertEqual(summary.balances, {EXALTED: 5})
        self.assertEqual(summary.gold, 5_100)
        self.assertEqual(trade_gold(trades[1]), 800)
        self.assertEqual(trade_gold(trades[2]), 4_200)

    def test_open_position_uses_directed_quotes_and_reports_age(self):
        trades = [trade("买入", EXALTED, ITEM, 30, 2, 100)]
        quotes = {
            (ITEM, DIVINE): Quote(ITEM, DIVINE, 2, 1, None, 99),
            (DIVINE, EXALTED): Quote(DIVINE, EXALTED, 1, 35, None, 90),
        }
        summary = summarize_trades(trades, EXALTED, quotes, 110)
        self.assertFalse(summary.settled)
        self.assertEqual(summary.profit, Fraction(5))
        self.assertEqual(summary.oldest_quote_age, 20)
        self.assertEqual(summary.gold, 100)
        quotes[(DIVINE, EXALTED)] = Quote(DIVINE, EXALTED, 1, 40, None, 111)
        updated = summarize_trades(trades, EXALTED, quotes, 300)
        self.assertEqual(updated.profit, Fraction(10))
        self.assertEqual(updated.oldest_quote_age, 201)
        self.assertIsNone(summarize_trades(trades, EXALTED, {}, 110).profit)

    def test_uncovered_nonbase_spending_cannot_be_called_profit(self):
        trades = [trade("换回", DIVINE, EXALTED, 1, 35, None)]
        summary = summarize_trades(trades, EXALTED, {}, 110)
        self.assertIsNone(summary.profit)
        self.assertEqual(summary.gold, 4_200)

    def test_later_item_unit_fee_fills_missing_trade_gold(self):
        buy = trade("买入", EXALTED, ITEM, 30, 2, None)
        self.assertIsNone(trade_gold(buy))
        self.assertEqual(trade_gold(buy, {ITEM: 50}), 100)
        self.assertIsNone(summarize_trades([buy], EXALTED, {}, 110).gold)
        self.assertEqual(summarize_trades([buy], EXALTED, {}, 110,
                                          {ITEM: 50}).gold, 100)

    def test_actual_quantities_must_be_positive_integers(self):
        with self.assertRaisesRegex(ValueError, "正整数"):
            trade("买入", EXALTED, ITEM, 0, 2, 100).validate()
        with self.assertRaisesRegex(ValueError, "方向"):
            trade("卖出", EXALTED, ITEM, 2, 1, 100).validate()


if __name__ == "__main__":
    unittest.main()

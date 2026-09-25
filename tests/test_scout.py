import unittest
from fractions import Fraction

from poe2arb.scout import MAX_AGE_SECONDS, normalize_pairs, scout_edges, snapshot_age


class ScoutReferenceTests(unittest.TestCase):
    def test_normalizes_books_and_filters_weak_liquidity(self):
        def book(volume, stock_b, traded_a=100, traded_b=100):
            return {
                "CurrencyOne": {"BaseItemTypeId": "a"},
                "CurrencyTwo": {"BaseItemTypeId": "b"},
                "CurrencyOneData": {"RelativePrice": "1", "VolumeTraded": traded_a,
                                    "StockValue": 2000},
                "CurrencyTwoData": {"RelativePrice": "0.5", "VolumeTraded": traded_b,
                                    "StockValue": stock_b},
                "Volume": volume,
            }

        pairs = normalize_pairs([book(500, 2000), book(200_000, 2000)])
        self.assertEqual(len(pairs), 1)
        edges = scout_edges({"pairs": pairs})
        self.assertEqual(edges["a", "b"].rate, Fraction(2))
        self.assertNotIn(("a", "b"), scout_edges({"pairs": normalize_pairs([book(200_000, 0)])}))
        self.assertEqual(scout_edges({"pairs": normalize_pairs([book(99_999, 2000)])}), {})
        self.assertEqual(scout_edges({"pairs": normalize_pairs([book(200_000, 2000, 99)])}), {})
        self.assertEqual(scout_edges({"pairs": normalize_pairs([book(200_000, 2000, traded_b=99)])}), {})
        self.assertEqual(snapshot_age({"epoch": 100}, now=160), 60)
        self.assertEqual(MAX_AGE_SECONDS, 15 * 60)


if __name__ == "__main__":
    unittest.main()

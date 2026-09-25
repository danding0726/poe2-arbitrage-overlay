import unittest
from fractions import Fraction

from poe2arb.scout import normalize_pairs, scout_edges, snapshot_age


class ScoutReferenceTests(unittest.TestCase):
    def test_normalizes_books_and_filters_weak_liquidity(self):
        def book(volume, stock_b):
            return {
                "CurrencyOne": {"BaseItemTypeId": "a"},
                "CurrencyTwo": {"BaseItemTypeId": "b"},
                "CurrencyOneData": {"RelativePrice": "1", "VolumeTraded": 100,
                                    "StockValue": 2000},
                "CurrencyTwoData": {"RelativePrice": "0.5", "VolumeTraded": 100,
                                    "StockValue": stock_b},
                "Volume": volume,
            }

        pairs = normalize_pairs([book(500, 2000), book(20_000, 2000)])
        self.assertEqual(len(pairs), 1)
        edges = scout_edges({"pairs": pairs})
        self.assertEqual(edges["a", "b"].rate, Fraction(2))
        self.assertNotIn(("a", "b"), scout_edges({"pairs": normalize_pairs([book(20_000, 0)])}))
        self.assertEqual(snapshot_age({"epoch": 100}, now=160), 60)


if __name__ == "__main__":
    unittest.main()

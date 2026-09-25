import unittest
from fractions import Fraction

from poe2arb.core import historical_edges, latest_market_hour


def market(hour, a="a", b="b", paid=100, received=200, low=2, high=3):
    return {
        "league": "Test", "_hour_id": hour, "market_pair": [a, b],
        "volume_traded": {a: paid, b: received},
        "lowest_ratio": {a: 1, b: low},
        "highest_ratio": {a: 1, b: high},
    }


class HistoricalReferenceTests(unittest.TestCase):
    def test_uses_latest_hour_and_keeps_both_directions_indicative(self):
        rows = [market(100, received=1000), market(200)]
        self.assertEqual(latest_market_hour(rows, "Test"), 200)
        edges = historical_edges(rows, "Test")
        self.assertEqual(edges["a", "b"].rate, Fraction(2) * Fraction(50, 51))
        self.assertEqual(edges["b", "a"].rate, Fraction(1, 2) * Fraction(50, 51))
        self.assertEqual(edges["a", "b"].source_volume, 100)

    def test_rejects_missing_or_unreliable_market(self):
        one_sided = market(100, low=2, high=2)
        scattered = market(100, low=2, high=4)
        missing = market(100)
        del missing["lowest_ratio"]
        self.assertEqual(historical_edges([one_sided], "Test"), {})
        self.assertEqual(historical_edges([scattered], "Test"), {})
        self.assertEqual(historical_edges([missing], "Test"), {})


if __name__ == "__main__":
    unittest.main()

import time
import unittest
from fractions import Fraction
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from poe2arb.core import Candidate, LiveQuote
from poe2arb.workflow import CHAOS, DIVINE, EXALTED, CORE, QuoteBook
from poe2arb.ocr import read_stock_region
from poe2arb.roundtrip import analyze_round_trip, analyze_sized_cycle, find_single_item_candidates
from poe2arb.core import historical_edges
from poe2arb.scout import normalize_pairs, scout_candidates, scout_edges, snapshot_age


class WorkflowTests(unittest.TestCase):
    def test_scout_independent_books_liquidity_and_duplicate_choice(self):
        def book(a, b, pa, pb, volume, stock_a=2000, stock_b=2000):
            return {
                "CurrencyOne": {"BaseItemTypeId": a},
                "CurrencyTwo": {"BaseItemTypeId": b},
                "CurrencyOneData": {"RelativePrice": pa, "VolumeTraded": 100,
                                    "StockValue": stock_a},
                "CurrencyTwoData": {"RelativePrice": pb, "VolumeTraded": 100,
                                    "StockValue": stock_b},
                "Volume": volume,
            }
        item = "Metadata/Items/Currency/TestItem"
        pairs = normalize_pairs([
            book(EXALTED, item, "1", "1", "500"),
            book(EXALTED, item, "1", "0.5", "20000.0"),
            book(item, CHAOS, "2", "1", "20000"),
            book(CHAOS, EXALTED, "0.3", "1", "20000"),
            book(item, DIVINE, "1", "1", "20000", stock_b=0),
        ])
        self.assertEqual(len(pairs), 4)
        self.assertEqual(next(row for row in pairs if {row["a"], row["b"]} == {EXALTED, item})["volume"], 20000)
        snapshot = {"epoch": 100, "pairs": pairs}
        edges = scout_edges(snapshot)
        self.assertNotIn((item, DIVINE), edges)
        self.assertEqual(edges[EXALTED, item].rate, Fraction(2))
        self.assertEqual(snapshot_age(snapshot, now=160), 60)
        self.assertTrue(any(row.path == (EXALTED, item, CHAOS, EXALTED)
                            for row in scout_candidates(snapshot)))

    def test_two_direction_round_trip_uses_whole_lots_and_stock(self):
        now = int(time.time())
        forward = LiveQuote("ex", "chaos", 3, 4, 20, 100, now)
        reverse = LiveQuote("chaos", "ex", 5, 4, 20, 200, now)
        result = analyze_round_trip(forward, reverse, now=now)
        self.assertEqual((result.first_lots, result.second_lots), (5, 4))
        self.assertEqual((result.start, result.finish, result.profit), (15, 16, 1))
        self.assertEqual(result.estimated_gold, 1300)
        self.assertAlmostEqual(result.profit_per_million_gold, 1_000_000 / 1300)
        with self.assertRaisesRegex(ValueError, "过期"):
            analyze_round_trip(forward, reverse, now=now + 46)
        with self.assertRaisesRegex(ValueError, "间隔"):
            analyze_round_trip(forward, LiveQuote("chaos", "ex", 5, 4, 20, 200, now - 26), now=now)
        with self.assertRaisesRegex(ValueError, "库存"):
            analyze_round_trip(LiveQuote("ex", "chaos", 3, 4, 19, 100, now), reverse, now=now)

    def test_single_item_cross_currency_round_trip(self):
        now = int(time.time())
        quotes = [
            LiveQuote("ex", "item", 10, 2, 6, 100, now),
            LiveQuote("item", "div", 3, 1, 2, 200, now),
            LiveQuote("div", "ex", 1, 20, 40, 300, now),
        ]
        result = analyze_sized_cycle(quotes, now=now)
        self.assertEqual(result.all_lots, (3, 2, 2))
        self.assertEqual((result.start, result.finish, result.profit), (30, 40, 10))
        self.assertEqual(result.estimated_gold, 1300)
        self.assertAlmostEqual(result.profit_per_million_gold, 10_000_000 / 1300)

    def test_historical_single_item_candidates_require_two_core_books(self):
        from pathlib import Path
        import json
        demo = json.loads((Path(__file__).parents[1] / "poe2arb" / "demo.json").read_text())
        edges = historical_edges(demo["markets"], "演示联赛")
        candidates = find_single_item_candidates(edges)
        self.assertTrue(candidates)
        self.assertTrue(all(row.path[0] in CORE and row.path[2] in CORE and row.path[1] not in CORE
                            for row in candidates))

    def test_stock_ocr_rejects_ratio_and_accepts_single_integer(self):
        stream = BytesIO()
        Image.new("RGB", (40, 30), "white").save(stream, format="PNG")
        for text, expected in (("40:50", None), ("50", 50)):
            fake = lambda image, t=text: SimpleNamespace(txts=[t], scores=[0.95])
            with patch("rapidocr.RapidOCR", return_value=fake):
                self.assertEqual(read_stock_region(stream.getvalue())["stock"], expected)

    def test_completed_pair_reused_by_multiple_routes_and_expires(self):
        now = int(time.time())
        book = QuoteBook()
        book.reset("league")
        first = Candidate((EXALTED, CHAOS, DIVINE, EXALTED), Fraction(1, 10), Fraction(100))
        second = Candidate((EXALTED, CHAOS, "other", EXALTED), Fraction(1, 20), Fraction(50))
        book.select(first)
        book.select(second)
        pair = (EXALTED, CHAOS)
        self.assertEqual(book.coverage(first), (0, 3))
        book.put(LiveQuote(*pair, 2, 5, 20, 100, now))
        self.assertEqual(book.coverage(first), (1, 3))
        self.assertEqual(book.coverage(second), (1, 3))
        self.assertNotIn(pair, book.pending())
        self.assertIsNone(book.get(pair, now=now + 181))
        book.reset("another league")
        self.assertIn(pair, book.pending())
        self.assertEqual(book.selected_routes, [])


if __name__ == "__main__":
    unittest.main()

import io
import unittest
from fractions import Fraction
from types import SimpleNamespace
from unittest.mock import patch

from poe2arb.ocr import (
    _ladder_quotes,
    _has_ladder_headers,
    _selected_order_from_detections,
    resolve_capture_order,
    read_exchange_panel,
)


def box(x, y, width=30, height=12):
    return [[x, y], [x + width, y], [x + width, y + height], [x, y + height]]


class ExchangeOcrTests(unittest.TestCase):
    def test_tooltip_stock_one_six_three_is_not_payment_163_or_gold_177(self):
        from PIL import Image

        lines = ["我需要的", "我擁有的", "1", "比率", "庫存", "1:26", "163", "177"]
        boxes = [box(50, 5), box(550, 5), box(150, 50),
                 box(300, 40), box(430, 40), box(300, 60),
                 box(430, 60, height=72), box(430, 150)]
        scores = [0.99] * len(lines)
        self.assertIsNone(_selected_order_from_detections(lines, scores, boxes))
        output = SimpleNamespace(txts=lines, scores=scores, boxes=boxes)
        stream = io.BytesIO()
        Image.new("RGB", (740, 185)).save(stream, format="PNG")
        with patch("rapidocr.RapidOCR") as factory:
            factory.return_value.return_value = output
            result = read_exchange_panel(stream.getvalue())
            self.assertIsNone(result["selected_order"])
            factory.return_value.assert_called_once()
        order = resolve_capture_order(result["selected_order"], {
            "pay": 26, "receive": 1, "stock": 1, "confidence": 0.99})
        self.assertEqual((order["pay"], order["receive"], order["gold"]), (26, 1, None))

    def test_three_stock_like_integers_on_different_rows_are_not_an_order(self):
        self.assertIsNone(_selected_order_from_detections(
            ["1", "163", "177"], [0.99] * 3,
            [box(150, 50), box(430, 80, height=60), box(430, 150)]))

    def test_order_ratio_without_tooltip_is_not_a_ladder(self):
        self.assertFalse(_has_ladder_headers(["1:50", "50", "1,000", "下訂單"], [0.99] * 4))
        self.assertTrue(_has_ladder_headers(["比率", "庫存", "1:50", "10"], [0.99] * 4))
        self.assertFalse(_has_ladder_headers(["比率", "庫存"], [0.99, 0.5]))
    def test_uses_coordinates_when_full_ocr_has_order_values(self):
        lines = ["我需要的", "1:6.90", "我拥有的", "10", "69", "1,600"]
        scores = [0.99, 0.98, 0.99, 0.96, 0.97, 0.95]
        boxes = [
            box(50, 5), box(300, 5), box(550, 5),
            box(500, 50), box(150, 50), box(330, 105),
        ]

        order = _selected_order_from_detections(lines, scores, boxes)

        self.assertEqual(order, {
            "receive": 69,
            "pay": 10,
            "gold": 1600,
            "confidence": 0.95,
        })

    def test_rejects_ambiguous_extra_integer(self):
        lines = ["10", "69", "1,600", "42"]
        scores = [0.99] * 4
        boxes = [box(500, 50), box(150, 50), box(330, 105), box(50, 130)]

        self.assertIsNone(_selected_order_from_detections(lines, scores, boxes))

    def test_binds_amounts_to_labels_not_ocr_output_order(self):
        lines = ["我拥有的", "10", "我需要的", "69", "1,600"]
        scores = [0.97] * len(lines)
        boxes = [
            box(550, 5), box(500, 50), box(50, 5), box(150, 50), box(330, 105),
        ]

        order = _selected_order_from_detections(lines, scores, boxes)

        self.assertEqual(order, {
            "receive": 69,
            "pay": 10,
            "gold": 1600,
            "confidence": 0.97,
        })

    def test_uses_fixed_game_sides_when_labels_are_not_recognized(self):
        lines = ["10", "69", "1,600"]
        scores = [0.99] * len(lines)
        boxes = [box(500, 50), box(150, 50), box(330, 105)]

        order = _selected_order_from_detections(lines, scores, boxes)

        self.assertEqual((order["receive"], order["pay"]), (69, 10))

    def test_reads_first_ratio_and_stock_row_from_hovered_ladder(self):
        lines = ["市场比率", "1:6.83", "6", "比率", "库存", "1:6.83", "6", "1:6.85", "60"]
        scores = [0.99] * len(lines)
        boxes = [
            box(200, 0), box(400, 20), box(300, 40), box(180, 60), box(350, 60),
            box(200, 90), box(350, 90), box(200, 120), box(350, 120),
        ]

        quote = _ladder_quotes(lines, scores, boxes)[0]

        self.assertEqual(quote, {
            "receive": 6,
            "pay": 41,
            "stock": 6,
            "ratio": "1:6.83",
            "unit_receive": Fraction(100, 683),
            "confidence": 0.99,
        })

    def test_skips_incomplete_first_row_instead_of_using_an_unrelated_integer(self):
        lines = ["比率", "库存", "6", "1:6.83", "1:6.85", "60"]
        scores = [0.98] * len(lines)
        boxes = [
            box(200, 0, height=40), box(500, 0, height=40),
            box(80, 30, height=40), box(250, 70, height=50),
            box(250, 150, height=50), box(500, 150, height=50),
        ]

        quote = _ladder_quotes(lines, scores, boxes)[0]

        self.assertEqual(quote["ratio"], "1:6.85")
        self.assertEqual(quote["stock"], 60)
        self.assertLess(quote["receive"], quote["stock"])

    def test_reads_reference_screenshot_first_complete_ladder_row(self):
        lines = ["比率", "库存", "7.67:1", "23", "7.65:1", "199", "7.65:1", "283"]
        scores = [0.99] * len(lines)
        boxes = [
            box(10, 0, 50, 18), box(155, 0, 50, 18),
            box(10, 35, 85, 20), box(165, 35, 40, 20),
            box(10, 65, 85, 20), box(165, 65, 50, 20),
            box(10, 95, 85, 20), box(165, 95, 50, 20),
        ]

        quote = _ladder_quotes(lines, scores, boxes)[0]

        self.assertEqual((quote["pay"], quote["receive"], quote["stock"]), (3, 23, 23))
        self.assertEqual(quote["unit_receive"], Fraction(767, 100))

    def test_reads_several_stock_rows_without_double_using_a_stock(self):
        lines = ["7.67:1", "23", "7.65:1", "199", "7.64:1", "573"]
        boxes = [box(x, y, 85 if x == 10 else 50, 20)
                 for y in (35, 65, 95) for x in (10, 165)]
        levels = _ladder_quotes(lines, [0.99] * 6, boxes)
        self.assertEqual([level["stock"] for level in levels], [23, 199, 573])
        self.assertEqual([level["ratio"] for level in levels],
                         ["7.67:1", "7.65:1", "7.64:1"])

    def test_more_stock_does_not_increase_the_minimum_trade(self):
        from poe2arb.ocr import _smallest_whole_lot

        self.assertEqual(_smallest_whole_lot("1", "6.83", 6), (41, 6))
        self.assertEqual(_smallest_whole_lot("1", "6.83", 600), (41, 6))

    def test_exalted_fifty_first_level_keeps_stock_separate_from_order_size(self):
        lines = ["1:50", "10", "1:51", "4", "1:52", "6", "1:56.67", "3",
                 "1:60", "31", "<1:60", "182"]
        boxes = [box(x, y, 85 if x == 10 else 50, 20)
                 for y in (35, 65, 95, 125, 155, 185) for x in (10, 165)]
        levels = _ladder_quotes(lines, [0.99] * len(lines), boxes)
        self.assertEqual((levels[0]["pay"], levels[0]["receive"], levels[0]["stock"]),
                         (50, 1, 10))
        self.assertEqual([level["stock"] for level in levels], [10, 4, 6, 3, 31])

    def test_rejects_stock_column_merged_across_three_rows(self):
        lines = ["1:50", "10", "1:51", "463", "1:52", "1:56.67", "1:60", "31"]
        boxes = [box(10, 35, 85, 20), box(165, 35, 50, 20),
                 box(10, 65, 85, 20), box(165, 65, 50, 80),
                 box(10, 95, 85, 20), box(10, 125, 85, 20),
                 box(10, 155, 85, 20), box(165, 155, 50, 20)]
        levels = _ladder_quotes(lines, [0.99] * len(lines), boxes)
        self.assertEqual([level["stock"] for level in levels], [10, 31])

    def test_insufficient_stock_cannot_be_confirmed_as_an_executable_quote(self):
        from poe2arb.ocr import _smallest_whole_lot

        self.assertIsNone(_smallest_whole_lot("7.67", "1", 10))
        self.assertIsNone(resolve_capture_order(None, {
            "pay": None, "receive": None, "ratio": "7.67:1", "confidence": 0.99,
        }))

    def test_selected_order_is_not_scaled_again_by_a_false_ladder_quote(self):
        selected = {
            "receive": 1,
            "pay": 65,
            "gold": 160,
            "confidence": 0.99,
        }
        false_ladder = {
            "receive": 65,
            "pay": 4225,
            "stock": 65,
            "ratio": "1:65",
            "unit_receive": Fraction(1, 65),
            "confidence": 0.99,
        }

        self.assertIs(resolve_capture_order(selected, false_ladder), selected)

    def test_ladder_quote_remains_a_fallback_when_selected_order_is_missing(self):
        ladder = {
            "receive": 6,
            "pay": 41,
            "stock": 6,
            "ratio": "1:6.83",
            "unit_receive": Fraction(100, 683),
            "confidence": 0.98,
        }

        self.assertEqual(resolve_capture_order(None, ladder), {
            "receive": 6,
            "pay": 41,
            "gold": None,
            "confidence": 0.98,
        })


if __name__ == "__main__":
    unittest.main()

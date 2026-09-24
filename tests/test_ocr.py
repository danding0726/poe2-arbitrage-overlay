import unittest

import numpy as np

from poe2arb.ocr import (
    _best_ladder_quote,
    _selected_order_from_detections,
)


def box(x, y, width=30, height=12):
    return [[x, y], [x + width, y], [x + width, y + height], [x, y + height]]


class ExchangeOcrTests(unittest.TestCase):
    def test_uses_coordinates_when_full_ocr_has_order_values(self):
        lines = ["我需要的", "1:6.90", "我拥有的", "10", "69", "1,600"]
        scores = [0.99, 0.98, 0.99, 0.96, 0.97, 0.95]
        boxes = np.asarray([
            box(50, 5), box(300, 5), box(550, 5),
            box(500, 50), box(150, 50), box(330, 105),
        ])

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
        boxes = np.asarray([
            box(550, 5), box(500, 50), box(50, 5), box(150, 50), box(330, 105),
        ])

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
        boxes = np.asarray([box(500, 50), box(150, 50), box(330, 105)])

        order = _selected_order_from_detections(lines, scores, boxes)

        self.assertEqual((order["receive"], order["pay"]), (69, 10))

    def test_reads_first_ratio_and_stock_row_from_hovered_ladder(self):
        lines = ["市场比率", "1:6.83", "6", "比率", "库存", "1:6.83", "6", "1:6.85", "60"]
        scores = [0.99] * len(lines)
        boxes = np.asarray([
            box(200, 0), box(400, 20), box(300, 40), box(180, 60), box(350, 60),
            box(200, 90), box(350, 90), box(200, 120), box(350, 120),
        ])

        quote = _best_ladder_quote(lines, scores, boxes)

        self.assertEqual(quote, {
            "receive": 6,
            "pay": 41,
            "stock": 6,
            "ratio": "1:6.83",
            "confidence": 0.99,
        })

    def test_uses_filled_amount_when_first_stock_detection_is_missing(self):
        lines = ["比率", "库存", "6", "1:6.83", "1:6.85", "60"]
        scores = [0.98] * len(lines)
        boxes = np.asarray([
            box(200, 0, height=40), box(500, 0, height=40),
            box(80, 30, height=40), box(250, 70, height=50),
            box(250, 150, height=50), box(500, 150, height=50),
        ])

        quote = _best_ladder_quote(lines, scores, boxes)

        self.assertEqual((quote["receive"], quote["pay"], quote["stock"]), (6, 41, 6))


if __name__ == "__main__":
    unittest.main()

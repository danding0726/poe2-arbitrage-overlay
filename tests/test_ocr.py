import unittest

import numpy as np

from poe2arb.ocr import _selected_order_from_detections, _selected_order_from_text


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

    def test_falls_back_to_labeled_text_when_boxes_do_not_align(self):
        lines = ["我需要的", "1:6.90", "我拥有的", "10", "1:6.90", "69", "1,600"]
        scores = [0.97] * len(lines)

        order = _selected_order_from_text(lines, scores)

        self.assertEqual(order, {
            "receive": 10,
            "pay": 69,
            "gold": 1600,
            "confidence": 0.97,
        })

    def test_text_fallback_requires_both_labels(self):
        self.assertIsNone(_selected_order_from_text(
            ["我需要的", "10", "69", "1,600"],
            [0.99, 0.99, 0.99, 0.99],
        ))


if __name__ == "__main__":
    unittest.main()

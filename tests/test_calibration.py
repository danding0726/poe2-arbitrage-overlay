import unittest

from poe2arb.calibration import calibration_guides, capture_preset


class CalibrationPresetTests(unittest.TestCase):
    def test_known_layouts_and_unknown_resolution(self):
        self.assertEqual(capture_preset((1920, 1080))["roi"]["bbox"],
                         [588, 160, 1328, 345])
        self.assertEqual(capture_preset((2560, 1440))["stock_roi"]["bbox"],
                         [1135, 300, 1430, 525])
        self.assertEqual(capture_preset((3440, 1440)), {})

    def test_preset_is_not_mutable_by_callers(self):
        first = capture_preset((2560, 1440))
        first["stock_roi"]["bbox"][0] = 0
        self.assertEqual(capture_preset((2560, 1440))["stock_roi"]["bbox"][0], 1135)

    def test_both_reference_boxes_are_drawn_without_creating_new_ocr_presets(self):
        guides = calibration_guides((1920, 1080))
        self.assertEqual(guides["roi"], [588, 160, 1328, 345])
        self.assertEqual(guides["stock_roi"], [851, 225, 1073, 394])
        self.assertNotIn("stock_roi", capture_preset((1920, 1080)))
        self.assertEqual(calibration_guides((2560, 1440))["stock_roi"],
                         [1135, 300, 1430, 525])
        self.assertEqual(calibration_guides((3440, 1440)), {})


if __name__ == "__main__":
    unittest.main()

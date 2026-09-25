import unittest

from poe2arb.calibration import capture_preset


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


if __name__ == "__main__":
    unittest.main()

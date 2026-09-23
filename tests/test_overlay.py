import unittest

from poe2arb.overlay import parse_integer_field


class OverlayInputTests(unittest.TestCase):
    def test_parses_integer_with_grouping_separator(self):
        self.assertEqual(parse_integer_field("1,600", "金币费用", minimum=0), 1600)

    def test_empty_field_has_specific_message(self):
        with self.assertRaisesRegex(ValueError, "第 2 跳可获数量未填写"):
            parse_integer_field("", "第 2 跳可获数量")

    def test_gold_allows_zero_but_not_negative_values(self):
        self.assertEqual(parse_integer_field("0", "金币费用", minimum=0), 0)
        with self.assertRaisesRegex(ValueError, "金币费用不能为负数"):
            parse_integer_field("-1", "金币费用", minimum=0)


if __name__ == "__main__":
    unittest.main()

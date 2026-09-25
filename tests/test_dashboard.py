import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from poe2arb.dashboard import Dashboard


class DashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_single_page_recalculates_from_three_independent_quotes(self):
        with tempfile.TemporaryDirectory() as directory:
            with (patch("poe2arb.dashboard.settings_path", return_value=Path(directory) / "settings.json"),
                  patch("poe2arb.dashboard.QTimer.singleShot")):
                window = Dashboard()
                try:
                    item = "Metadata/Items/Currency/CurrencyCorrupt"
                    window.item_select.setCurrentIndex(window.item_select.findData(item))
                    for role, values in (
                        ("买入", (30, 2, 10, 100)),
                        ("卖出", (2, 1, 4, 200)),
                        ("换回", (1, 35, 200, 300)),
                    ):
                        row = window.rows[role]
                        for field, value in zip((row.pay, row.receive, row.stock, row.gold), values):
                            field.setText(str(value))
                        window._save_row(row)
                    self.assertIn("+5", window.profit.text())
                    self.assertIn("每百万金币收益", window.metrics)
                    self.assertFalse(window.rows["买入"].source_icon.pixmap().isNull())
                    self.assertFalse(window.rows["买入"].target_icon.pixmap().isNull())
                    stored_pair = window.rows["买入"].pair
                    self.assertEqual(window.quotes[stored_pair].gold, 100)
                finally:
                    window.close()
                restored = Dashboard()
                try:
                    self.assertEqual(restored.quotes[stored_pair].gold, 100)
                finally:
                    restored.close()


if __name__ == "__main__":
    unittest.main()

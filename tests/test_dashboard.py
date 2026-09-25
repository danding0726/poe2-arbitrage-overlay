import json
import os
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QBoxLayout, QPushButton, QScrollArea
from PySide6.QtTest import QTest

from poe2arb.catalog import catalog
from poe2arb.core import HistoricalEdge
from poe2arb.dashboard import Dashboard, readable_rate
from poe2arb.single_item import CHAOS, DIVINE, EXALTED, Quote


class DashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_small_rates_use_one_target_unit(self):
        self.assertEqual(readable_rate("崇高石", "神圣石", Fraction(2, 30)),
                         "15 崇高石 ≈ 1 神圣石")
        self.assertEqual(readable_rate("崇高石", "神圣石", Fraction(1, 2500)),
                         "2,500 崇高石 ≈ 1 神圣石")
        self.assertEqual(readable_rate("神圣石", "崇高石", Fraction(30, 2)),
                         "1 神圣石 ≈ 15 崇高石")

    def test_old_order_gold_migrates_to_per_item_and_old_core_gold_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            item = "Metadata/Items/Currency/CurrencyCorrupt"
            path.write_text(json.dumps({"quotes": [
                {"source": EXALTED, "target": item, "pay": 30, "receive": 2,
                 "stock": 2, "observed_at": 100, "gold": 1000},
                {"source": DIVINE, "target": EXALTED, "pay": 1, "receive": 35,
                 "stock": 35, "observed_at": 100, "gold": 999},
            ]}), encoding="utf-8")
            with (patch("poe2arb.dashboard.settings_path", return_value=path),
                  patch("poe2arb.dashboard.QTimer.singleShot")):
                window = Dashboard()
                try:
                    self.assertEqual(window.quotes[(EXALTED, item)].gold, 500)
                    self.assertIsNone(window.quotes[(DIVINE, EXALTED)].gold)
                    self.assertEqual(json.loads(path.read_text(encoding="utf-8"))
                                     ["gold_per_item_version"], 1)
                finally:
                    window.close()

    def test_item_dropdown_ranks_historical_leads_and_recent_choices_persist(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            with (patch("poe2arb.dashboard.settings_path", return_value=path),
                  patch("poe2arb.dashboard.QTimer.singleShot")):
                window = Dashboard()
                try:
                    first, second = [item for item in catalog() if item not in
                                     (EXALTED, DIVINE, CHAOS)][:2]
                    edges = {
                        (EXALTED, first): HistoricalEdge(EXALTED, first, Fraction(2), 1000, 1),
                        (first, DIVINE): HistoricalEdge(first, DIVINE, Fraction(1), 1000, 1),
                        (EXALTED, second): HistoricalEdge(EXALTED, second, Fraction(3, 2), 1000, 1),
                        (second, DIVINE): HistoricalEdge(second, DIVINE, Fraction(1), 1000, 1),
                        (DIVINE, EXALTED): HistoricalEdge(DIVINE, EXALTED, Fraction(1), 1000, 1),
                    }
                    with patch.object(window, "_edges", return_value=edges):
                        window._update_suggestions()
                    self.assertEqual(window.item_select.itemData(1), first)
                    self.assertEqual(window.item_select.itemData(2), second)
                    window.item_select.setCurrentIndex(1)
                    self.assertEqual(window.selected_item, first)
                    window.select_item(second)
                    self.assertEqual(window.recent_items[:2], [second, first])
                    self.assertEqual(window.recent_buttons[0].property("recent_item"), second)
                finally:
                    window.close()
                restored = Dashboard()
                try:
                    self.assertEqual(restored.recent_items[:2], [second, first])
                    restored.recent_buttons[1].click()
                    self.assertEqual(restored.selected_item, first)
                finally:
                    restored.close()

    def test_layout_adapts_and_journal_explains_selected_exchange_leg(self):
        with tempfile.TemporaryDirectory() as directory:
            with (patch("poe2arb.dashboard.settings_path", return_value=Path(directory) / "settings.json"),
                  patch("poe2arb.dashboard.QTimer.singleShot")):
                window = Dashboard()
                try:
                    window.select_item("Metadata/Items/Currency/CurrencyCorrupt")
                    window.resize(1000, 800)
                    self.app.processEvents()
                    scroll = window.findChild(QScrollArea)
                    self.assertEqual(window.body_layout.direction(),
                                     QBoxLayout.Direction.TopToBottom)
                    self.assertEqual(scroll.horizontalScrollBar().maximum(), 0)
                    self.assertEqual(len({row.direction_host.width()
                                          for row in window.rows.values()}), 1)
                    for row in window.rows.values():
                        self.assertGreater(row.source_name.width(), 0)
                        self.assertEqual(row.arrow_label.width(), 18)
                        self.assertTrue(row.arrow_label.isVisible())
                        self.assertGreater(row.target_name.width(), 0)
                        self.assertTrue(row.source_name.text())
                        self.assertTrue(row.target_name.text())
                    self.assertIn("崇高石 → 瓦尔宝珠 → 神圣石 → 崇高石",
                                  window.trade_route.text())
                    window.trade_role.setCurrentText("卖出")
                    self.assertIn("卖出：支付 瓦尔宝珠 → 获得 神圣石",
                                  window.trade_leg.text())
                    window.resize(1320, 940)
                    self.app.processEvents()
                    self.assertEqual(window.body_layout.direction(),
                                     QBoxLayout.Direction.LeftToRight)
                    self.assertEqual(scroll.horizontalScrollBar().maximum(), 0)
                    self.assertEqual(len({row.direction_host.width()
                                          for row in window.rows.values()}), 1)
                    self.assertTrue(all(row.arrow_label.isVisible()
                                        for row in window.rows.values()))
                finally:
                    window.close()

    def test_manual_decimal_quote_updates_integer_fields_and_marks_shortage(self):
        with tempfile.TemporaryDirectory() as directory:
            with (patch("poe2arb.dashboard.settings_path", return_value=Path(directory) / "settings.json"),
                  patch("poe2arb.dashboard.QTimer.singleShot")):
                window = Dashboard()
                try:
                    window.select_item("Metadata/Items/Currency/CurrencyCorrupt")
                    row = window.rows["买入"]
                    for field, value in zip((row.pay, row.receive, row.stock, row.gold),
                                            ("1.5", "1", "1", "250")):
                        field.setText(value)
                    window._save_row(row)
                    self.assertEqual((row.pay.text(), row.receive.text(), row.gold.text()),
                                     ("3", "2", "250"))
                    self.assertEqual((window.quotes[row.pair].pay, window.quotes[row.pair].receive,
                                      window.quotes[row.pair].gold), (3, 2, 250))
                    self.assertIn("库存不足", row.age.text())
                    self.assertIn("每个物品金币", window.capture_hint.text())
                    window.start_select.setCurrentIndex(window.start_select.findData(CHAOS))
                    self.assertEqual(window.rows["买入"].gold.text(), "250")
                finally:
                    window.close()

    def test_core_decimal_quote_updates_card(self):
        with tempfile.TemporaryDirectory() as directory:
            with (patch("poe2arb.dashboard.settings_path", return_value=Path(directory) / "settings.json"),
                  patch("poe2arb.dashboard.QTimer.singleShot")):
                window = Dashboard()
                try:
                    card = window.rate_cards[0]
                    window.edit_core_rate(card.a, card.b)
                    for field, value in zip(card.fields, ("30.5", "2", "2")):
                        field.setText(value)
                    window.save_core_rate()
                    self.assertEqual([field.text() for field in card.fields], ["61", "4", "2"])
                    self.assertEqual(len(card.fields), 3)
                    self.assertEqual(card.directions[(card.a, card.b)].state.text(), "库存不足")
                finally:
                    window.close()

    def test_ignore_stock_calculates_without_stock_and_restores_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            with (patch("poe2arb.dashboard.settings_path", return_value=Path(directory) / "settings.json"),
                  patch("poe2arb.dashboard.QTimer.singleShot")):
                window = Dashboard()
                try:
                    window.select_item("Metadata/Items/Currency/CurrencyCorrupt")
                    window.ignore_stock.setChecked(True)
                    for role, values in (("买入", (30, 2)), ("卖出", (2, 1)),
                                         ("换回", (1, 35))):
                        row = window.rows[role]
                        row.pay.setText(str(values[0]))
                        row.receive.setText(str(values[1]))
                        window._save_row(row)
                        self.assertIsNone(window.quotes[row.pair].stock)
                    self.assertIn("+5", window.profit.text())
                    self.assertIn("忽略库存", window.result_state.text())
                    self.assertEqual(window.metrics["最多完整轮数"].text(), "未校验")
                    self.assertIn("不代表可成交", window.result_detail.text())
                    window.ignore_stock.setChecked(False)
                    self.assertIn("库存未录入", window.result_state.text())
                    window.ignore_stock.setChecked(True)
                finally:
                    window.close()
                restored = Dashboard()
                try:
                    self.assertTrue(restored.ignore_stock.isChecked())
                    self.assertIn("忽略库存", restored.result_state.text())
                finally:
                    restored.close()

    def test_actual_trade_journal_values_open_and_completed_cycle(self):
        with tempfile.TemporaryDirectory() as directory:
            with (patch("poe2arb.dashboard.settings_path", return_value=Path(directory) / "settings.json"),
                  patch("poe2arb.dashboard.QTimer.singleShot")):
                window = Dashboard()
                try:
                    window.league.addItem("测试联赛")
                    window.league.setCurrentText("测试联赛")
                    window.select_item("Metadata/Items/Currency/CurrencyCorrupt")
                    buy = window.rows["买入"]
                    buy.pay.setText("30")
                    buy.receive.setText("2")
                    buy.gold.setText("50")
                    window._save_row(buy)
                    for role, values in (("买入", (30, 2)),
                                         ("卖出", (2, 1)),
                                         ("换回", (1, 35))):
                        window.trade_role.setCurrentText(role)
                        self.assertEqual(len(window.trade_fields), 2)
                        for field, value in zip(window.trade_fields, values):
                            field.setText(str(value))
                        window._record_trade()
                    self.assertEqual(window.trade_list.count(), 3)
                    self.assertIn("已换回净收益 +5", window.trade_profit.text())
                    self.assertIn("金币支出 5,100", window.trade_balances.text())
                    self.assertIn("每百万金币 净收益", window.trade_balances.text())
                    window.trade_list.setCurrentRow(0)
                    window._remove_trade()
                    self.assertIn("持仓暂无法估值", window.trade_profit.text())
                finally:
                    window.close()
                restored = Dashboard()
                try:
                    self.assertEqual(len(restored.trades), 2)
                finally:
                    restored.close()

    def test_single_page_recalculates_from_three_independent_quotes(self):
        with tempfile.TemporaryDirectory() as directory:
            with (patch("poe2arb.dashboard.settings_path", return_value=Path(directory) / "settings.json"),
                  patch("poe2arb.dashboard.QTimer.singleShot")):
                window = Dashboard()
                try:
                    item = "Metadata/Items/Currency/CurrencyCorrupt"
                    window.select_item(item)
                    self.assertIn("已录入 0/3", window.result_state.text())
                    for role, values in (
                        ("买入", (30, 2, 10, 100)),
                        ("卖出", (2, 1, 4, 200)),
                        ("换回", (1, 35, 200, 300)),
                    ):
                        row = window.rows[role]
                        for field, value in zip((row.pay, row.receive, row.stock, row.gold), values):
                            field.setText(str(value))
                        window._save_row(row)
                        if role == "买入":
                            self.assertIn("已录入 1/3", window.result_state.text())
                    self.assertIn("+5", window.profit.text())
                    self.assertIn("每百万金币收益", window.metrics)
                    self.assertFalse(window.rows["买入"].source_icon.pixmap().isNull())
                    self.assertFalse(window.rows["买入"].target_icon.pixmap().isNull())
                    stored_pair = window.rows["买入"].pair
                    self.assertEqual(window.quotes[stored_pair].gold, 100)
                    window.refresh(now=window.quotes[stored_pair].observed_at + 181)
                    self.assertIn("+5", window.profit.text())
                    self.assertIn("参考测算", window.result_state.text())
                    self.assertIn("不代表当前可成交", window.result_detail.text())
                finally:
                    window.close()
                restored = Dashboard()
                try:
                    self.assertEqual(restored.quotes[stored_pair].gold, 100)
                finally:
                    restored.close()

    def test_search_matches_english_and_core_rate_edits_in_card(self):
        with tempfile.TemporaryDirectory() as directory:
            with (patch("poe2arb.dashboard.settings_path", return_value=Path(directory) / "settings.json"),
                  patch("poe2arb.dashboard.QTimer.singleShot")):
                window = Dashboard()
                try:
                    window._filter_items("Vaal Orb")
                    self.assertGreater(window.item_results.count(), 0)
                    window._choose_first_match()
                    self.assertEqual(window.selected_item, "Metadata/Items/Currency/CurrencyCorrupt")
                    card = window.rate_cards[0]
                    card.directions[(card.a, card.b)].findChild(QPushButton, "rateAction").click()
                    self.assertTrue(card.editor.isVisible())
                    self.assertFalse(window.rate_cards[1].editor.isVisible())
                    for field, value in zip(card.fields, (30, 2, 10)):
                        field.setText(str(value))
                    window.save_core_rate()
                    self.assertFalse(card.editor.isVisible())
                    self.assertEqual(window.quotes[(card.a, card.b)].pay, 30)
                    direction = card.directions[(card.a, card.b)]
                    self.assertIn("付 30 崇高石", direction.amount.text())
                    self.assertIn("得 2 神圣石", direction.amount.text())
                    self.assertIn("15 崇高石 ≈ 1 神圣石", direction.meta.text())
                    self.assertEqual(direction.state.text(), "近期报价")
                    window.refresh(now=window.quotes[(card.a, card.b)].observed_at + 181)
                    self.assertEqual(direction.state.text(), "需复核")
                    card.directions[(card.a, card.b)].findChild(QPushButton, "rateAction").click()
                    window.capture_role = "核心"
                    window._capture_done((
                        {"selected_order": {"pay": 30, "receive": 2, "gold": 100}},
                        {"stock": 10, "best_quote": None, "levels": [
                            {"pay": 30, "receive": 2, "stock": 10,
                             "ratio": "2:30", "confidence": 0.99},
                            {"pay": 35, "receive": 2, "stock": 20,
                             "ratio": "2:35", "confidence": 0.99},
                        ]},
                    ))
                    window.save_core_rate()
                    self.assertEqual(len(window.depth_books[(card.a, card.b)].levels), 2)
                finally:
                    window.close()

    def test_complete_manual_values_calculate_without_enter_even_when_old(self):
        with tempfile.TemporaryDirectory() as directory:
            with (patch("poe2arb.dashboard.settings_path", return_value=Path(directory) / "settings.json"),
                  patch("poe2arb.dashboard.QTimer.singleShot")):
                window = Dashboard()
                try:
                    window.select_item("Metadata/Items/Currency/CurrencyCorrupt")
                    for role, values in (
                        ("买入", (30, 2, 10)),
                        ("卖出", (2, 1, 4)),
                        ("换回", (1, 35, 200)),
                    ):
                        row = window.rows[role]
                        for field, value in zip((row.pay, row.receive, row.stock), values):
                            QTest.keyClicks(field, str(value))
                    QTest.qWait(450)
                    self.assertIn("+5", window.profit.text())
                    oldest = min(quote.observed_at for quote in window.quotes.values())
                    window.refresh(now=oldest + 600)
                    self.assertIn("+5", window.profit.text())
                    self.assertIn("参考测算", window.result_state.text())
                    for role, (pay, receive, stock) in (
                        ("买入", (30, 2, 2)),
                        ("卖出", (3, 1, 1)),
                        ("换回", (1, 55, 55)),
                    ):
                        source, target = window.rows[role].pair
                        window.quotes[(source, target)] = Quote(
                            source, target, pay, receive, stock, oldest,
                        )
                    window.refresh(now=oldest + 600)
                    self.assertIn("+20", window.profit.text())
                    self.assertIn("库存不足", window.result_state.text())
                    self.assertIn("理论价差", window.result_detail.text())
                    self.assertIn("买入需 6", window.result_detail.text())
                    self.assertIn("当前可获 2", window.result_detail.text())
                finally:
                    window.close()

    def test_background_league_refresh_keeps_entered_quotes(self):
        with tempfile.TemporaryDirectory() as directory:
            with (patch("poe2arb.dashboard.settings_path", return_value=Path(directory) / "settings.json"),
                  patch("poe2arb.dashboard.QTimer.singleShot")):
                window = Dashboard()
                try:
                    window.league.clear()
                    window.league.addItems(["旧联赛", "新联赛"])
                    window.league.setCurrentText("旧联赛")
                    pair = window.rate_cards[0].a, window.rate_cards[0].b
                    window.quotes[pair] = Quote(*pair, 30, 2, 10, 100)
                    with (patch("poe2arb.dashboard.leagues", return_value=["旧联赛", "新联赛"]),
                          patch.object(window, "_update_suggestions")):
                        window._scout_done({"league": "新联赛"})
                    self.assertEqual(window.league.currentText(), "旧联赛")
                    self.assertIn(pair, window.quotes)
                finally:
                    window.close()

    def test_captured_depth_is_saved_used_and_restored(self):
        with tempfile.TemporaryDirectory() as directory:
            with (patch("poe2arb.dashboard.settings_path", return_value=Path(directory) / "settings.json"),
                  patch("poe2arb.dashboard.QTimer.singleShot")):
                window = Dashboard()
                try:
                    window.select_item("Metadata/Items/Currency/CurrencyCorrupt")
                    window.start_select.setCurrentIndex(window.start_select.findData(CHAOS))
                    window.exit_select.setCurrentIndex(window.exit_select.findData(DIVINE))
                    window.capture_role = "买入"
                    window._capture_done((
                        {"selected_order": {"pay": 25, "receive": 11, "gold": 5500}},
                        {"stock": 11, "best_quote": None, "levels": [
                            {"pay": 25, "receive": 11, "stock": 11,
                             "ratio": "11:25", "confidence": 0.99},
                            {"pay": 30, "receive": 10, "stock": 400,
                             "ratio": "10:30", "confidence": 0.99},
                        ]},
                    ))
                    buy_pair = window.rows["买入"].pair
                    self.assertIn(buy_pair, window.pending_depth)
                    self.assertEqual(window.rows["买入"].gold.text(), "500")
                    window.rows["买入"].save.click()
                    self.assertEqual(len(window.depth_books[buy_pair].levels), 2)
                    for role, values in (("卖出", (9, 2, 62, 1600)),
                                         ("换回", (8, 61, 3434, 8480))):
                        row = window.rows[role]
                        for field, value in zip((row.pay, row.receive, row.stock, row.gold), values):
                            field.setText(str(value))
                        window._save_row(row)
                    self.assertIn("多档预估", window.result_state.text())
                    self.assertIn("2档×", window.result_detail.text())
                    self.assertIn("剩余持仓估值", window.profit_caption.text())
                    self.assertIn("仅为估值", window.result_detail.text())
                    self.assertIn("含剩余估值", window.metrics["收益率"].text())
                    self.assertIn("已读 2 档", window.rows["买入"].depth_info.text())
                    window.stock_mode.setCurrentIndex(1)
                    self.assertIn("累计", window.result_detail.text())
                    window.ignore_stock.setChecked(True)
                    self.assertIn("理论价差 · 忽略库存", window.result_state.text())
                    self.assertNotIn("多档预估", window.result_state.text())
                    window.ignore_stock.setChecked(False)
                finally:
                    window.close()
                restored = Dashboard()
                try:
                    self.assertEqual(len(restored.depth_books[buy_pair].levels), 2)
                    self.assertEqual(restored.stock_mode.currentData(), "cumulative")
                    self.assertIn("多档预估", restored.result_state.text())
                finally:
                    restored.close()

    def test_capture_without_ladder_does_not_reuse_old_stock(self):
        with tempfile.TemporaryDirectory() as directory:
            with (patch("poe2arb.dashboard.settings_path", return_value=Path(directory) / "settings.json"),
                  patch("poe2arb.dashboard.QTimer.singleShot")):
                window = Dashboard()
                try:
                    window.select_item("Metadata/Items/Currency/CurrencyCorrupt")
                    row = window.rows["买入"]
                    row.stock.setText("99")
                    window.capture_role = "买入"
                    window._capture_done((
                        {"selected_order": {"pay": 20, "receive": 2, "gold": 300}},
                        None,
                    ))
                    self.assertEqual(row.stock.text(), "")
                    self.assertIn("校准", window.capture_hint.text())
                finally:
                    window.close()


if __name__ == "__main__":
    unittest.main()

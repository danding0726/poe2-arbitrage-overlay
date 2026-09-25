"""Single-window dashboard for one item's cross-currency spread."""

from __future__ import annotations

import io
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

from PySide6.QtCore import QPoint, QRect, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication, QComboBox, QCompleter, QFrame, QGridLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget,
)

from .catalog import catalog, item_icon, item_name, item_pixmap
from .core import historical_edges, latest_market_hour
from .data import app_data_dir, leagues, load_snapshot, sync_recent
from .scout import MAX_AGE_SECONDS, fetch_scout_snapshot, load_scout_snapshot, scout_edges, snapshot_age
from .single_item import CHAOS, CORE, DIVINE, EXALTED, Quote, evaluate, indicative_paths


def settings_path() -> Path:
    return app_data_dir() / "dashboard_settings.json"


def read_settings() -> dict:
    try:
        return json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


class Job(QThread):
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, function, *args):
        super().__init__()
        self.function = function
        self.args = args

    def run(self):
        try:
            self.done.emit(self.function(*self.args))
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class RegionSelector(QWidget):
    selected = Signal(object)
    cancelled = Signal()

    def __init__(self, image: QPixmap, label: str):
        super().__init__()
        self.image = image
        self.label = label
        self.start: QPoint | None = None
        self.end: QPoint | None = None
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.showFullScreen()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.drawPixmap(self.rect(), self.image)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 90))
        painter.setPen(QColor("#ffffff"))
        painter.drawText(25, 38, f"框选{self.label} · Esc 取消")
        if self.start and self.end:
            rect = QRect(self.start, self.end).normalized()
            painter.setPen(QPen(QColor("#6bd6a0"), 3))
            painter.drawRect(rect)

    def mousePressEvent(self, event):
        self.start = event.position().toPoint()
        self.end = self.start

    def mouseMoveEvent(self, event):
        if self.start:
            self.end = event.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, event):
        if not self.start:
            return
        rect = QRect(self.start, event.position().toPoint()).normalized()
        if rect.width() < 30 or rect.height() < 20:
            self.cancelled.emit()
            self.close()
            return
        sx, sy = self.image.width() / self.width(), self.image.height() / self.height()
        bbox = [round(rect.left() * sx), round(rect.top() * sy),
                round(rect.right() * sx), round(rect.bottom() * sy)]
        self.selected.emit({"bbox": bbox, "screen": [self.image.width(), self.image.height()]})
        self.close()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.cancelled.emit()
            self.close()


class RateCard(QFrame):
    selected = Signal(str, str)

    def __init__(self, a: str, b: str):
        super().__init__()
        self.a, self.b = a, b
        self.setObjectName("rateCard")
        layout = QVBoxLayout(self)
        layout.setSpacing(5)
        title = QHBoxLayout()
        for index, item in enumerate((a, b)):
            if index:
                title.addWidget(QLabel("⇄"))
            icon = QLabel()
            icon.setPixmap(item_pixmap(item, 22))
            title.addWidget(icon)
            title.addWidget(QLabel(item_name(item)))
        title.addStretch()
        layout.addLayout(title)
        self.forward = QPushButton()
        self.reverse = QPushButton()
        self.forward.clicked.connect(lambda: self.selected.emit(a, b))
        self.reverse.clicked.connect(lambda: self.selected.emit(b, a))
        layout.addWidget(self.forward)
        layout.addWidget(self.reverse)

    def refresh(self, quotes: dict, references: dict, now: int):
        for button, pair in ((self.forward, (self.a, self.b)),
                             (self.reverse, (self.b, self.a))):
            quote = quotes.get(pair)
            direction = f"{item_name(pair[0])} → {item_name(pair[1])}"
            if quote:
                age = max(0, now - quote.observed_at)
                button.setText(f"{direction}   {quote.pay}:{quote.receive}  ·  {age}s")
                button.setProperty("stale", age > 45)
            else:
                edge = references.get(pair)
                fallback = f"历史参考 ≈{float(edge.rate):.3g}" if edge else "待录入"
                button.setText(f"{direction}   {fallback}")
                button.setProperty("stale", True)
            button.style().unpolish(button)
            button.style().polish(button)


class QuoteRow(QFrame):
    requested = Signal(str)
    changed = Signal()

    def __init__(self, role: str):
        super().__init__()
        self.role = role
        self.pair: tuple[str, str] | None = None
        self.setObjectName("quoteRow")
        grid = QGridLayout(self)
        grid.setContentsMargins(10, 9, 10, 9)
        grid.setHorizontalSpacing(8)
        self.title = QLabel(role)
        self.title.setObjectName("rowTitle")
        grid.addWidget(self.title, 0, 0, 1, 2)
        direction_layout = QHBoxLayout()
        direction_layout.setContentsMargins(0, 0, 0, 0)
        direction_layout.setSpacing(5)
        self.source_icon = QLabel()
        self.target_icon = QLabel()
        self.direction = QLabel("选择物品后显示方向")
        self.direction.setObjectName("direction")
        direction_layout.addWidget(self.source_icon)
        direction_layout.addWidget(self.direction)
        direction_layout.addWidget(self.target_icon)
        direction_layout.addStretch()
        direction_host = QWidget()
        direction_host.setLayout(direction_layout)
        grid.addWidget(direction_host, 1, 0, 1, 2)
        self.pay = QLineEdit()
        self.receive = QLineEdit()
        self.stock = QLineEdit()
        self.gold = QLineEdit()
        for col, (title, field) in enumerate((
            ("支付", self.pay), ("获得", self.receive),
            ("可获库存", self.stock), ("金币", self.gold),
        )):
            field.setPlaceholderText(title)
            field.setFixedWidth(92)
            field.editingFinished.connect(self.changed.emit)
            grid.addWidget(field, 1, col + 2)
        self.age = QLabel("待录入")
        self.age.setObjectName("muted")
        grid.addWidget(self.age, 0, 2, 1, 3)
        self.capture = QPushButton("截图读取")
        self.capture.clicked.connect(lambda: self.requested.emit(self.role))
        grid.addWidget(self.capture, 1, 6)

    def set_pair(self, pair: tuple[str, str] | None, quote: Quote | None):
        self.pair = pair
        if pair:
            self.direction.setText(f"{item_name(pair[0])}  →  {item_name(pair[1])}")
            self.direction.setToolTip("游戏右侧「我拥有的」→ 左侧「我需要的」")
            self.source_icon.setPixmap(item_pixmap(pair[0], 22))
            self.target_icon.setPixmap(item_pixmap(pair[1], 22))
        else:
            self.direction.setText("先选择物品与交易方向")
            self.source_icon.clear()
            self.target_icon.clear()
        amounts = (quote.pay, quote.receive, quote.stock, quote.gold) if quote else (None,) * 4
        for field, value in zip((self.pay, self.receive, self.stock, self.gold), amounts):
            field.blockSignals(True)
            field.setText(str(value) if value is not None else "")
            field.blockSignals(False)
        self.update_age(quote, int(time.time()))

    def update_age(self, quote: Quote | None, now: int):
        if quote is None:
            self.age.setText("待录入 · 按当前游戏订单填写")
        else:
            age = max(0, now - quote.observed_at)
            self.age.setText(f"{age} 秒前 · {'已过期' if age > 45 else '有效'}")


class Dashboard(QWidget):
    def __init__(self):
        super().__init__()
        self.settings = read_settings()
        self.snapshot = load_snapshot()
        self.scout_snapshot = load_scout_snapshot()
        self.quotes: dict[tuple[str, str], Quote] = self._restore_quotes()
        self.jobs: list[Job] = []
        self._closing = False
        self.capture_role: str | None = None
        self.region_selector: RegionSelector | None = None
        self.selected_item: str | None = None
        self._build()
        self._style()
        self._load_leagues()
        self._restore_selection()
        self.refresh()
        self.resize(1220, 755)
        self.setMinimumSize(1080, 660)
        self.setWindowTitle("PoE2 单物品价差助手")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint)
        self.show()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1000)
        QTimer.singleShot(100, self.update_history)
        QTimer.singleShot(300, self.update_scout)

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        root = QVBoxLayout()
        root.setContentsMargins(18, 15, 18, 16)
        root.setSpacing(12)
        panel = QFrame()
        panel.setObjectName("panel")
        panel.setLayout(root)
        outer.addWidget(panel)

        header = QHBoxLayout()
        heading = QLabel("◆  PoE2 单物品价差助手")
        heading.setObjectName("heading")
        header.addWidget(heading)
        self.league = QComboBox()
        self.league.setMinimumWidth(170)
        self.league.currentIndexChanged.connect(self._league_changed)
        header.addStretch()
        header.addWidget(QLabel("联赛"))
        header.addWidget(self.league)
        self.status = QLabel("正在加载行情…")
        self.status.setObjectName("muted")
        header.addWidget(self.status)
        refresh = QPushButton("刷新历史线索")
        refresh.clicked.connect(self.update_history)
        header.addWidget(refresh)
        root.addLayout(header)

        market_head = QHBoxLayout()
        market_head.addWidget(self._section("核心通货汇率"))
        market_head.addStretch()
        hint = QLabel("当前游戏报价优先 · 历史值仅供参考 · 45 秒有效")
        hint.setObjectName("muted")
        market_head.addWidget(hint)
        root.addLayout(market_head)
        market = QHBoxLayout()
        market.setSpacing(9)
        self.rate_cards = [RateCard(EXALTED, DIVINE), RateCard(EXALTED, CHAOS), RateCard(DIVINE, CHAOS)]
        for card in self.rate_cards:
            card.selected.connect(self.edit_core_rate)
            market.addWidget(card, 1)
        root.addLayout(market)

        search = QHBoxLayout()
        search.addWidget(self._section("交易物品"))
        self.item_select = QComboBox()
        self.item_select.setEditable(True)
        self.item_select.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.item_select.setMinimumWidth(310)
        self.item_select.addItem("搜索或选择物品…", None)
        for item_id in sorted(catalog(), key=item_name):
            if item_id not in CORE:
                self.item_select.addItem(item_icon(item_id), item_name(item_id), item_id)
        self.item_select.completer().setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self.item_select.completer().setFilterMode(Qt.MatchFlag.MatchContains)
        self.item_select.currentIndexChanged.connect(self._item_changed)
        search.addWidget(self.item_select, 2)
        search.addWidget(QLabel("历史建议"))
        self.suggestion = QComboBox()
        self.suggestion.setMinimumWidth(280)
        self.suggestion.currentIndexChanged.connect(self._suggestion_changed)
        search.addWidget(self.suggestion, 2)
        root.addLayout(search)

        route = QHBoxLayout()
        route.addWidget(QLabel("买入使用"))
        self.start_select = QComboBox()
        self.exit_select = QComboBox()
        for selector in (self.start_select, self.exit_select):
            for item in CORE:
                selector.addItem(item_icon(item), item_name(item), item)
            selector.currentIndexChanged.connect(self._route_changed)
        route.addWidget(self.start_select)
        route.addWidget(QLabel("卖出换得"))
        route.addWidget(self.exit_select)
        self.reference = QLabel("历史数据只用于推荐检查方向")
        self.reference.setObjectName("muted")
        route.addWidget(self.reference, 1)
        root.addLayout(route)

        body = QHBoxLayout()
        body.setSpacing(12)
        left = QVBoxLayout()
        left.addWidget(self._section("当前订单"))
        self.rows = {role: QuoteRow(role) for role in ("买入", "卖出", "换回")}
        for row in self.rows.values():
            row.requested.connect(self.capture_quote)
            row.changed.connect(self._quote_changed)
            left.addWidget(row)
        self.capture_hint = QLabel("选择一个方向后，可手动填写订单或点击「截图读取」；金币按游戏显示的准确订单填写。")
        self.capture_hint.setWordWrap(True)
        self.capture_hint.setObjectName("muted")
        left.addWidget(self.capture_hint)
        left.addStretch()
        body.addLayout(left, 3)

        result = QFrame()
        result.setObjectName("resultCard")
        right = QVBoxLayout(result)
        right.setSpacing(10)
        right.addWidget(self._section("实时利润"))
        self.result_state = QLabel("等待报价")
        self.result_state.setObjectName("resultState")
        right.addWidget(self.result_state)
        self.profit = QLabel("—")
        self.profit.setObjectName("profit")
        right.addWidget(self.profit)
        self.profit_caption = QLabel("换回起始通货后计算")
        self.profit_caption.setObjectName("muted")
        right.addWidget(self.profit_caption)
        self.metrics = {}
        for label in ("最小一轮投入", "完整交易数量", "最多完整轮数", "收益率", "三笔金币", "每百万金币收益"):
            line = QHBoxLayout()
            name = QLabel(label)
            name.setObjectName("muted")
            value = QLabel("—")
            value.setAlignment(Qt.AlignmentFlag.AlignRight)
            line.addWidget(name)
            line.addWidget(value, 1)
            right.addLayout(line)
            self.metrics[label] = value
        self.result_detail = QLabel("输入买入、卖出和换回三条当前游戏报价后显示结果。")
        self.result_detail.setWordWrap(True)
        self.result_detail.setObjectName("detail")
        right.addWidget(self.result_detail)
        right.addStretch()
        body.addWidget(result, 2)
        root.addLayout(body, 1)

        footer = QHBoxLayout()
        self.core_editor = QFrame()
        self.core_editor.setObjectName("inlineEditor")
        editor = QHBoxLayout(self.core_editor)
        self.core_title = QLabel("核心汇率")
        editor.addWidget(self.core_title)
        self.core_fields = [QLineEdit() for _ in range(4)]
        for field, title in zip(self.core_fields, ("支付", "获得", "可获库存", "金币")):
            field.setPlaceholderText(title)
            field.setFixedWidth(90)
            editor.addWidget(field)
        capture = QPushButton("截图读取")
        capture.clicked.connect(self.capture_core_rate)
        editor.addWidget(capture)
        save = QPushButton("保存报价")
        save.clicked.connect(self.save_core_rate)
        editor.addWidget(save)
        self.core_editor.hide()
        footer.addWidget(self.core_editor)
        footer.addStretch()
        trade_calibration = QPushButton("校准订单区")
        trade_calibration.clicked.connect(lambda: self.calibrate("roi"))
        footer.addWidget(trade_calibration)
        stock_calibration = QPushButton("校准比率/库存")
        stock_calibration.clicked.connect(lambda: self.calibrate("stock_roi"))
        footer.addWidget(stock_calibration)
        footer.addWidget(QLabel("仅供手动交易参考 · 下单前核对游戏数量与金币"))
        root.addLayout(footer)

    @staticmethod
    def _section(title: str) -> QLabel:
        label = QLabel(title)
        label.setObjectName("section")
        return label

    def _style(self):
        self.setStyleSheet("""
            QWidget { background:#111918; color:#eaf3ed; font:13px 'Microsoft YaHei UI'; }
            QFrame#panel { background:#111918; }
            QLabel#heading { font-size:18px; font-weight:600; color:#f0f7f1; }
            QLabel#section { font-size:15px; font-weight:600; }
            QLabel#muted, QLabel#detail { color:#96aaa0; }
            QLabel#direction { font-size:14px; font-weight:600; }
            QLabel#rowTitle { color:#78d4a2; font-weight:600; }
            QPushButton, QComboBox, QLineEdit { background:#1c2925; border:1px solid #31463c;
                border-radius:7px; padding:7px; min-height:23px; }
            QPushButton:hover { background:#28513d; border-color:#5eae81; }
            QComboBox QAbstractItemView { background:#1c2925; selection-background-color:#315c44; }
            QFrame#rateCard, QFrame#quoteRow, QFrame#resultCard, QFrame#inlineEditor {
                background:#18231f; border:1px solid #30423a; border-radius:11px; }
            QFrame#rateCard QPushButton { text-align:left; border:0; background:transparent; padding:3px; }
            QFrame#rateCard QPushButton:hover { color:#79d8a5; }
            QFrame#rateCard QPushButton[stale="true"] { color:#82968a; }
            QFrame#resultCard { background:#1a2922; }
            QLabel#resultState { color:#88dba8; font-size:14px; }
            QLabel#profit { color:#89e4ae; font-size:29px; font-weight:600; }
        """)

    def _load_leagues(self):
        names = leagues(self.snapshot)
        if self.scout_snapshot and self.scout_snapshot.get("league") not in names:
            names.append(self.scout_snapshot["league"])
        current = self.league.currentText() or self.settings.get("league", "")
        if not current and self.scout_snapshot:
            current = self.scout_snapshot.get("league", "")
        self.league.blockSignals(True)
        self.league.clear()
        self.league.addItems(names)
        if current in names:
            self.league.setCurrentText(current)
        elif names:
            self.league.setCurrentIndex(0)
        self.league.blockSignals(False)

    def _restore_selection(self):
        item = self.settings.get("item")
        if item:
            index = self.item_select.findData(item)
            if index >= 0:
                self.item_select.setCurrentIndex(index)
        for selector, key in ((self.start_select, "start"), (self.exit_select, "exit")):
            index = selector.findData(self.settings.get(key))
            if index >= 0:
                selector.setCurrentIndex(index)
        if self.start_select.currentData() == self.exit_select.currentData():
            self.exit_select.setCurrentIndex(1)
        self._item_changed()

    def _save_settings(self):
        data = {"league": self.league.currentText(), "item": self.selected_item,
                "start": self.start_select.currentData(), "exit": self.exit_select.currentData(),
                "roi": self.settings.get("roi"), "stock_roi": self.settings.get("stock_roi"),
                "quotes": [asdict(quote) for quote in self.quotes.values()]}
        settings_path().write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def _restore_quotes(self) -> dict[tuple[str, str], Quote]:
        quotes = {}
        for row in self.settings.get("quotes", []):
            try:
                quote = Quote(**row)
                quote.validate()
                quotes[(quote.source, quote.target)] = quote
            except (TypeError, ValueError):
                continue
        return quotes

    def _job(self, function, callback, *args):
        job = Job(function, *args)
        self.jobs.append(job)
        job.done.connect(callback)
        job.failed.connect(lambda message: self.status.setText(message))
        job.finished.connect(lambda: self._job_finished(job))
        job.start()

    def _job_finished(self, job: Job):
        if job in self.jobs:
            self.jobs.remove(job)
        if self._closing and not self.jobs:
            QTimer.singleShot(0, self.close)

    def closeEvent(self, event):
        if self.jobs:
            self._closing = True
            self.hide()
            event.ignore()
            return
        super().closeEvent(event)

    def update_history(self):
        self.status.setText("正在更新小时历史…")
        self._job(sync_recent, self._history_done)

    def _history_done(self, snapshot):
        self.snapshot = snapshot
        self._load_leagues()
        self._update_suggestions()
        self.status.setText("小时历史已更新")

    def update_scout(self):
        league = self.league.currentText() or None
        self._job(fetch_scout_snapshot, self._scout_done, league)

    def _scout_done(self, snapshot):
        self.scout_snapshot = snapshot
        self._load_leagues()
        if not self.settings.get("league"):
            self.league.setCurrentText(snapshot["league"])
        self._update_suggestions()

    def _league_changed(self):
        self.quotes.clear()
        self._save_settings()
        self._update_suggestions()
        self._route_changed()

    def _edges(self):
        league = self.league.currentText()
        if (self.scout_snapshot and self.scout_snapshot.get("league") == league
                and 0 <= snapshot_age(self.scout_snapshot) <= MAX_AGE_SECONDS):
            return scout_edges(self.scout_snapshot)
        return historical_edges(self.snapshot.get("markets", []), league)

    def _update_suggestions(self):
        edges = self._edges()
        scores = []
        for item in {target for source, target in edges if target not in CORE}:
            paths = indicative_paths(edges, item)
            if paths and paths[0][2] > 0:
                scores.append((paths[0][2], item, paths[0][0], paths[0][1]))
        scores.sort(reverse=True)
        self.suggestion.blockSignals(True)
        self.suggestion.clear()
        self.suggestion.addItem("选择历史建议…", None)
        for gain, item, start, exit_currency in scores[:30]:
            self.suggestion.addItem(item_icon(item),
                f"{item_name(item)} · 线索 {float(gain):+.1%} · {item_name(start)}→{item_name(exit_currency)}",
                (item, start, exit_currency))
        self.suggestion.blockSignals(False)
        self._update_reference()

    def _suggestion_changed(self):
        selected = self.suggestion.currentData()
        if not selected:
            return
        item, start, exit_currency = selected
        index = self.item_select.findData(item)
        if index >= 0:
            self.item_select.setCurrentIndex(index)
        self.start_select.setCurrentIndex(self.start_select.findData(start))
        self.exit_select.setCurrentIndex(self.exit_select.findData(exit_currency))

    def _item_changed(self):
        self.selected_item = self.item_select.currentData()
        self._update_reference()
        self._route_changed()

    def _update_reference(self):
        if not self.selected_item:
            self.reference.setText("选择物品后显示历史线索")
            return
        paths = indicative_paths(self._edges(), self.selected_item)
        hour = latest_market_hour(self.snapshot.get("markets", []), self.league.currentText())
        scout_current = (
            self.scout_snapshot
            and self.scout_snapshot.get("league") == self.league.currentText()
            and 0 <= snapshot_age(self.scout_snapshot) <= MAX_AGE_SECONDS
        )
        source = "Scout 快照" if scout_current else "小时历史"
        if paths:
            start, exit_currency, gain = paths[0]
            self.reference.setText(f"{source}线索：{item_name(start)}买 → {item_name(exit_currency)}卖，参考 {float(gain):+.1%}；需实时核价")
        else:
            self.reference.setText(f"{source}无该物品完整线索" + (f" · 小时 {hour}" if hour else ""))

    def _route_changed(self):
        item = self.selected_item
        start, exit_currency = self.start_select.currentData(), self.exit_select.currentData()
        pairs = {
            "买入": (start, item) if item and start else None,
            "卖出": (item, exit_currency) if item and exit_currency else None,
            "换回": (exit_currency, start) if start and exit_currency and start != exit_currency else None,
        }
        now = int(time.time())
        for role, row in self.rows.items():
            pair = pairs[role]
            row.set_pair(pair, self.quotes.get(pair) if pair else None)
        self._save_settings()
        self.refresh(now)

    def _quote_changed(self):
        row = next((row for row in self.rows.values() if self.sender() is row), None)
        if row is None:
            return
        self._save_row(row)

    def _save_row(self, row: QuoteRow):
        if not row.pair:
            return
        values = [field.text().strip().replace(",", "") for field in (row.pay, row.receive, row.stock, row.gold)]
        if not all(values[:3]):
            self.quotes.pop(row.pair, None)
            self.refresh()
            return
        try:
            pay, receive, stock = map(int, values[:3])
            gold = int(values[3]) if values[3] else None
            quote = Quote(*row.pair, pay, receive, stock, int(time.time()), gold)
            quote.validate()
            self.quotes[row.pair] = quote
            self._save_settings()
            row.age.setText("刚刚录入 · 请确认游戏方向")
        except ValueError as exc:
            self.capture_hint.setText(f"{row.role}报价无效：{exc}")
            return
        self.capture_hint.setText("报价已录入；游戏右侧是支付，左侧是获得。")
        self.refresh()

    def edit_core_rate(self, source: str, target: str):
        self.core_pair = (source, target)
        self.core_title.setText(f"{item_name(source)} → {item_name(target)}")
        quote = self.quotes.get(self.core_pair)
        for field, value in zip(self.core_fields,
                                (quote.pay, quote.receive, quote.stock, quote.gold) if quote
                                else (None, None, None, None)):
            field.setText(str(value) if value is not None else "")
        self.core_editor.show()
        self.core_fields[0].setFocus()

    def save_core_rate(self):
        values = [field.text().strip().replace(",", "") for field in self.core_fields]
        try:
            pay, receive, stock = map(int, values[:3])
            gold = int(values[3]) if values[3] else None
            quote = Quote(*self.core_pair, pay, receive, stock, int(time.time()), gold)
            quote.validate()
        except (ValueError, AttributeError) as exc:
            self.status.setText(f"核心汇率无效：{exc}")
            return
        self.quotes[self.core_pair] = quote
        self._save_settings()
        self.core_editor.hide()
        self._route_changed()

    def capture_quote(self, role: str):
        row = self.rows[role]
        if row.pair is None:
            return
        self.capture_role = role
        self.capture_hint.setText(
            f"请在游戏右侧放 {item_name(row.pair[0])}、左侧放 {item_name(row.pair[1])}，并悬停市场比率；2.5 秒后截图。"
        )
        self.hide()
        QTimer.singleShot(2500, self._capture_screen)

    def calibrate(self, key: str):
        from PIL import ImageGrab

        self.hide()

        def open_selector():
            try:
                image = ImageGrab.grab(all_screens=False)
                stream = io.BytesIO()
                image.save(stream, format="PNG")
                pixmap = QPixmap()
                pixmap.loadFromData(stream.getvalue())
                label = "交易订单和金币区域" if key == "roi" else "悬停后的比率和库存区域"
                self.region_selector = RegionSelector(pixmap, label)
                self.region_selector.selected.connect(lambda region: self._save_region(key, region))
                self.region_selector.cancelled.connect(self.show)
            except Exception as exc:
                self.show()
                self.capture_hint.setText(f"校准失败：{exc}")

        QTimer.singleShot(700, open_selector)

    def _save_region(self, key: str, region: dict):
        self.settings[key] = region
        self._save_settings()
        self.show()
        self.capture_hint.setText("截图区域已保存；现在可以重新读取对应交易方向。")

    def capture_core_rate(self):
        if not hasattr(self, "core_pair"):
            return
        self.capture_role = "核心"
        self.capture_hint.setText(
            f"请在游戏右侧放 {item_name(self.core_pair[0])}、左侧放 {item_name(self.core_pair[1])}，并悬停市场比率；2.5 秒后截图。"
        )
        self.hide()
        QTimer.singleShot(2500, self._capture_screen)

    def _capture_screen(self):
        from PIL import ImageGrab
        from .calibration import capture_preset

        try:
            full = ImageGrab.grab(all_screens=False)
            preset = capture_preset(full.size)
            stock = self.settings.get("stock_roi") or preset.get("stock_roi")
            trade = self.settings.get("roi") or preset.get("roi")
            if not trade:
                raise ValueError("该分辨率尚无 OCR 预设，请手动录入当前订单")
            for region in (trade, stock):
                if region and region["screen"] != list(full.size):
                    raise ValueError("分辨率已变化，请重新校准截图区域")
            images = []
            for roi in (trade, stock):
                if roi:
                    stream = io.BytesIO()
                    full.crop(tuple(roi["bbox"])).save(stream, format="PNG")
                    images.append(stream.getvalue())
                else:
                    images.append(None)
            self.show()
            self._job(self._read_capture, self._capture_done, *images)
        except Exception as exc:
            self.show()
            self.capture_hint.setText(f"截图失败：{exc}")

    @staticmethod
    def _read_capture(trade_data: bytes, stock_data: bytes | None):
        from .ocr import read_exchange_panel, read_stock_region

        panel = read_exchange_panel(trade_data)
        ladder = read_stock_region(stock_data) if stock_data else None
        return panel, ladder

    def _capture_done(self, captured):
        from .ocr import resolve_capture_order

        panel, ladder = captured
        if self.capture_role == "核心":
            fields = self.core_fields
        else:
            row = self.rows.get(self.capture_role)
            if row is None or row.pair is None:
                return
            fields = (row.pay, row.receive, row.stock, row.gold)
        order = resolve_capture_order(panel.get("selected_order"),
                                      ladder.get("best_quote") if ladder else None)
        if not order:
            self.capture_hint.setText("未可靠识别订单；请手动填写，或调整游戏交易栏后重读。")
            return
        fields[0].setText(str(order["pay"]))
        fields[1].setText(str(order["receive"]))
        if ladder and ladder.get("stock"):
            fields[2].setText(str(ladder["stock"]))
        if order.get("gold") is not None:
            fields[3].setText(str(order["gold"]))
        # The game direction cannot be inferred reliably from a numeric crop.
        # Keep the OCR values visible for the user to check before they are used.
        next_step = ("然后点击「保存报价」。" if self.capture_role == "核心" else
                     "然后点此行任一输入框并按 Enter 保存。")
        self.capture_hint.setText(
            f"已读到支付 {order['pay']}、获得 {order['receive']}。请核对方向和库存，{next_step}"
        )

    def refresh(self, now: int | None = None):
        if not hasattr(self, "rows"):
            return
        now = now or int(time.time())
        references = self._edges()
        for card in self.rate_cards:
            card.refresh(self.quotes, references, now)
        for row in self.rows.values():
            row.update_age(self.quotes.get(row.pair) if row.pair else None, now)
        item = self.selected_item
        start, exit_currency = self.start_select.currentData(), self.exit_select.currentData()
        if not item or start == exit_currency:
            self._empty_result("请选择物品和两种不同的核心通货")
            return
        pairs = ((start, item), (item, exit_currency), (exit_currency, start))
        missing = [pair for pair in pairs if pair not in self.quotes]
        if missing:
            self._empty_result("还需录入：" + "、".join(f"{item_name(a)}→{item_name(b)}" for a, b in missing))
            return
        try:
            outcome = evaluate(*(self.quotes[pair] for pair in pairs), now=now)
        except ValueError as exc:
            self._empty_result(str(exc))
            return
        if outcome.profit <= 0:
            state = "当前完整订单 · 无利润"
        elif outcome.exact_gold is None:
            state = "通货盈利 · 金币待核验"
        else:
            state = "当前完整订单 · 盈利"
        self.result_state.setText(state)
        self.profit.setText(f"{outcome.profit:+,} {item_name(start)}")
        if outcome.profit <= 0:
            color = "#f29a8d"
        elif outcome.exact_gold is None:
            color = "#e9c170"
        else:
            color = "#89e4ae"
        self.profit.setStyleSheet(f"color:{color}")
        self.profit_caption.setText("三笔交易后换回起始通货 · 不计未成交零头")
        values = {
            "最小一轮投入": f"{outcome.start_amount:,} {item_name(start)}",
            "完整交易数量": f"{outcome.item_amount:,} {item_name(item)}",
            "最多完整轮数": str(outcome.max_rounds),
            "收益率": f"{float(outcome.roi):+.2%}",
            "三笔金币": f"{outcome.exact_gold:,}" if outcome.exact_gold is not None else "准确数量待核验",
            "每百万金币收益": (f"{float(outcome.profit_per_million_gold):+,.1f} {item_name(start)}"
                              if outcome.profit_per_million_gold is not None else "金币待核验"),
        }
        for label, value in values.items():
            self.metrics[label].setText(value)
        order_plan = (
            f"计划：{outcome.start_amount:,} {item_name(start)} → "
            f"{outcome.item_amount:,} {item_name(item)} → "
            f"{outcome.exit_amount:,} {item_name(exit_currency)} → "
            f"{outcome.final_amount:,} {item_name(start)}。"
        )
        self.result_detail.setText(
            order_plan
            + ("多单金币不能按单笔线性放大，请在游戏里输入完整数量并核对实际费用。"
               if outcome.exact_gold is None else "报价与库存随时可能变化，下单前再核对。")
        )

    def _empty_result(self, reason: str):
        self.result_state.setText("等待当前报价")
        self.profit.setText("—")
        self.profit.setStyleSheet("")
        self.profit_caption.setText("完整三笔订单后计算")
        for value in self.metrics.values():
            value.setText("—")
        self.result_detail.setText(reason)


def run():
    app = QApplication(sys.argv)
    app.setFont(QFont("Microsoft YaHei UI", 10))
    window = Dashboard()
    return app.exec()

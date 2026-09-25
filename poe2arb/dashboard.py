"""Single-window dashboard for one item's cross-currency spread."""

from __future__ import annotations

import io
import json
import sys
import time
from dataclasses import asdict, replace
from fractions import Fraction
from pathlib import Path

from PySide6.QtCore import QPoint, QRect, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication, QBoxLayout, QCheckBox, QComboBox, QFrame, QGridLayout, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton, QScrollArea,
    QSizePolicy, QVBoxLayout, QWidget,
)

from .catalog import catalog, item_icon, item_name, item_pixmap
from .core import historical_edges, latest_market_hour
from .data import app_data_dir, leagues, load_snapshot, sync_recent
from .depth import Book, DepthPlan, Level, captured_book, effective_levels, estimate_depth
from .scout import MAX_AGE_SECONDS, fetch_scout_snapshot, load_scout_snapshot, scout_edges, snapshot_age
from .single_item import (CHAOS, CORE, DIVINE, EXALTED, MAX_QUOTE_AGE, Quote,
                          core_gold_cost, evaluate, indicative_paths, item_unit_gold,
                          normalize_quote_amounts)
from .trade_log import ROLES, Trade, summarize_trades, trade_gold

MAX_HOURLY_LEAD_AGE_SECONDS = 2 * 3600


def settings_path() -> Path:
    return app_data_dir() / "dashboard_settings.json"


def read_settings() -> dict:
    try:
        return json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def age_text(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} 秒"
    return f"{seconds // 60} 分 {seconds % 60} 秒"


def readable_rate(source: str, target: str, rate: Fraction) -> str:
    """Show the rarer side as one unit instead of a tiny decimal."""
    if rate <= 0:
        return "汇率不可用"
    inverse = rate < 1
    amount = 1 / rate if inverse else rate
    if amount.denominator == 1:
        number = f"{amount.numerator:,}"
    else:
        number = f"{float(amount):,.4f}".rstrip("0").rstrip(".")
    if inverse:
        return f"{number} {source} ≈ 1 {target}"
    return f"1 {source} ≈ {number} {target}"


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


class RateDirection(QFrame):
    selected = Signal(str, str)

    def __init__(self, source: str, target: str):
        super().__init__()
        self.source, self.target = source, target
        self.setObjectName("rateDirection")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(3)
        heading = QHBoxLayout()
        self.direction = QLabel(f"{item_name(source)} → {item_name(target)}")
        self.direction.setObjectName("rateDirectionTitle")
        heading.addWidget(self.direction)
        heading.addStretch()
        self.state = QLabel("待录入")
        self.state.setObjectName("rateState")
        heading.addWidget(self.state)
        action = QPushButton("更新")
        action.setObjectName("rateAction")
        action.setToolTip(f"录入 {item_name(source)} → {item_name(target)} 的当前游戏订单")
        action.clicked.connect(lambda: self.selected.emit(source, target))
        heading.addWidget(action)
        layout.addLayout(heading)
        self.amount = QLabel("尚未录入当前订单")
        self.amount.setObjectName("rateAmount")
        self.amount.setWordWrap(True)
        layout.addWidget(self.amount)
        self.meta = QLabel("历史价格仅供参考")
        self.meta.setObjectName("rateMeta")
        self.meta.setWordWrap(True)
        layout.addWidget(self.meta)

    def refresh(self, quote: Quote | None, reference, now: int, depth_count: int = 0):
        source, target = item_name(self.source), item_name(self.target)
        if quote:
            age = max(0, now - quote.observed_at)
            self.amount.setText(f"付 {quote.pay} {source}  →  得 {quote.receive} {target}")
            depth_hint = f" · {depth_count}档" if depth_count > 1 else ""
            self.meta.setText(f"折算：{readable_rate(source, target, quote.rate)} · {age_text(age)}前{depth_hint}")
            stale = age > MAX_QUOTE_AGE
            if quote.stock is None:
                self.state.setText("库存未录入")
                stale = True
            elif quote.stock < quote.receive:
                self.state.setText("库存不足")
                stale = True
            else:
                self.state.setText("需复核" if stale else "近期报价")
        else:
            self.amount.setText("尚未录入当前订单")
            if reference:
                self.meta.setText(f"历史参考：{readable_rate(source, target, reference.rate)}")
            else:
                self.meta.setText("无历史参考，点击更新录入")
            self.state.setText("待录入")
            stale = True
        self.state.setProperty("stale", stale)
        self.state.style().unpolish(self.state)
        self.state.style().polish(self.state)


class RateCard(QFrame):
    selected = Signal(str, str)
    save_requested = Signal()
    capture_requested = Signal()

    def __init__(self, a: str, b: str):
        super().__init__()
        self.a, self.b = a, b
        self.setObjectName("rateCard")
        layout = QVBoxLayout(self)
        layout.setSpacing(5)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
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
        self.directions: dict[tuple[str, str], RateDirection] = {}
        for source, target in ((a, b), (b, a)):
            row = RateDirection(source, target)
            row.selected.connect(self.selected.emit)
            layout.addWidget(row)
            self.directions[(source, target)] = row
        self.editor = QFrame()
        self.editor.setObjectName("rateEditor")
        edit = QVBoxLayout(self.editor)
        edit.setContentsMargins(8, 8, 8, 8)
        self.edit_title = QLabel()
        self.edit_title.setObjectName("rowTitle")
        edit.addWidget(self.edit_title)
        grid = QGridLayout()
        self.fields = []
        for index, title in enumerate(("支付", "获得", "可获库存")):
            field = QLineEdit()
            field.setPlaceholderText("可填小数" if index < 2 else "数量")
            field.setFixedWidth(82)
            field.setToolTip("可填小数，保存后按比例换算为最小整数订单" if index < 2
                             else f"当前游戏订单的{title}数量")
            grid.addWidget(QLabel(title), 0, index)
            grid.addWidget(field, 1, index)
            self.fields.append(field)
        edit.addLayout(grid)
        actions = QHBoxLayout()
        capture = QPushButton("截图读取")
        capture.clicked.connect(self.capture_requested.emit)
        actions.addWidget(capture)
        save = QPushButton("保存报价")
        save.clicked.connect(self.save_requested.emit)
        actions.addWidget(save)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.editor.hide)
        actions.addWidget(cancel)
        edit.addLayout(actions)
        layout.addWidget(self.editor)
        self.editor.hide()

    def edit(self, source: str, target: str, quote: Quote | None):
        self.edit_title.setText(f"更新 {item_name(source)} → {item_name(target)} · 右侧支付，左侧获得")
        values = (quote.pay, quote.receive, quote.stock) if quote else (None,) * 3
        for field, value in zip(self.fields, values):
            field.setText(str(value) if value is not None else "")
        self.editor.show()
        self.fields[0].setFocus()

    def refresh(self, quotes: dict, references: dict, now: int, depth_books: dict):
        for pair, row in self.directions.items():
            book = depth_books.get(pair)
            row.refresh(quotes.get(pair), references.get(pair), now,
                        len(book.levels) if book else 0)


class QuoteRow(QFrame):
    requested = Signal(str)
    changed = Signal()
    FIELD_WIDTH = 82
    ACTION_WIDTH = 112
    DIRECTION_MIN_WIDTH = 180
    DIRECTION_MAX_WIDTH = 390
    DIRECTION_NON_TEXT_WIDTH = 90

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
        self.source_icon.setFixedSize(22, 22)
        self.target_icon.setFixedSize(22, 22)
        self.source_name = QLabel()
        self.target_name = QLabel()
        self.arrow_label = QLabel("→")
        self.arrow_label.setFixedWidth(18)
        for label in (self.source_name, self.arrow_label, self.target_name):
            label.setObjectName("direction")
        direction_layout.addWidget(self.source_icon)
        direction_layout.addWidget(self.source_name)
        direction_layout.addWidget(self.arrow_label)
        direction_layout.addWidget(self.target_name)
        direction_layout.addWidget(self.target_icon)
        direction_layout.addStretch()
        self.source_text = "选择物品后显示方向"
        self.target_text = ""
        self.direction_host = QWidget()
        self.direction_host.setFixedWidth(self.DIRECTION_MIN_WIDTH)
        self.direction_host.setLayout(direction_layout)
        grid.addWidget(self.direction_host, 1, 0, 1, 2)
        self.pay = QLineEdit()
        self.receive = QLineEdit()
        self.stock = QLineEdit()
        self.gold = QLineEdit()
        self.save_timer = QTimer(self)
        self.save_timer.setSingleShot(True)
        self.save_timer.setInterval(400)
        self.save_timer.timeout.connect(self.changed.emit)
        for col, (title, field) in enumerate((
            ("支付", self.pay), ("获得", self.receive),
            ("可获库存", self.stock), ("每个金币", self.gold),
        )):
            field.setPlaceholderText("可填小数" if col < 2 else "数量")
            field.setFixedWidth(self.FIELD_WIDTH)
            if col < 2:
                field.setToolTip("可填小数，保存后按比例换算为最小整数订单")
            field.textEdited.connect(lambda _text: self.save_timer.start())
            field.editingFinished.connect(self._finish_edit)
            label = QLabel(title)
            label.setObjectName("fieldLabel")
            if title == "每个金币":
                self.gold_label = label
            grid.addWidget(label, 0, col + 2)
            grid.addWidget(field, 1, col + 2)
        self.age = QLabel("待录入")
        self.age.setObjectName("muted")
        grid.addWidget(self.age, 2, 0, 1, 2)
        self.capture = QPushButton("截图读取")
        self.capture.clicked.connect(lambda: self.requested.emit(self.role))
        actions = QVBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.addWidget(self.capture)
        self.save = QPushButton("保存读取")
        self.save.clicked.connect(self._finish_edit)
        actions.addWidget(self.save)
        action_host = QWidget()
        action_host.setFixedWidth(self.ACTION_WIDTH)
        action_host.setLayout(actions)
        grid.setColumnMinimumWidth(5, self.FIELD_WIDTH)
        grid.setColumnStretch(6, 1)
        grid.addWidget(action_host, 1, 6, 2, 1, Qt.AlignmentFlag.AlignRight)
        self.depth_info = QLabel()
        self.depth_info.setObjectName("muted")
        self.depth_info.setWordWrap(True)
        grid.addWidget(self.depth_info, 2, 2, 1, 4)

    def _finish_edit(self):
        self.save_timer.stop()
        self.changed.emit()

    def resizeEvent(self, event):
        grid = self.layout()
        margins = grid.contentsMargins()
        other_width = (4 * self.FIELD_WIDTH + self.ACTION_WIDTH
                       + 6 * grid.horizontalSpacing()
                       + margins.left() + margins.right())
        width = min(self.DIRECTION_MAX_WIDTH,
                    max(self.DIRECTION_MIN_WIDTH, event.size().width() - other_width))
        if self.direction_host.width() != width:
            self.direction_host.setFixedWidth(width)
        self._fit_direction_text()
        super().resizeEvent(event)

    def _fit_direction_text(self):
        available = max(70, self.direction_host.width() - self.DIRECTION_NON_TEXT_WIDTH)
        metrics = self.source_name.fontMetrics()
        if not self.pair:
            self.source_name.setFixedWidth(available)
            self.source_name.setText(metrics.elidedText(
                self.source_text, Qt.TextElideMode.ElideRight, available))
            return
        source_natural = metrics.horizontalAdvance(self.source_text)
        target_natural = metrics.horizontalAdvance(self.target_text)
        if source_natural + target_natural <= available:
            source_width = source_natural
            target_width = target_natural
        elif source_natural <= available // 2:
            source_width = source_natural
            target_width = available - source_width
        elif target_natural <= available // 2:
            source_width = available - target_natural
            target_width = target_natural
        else:
            source_width = available // 2
            target_width = available - source_width
        source_width = max(30, source_width)
        target_width = max(30, target_width)
        self.source_name.setFixedWidth(source_width)
        self.target_name.setFixedWidth(target_width)
        self.source_name.setText(metrics.elidedText(
            self.source_text, Qt.TextElideMode.ElideRight, source_width))
        self.target_name.setText(metrics.elidedText(
            self.target_text, Qt.TextElideMode.ElideRight, target_width))

    def set_pair(self, pair: tuple[str, str] | None, quote: Quote | None):
        self.pair = pair
        if pair:
            self.source_text, self.target_text = item_name(pair[0]), item_name(pair[1])
            tooltip = (f"{self.source_text} → {self.target_text}"
                       " · 游戏右侧「我拥有的」→ 左侧「我需要的」")
            self.source_name.setToolTip(tooltip)
            self.target_name.setToolTip(tooltip)
            self.arrow_label.setVisible(True)
            self.target_name.setVisible(True)
            self.source_icon.setPixmap(item_pixmap(pair[0], 22))
            self.target_icon.setPixmap(item_pixmap(pair[1], 22))
        else:
            self.source_text, self.target_text = "先选择物品与交易方向", ""
            self.arrow_label.setVisible(False)
            self.target_name.setVisible(False)
            self.source_icon.clear()
            self.target_icon.clear()
        self._fit_direction_text()
        show_gold = bool(pair and pair[1] not in CORE)
        self.gold_label.setVisible(show_gold)
        self.gold.setVisible(show_gold)
        self.gold.setToolTip("换得 1 个交易物品所需的固定金币，不是整笔订单金币")
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
            if quote.stock is None:
                state = "库存未录入 · 仅供参考"
            elif quote.stock < quote.receive:
                state = "库存不足 · 仅供参考"
            else:
                state = "需重新核价" if age > MAX_QUOTE_AGE else "近期录入"
            self.age.setText(f"{age_text(age)}前 · {state}")

    def set_depth(self, book: Book | None):
        if book and len(book.levels) > 1:
            parts = []
            for index, level in enumerate(book.levels, 1):
                ratio = level.ratio or f"{level.receive}:{level.pay}"
                parts.append(f"{index}档 {ratio} / 库存{level.stock}")
            self.depth_info.setText(f"已读 {len(book.levels)} 档 · {' · '.join(parts)}")
        else:
            self.depth_info.clear()


class Dashboard(QWidget):
    def __init__(self):
        super().__init__()
        self.settings = read_settings()
        self.snapshot = load_snapshot()
        self.scout_snapshot = load_scout_snapshot()
        self.quotes: dict[tuple[str, str], Quote] = self._restore_quotes()
        self.depth_books: dict[tuple[str, str], Book] = self._restore_depth_books()
        self.pending_depth: dict[tuple[str, str], Book] = {}
        self.trades: list[Trade] = self._restore_trades()
        self.recent_items = [item for item in self.settings.get("recent_items", [])
                             if item in catalog() and item not in CORE][:5]
        self.jobs: list[Job] = []
        self._closing = False
        self.capture_role: str | None = None
        self.region_selector: RegionSelector | None = None
        self.selected_item: str | None = None
        self._active_lead_source = "none"
        self._build()
        self._style()
        self._load_leagues()
        self._restore_selection()
        self._update_suggestions()
        self.refresh()
        self.setMinimumSize(1000, 640)
        available = QApplication.primaryScreen().availableGeometry()
        self.resize(min(1320, available.width()), min(940, available.height()))
        self.setWindowTitle("PoE2 单物品价差助手")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint)
        self.show()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1000)
        self.scout_timer = QTimer(self)
        self.scout_timer.timeout.connect(self.update_scout)
        self.scout_timer.start(5 * 60 * 1000)
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
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(panel)
        outer.addWidget(scroll)

        header = QHBoxLayout()
        heading = QLabel("◆  PoE2 单物品价差助手")
        heading.setObjectName("heading")
        header.addWidget(heading)
        self.league = QComboBox()
        self.league.setMinimumWidth(170)
        self.league.setMaximumWidth(210)
        self.league.currentIndexChanged.connect(self._league_changed)
        header.addStretch()
        header.addWidget(QLabel("联赛"))
        header.addWidget(self.league)
        self.status = QLabel("正在加载行情…")
        self.status.setObjectName("muted")
        header.addWidget(self.status)
        refresh = QPushButton("刷新历史线索")
        refresh.clicked.connect(self.refresh_leads)
        header.addWidget(refresh)
        root.addLayout(header)

        market_head = QHBoxLayout()
        market_head.addWidget(self._section("核心通货汇率"))
        market_head.addStretch()
        market_head.addWidget(QLabel("多档库存"))
        self.stock_mode = QComboBox()
        self.stock_mode.addItem("逐档", "per_level")
        self.stock_mode.addItem("累计", "cumulative")
        self.stock_mode.setToolTip("逐档：每行是独立库存；累计：下一行数字已包含前面的库存。多档结果始终为预估。")
        mode_index = self.stock_mode.findData(self.settings.get("stock_mode", "per_level"))
        self.stock_mode.setCurrentIndex(max(0, mode_index))
        self.stock_mode.currentIndexChanged.connect(self._stock_mode_changed)
        market_head.addWidget(self.stock_mode)
        self.ignore_stock = QCheckBox("忽略库存 · 只看理论价差")
        self.ignore_stock.setToolTip("只按首档汇率计算最小整数交易；可不填库存。结果不能视为可成交利润。")
        self.ignore_stock.setChecked(bool(self.settings.get("ignore_stock", False)))
        self.ignore_stock.toggled.connect(self._stock_mode_changed)
        market_head.addWidget(self.ignore_stock)
        hint = QLabel("当前订单优先 · 超过 3 分钟仍保留参考测算 · 历史价不参与利润")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        hint.setMaximumWidth(340)
        market_head.addWidget(hint)
        root.addLayout(market_head)
        market = QHBoxLayout()
        market.setSpacing(9)
        self.rate_cards = [RateCard(EXALTED, DIVINE), RateCard(EXALTED, CHAOS),
                           RateCard(DIVINE, CHAOS)]
        for card in self.rate_cards:
            card.selected.connect(self.edit_core_rate)
            card.save_requested.connect(self.save_core_rate)
            card.capture_requested.connect(self.capture_core_rate)
            market.addWidget(card, 1, Qt.AlignmentFlag.AlignTop)
        root.addLayout(market)

        search = QHBoxLayout()
        search.addWidget(self._section("交易物品"))
        self.item_select = QComboBox()
        self.item_select.setMinimumWidth(240)
        self.item_select.setMaximumWidth(480)
        self.item_select.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.item_select.setMinimumContentsLength(16)
        self.item_select.setMaxVisibleItems(15)
        self.item_select.addItem("按历史线索选择物品…", None)
        self.item_select.currentIndexChanged.connect(self._item_combo_changed)
        search.addWidget(self.item_select)
        self.item_search = QLineEdit()
        self.item_search.setPlaceholderText("搜索物品中文、英文名称…")
        self.item_search.setClearButtonEnabled(True)
        self.item_search.textEdited.connect(self._filter_items)
        self.item_search.returnPressed.connect(self._choose_first_match)
        search.addWidget(self.item_search, 1)
        root.addLayout(search)
        self.item_results = QListWidget()
        self.item_results.setMaximumHeight(165)
        self.item_results.itemClicked.connect(self._choose_item_result)
        self.item_results.hide()
        root.addWidget(self.item_results)
        suggestion_row = QHBoxLayout()
        self.suggestion_label = QLabel("历史线索 · 点击即选")
        suggestion_row.addWidget(self.suggestion_label)
        self.suggestions = []
        for _ in range(3):
            button = QPushButton()
            button.setObjectName("suggestion")
            button.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            button.setToolTip("仅供选择检查目标；请以游戏当前订单计算利润")
            button.clicked.connect(lambda checked=False, b=button: self._apply_suggestion(b.property("suggestion")))
            self.suggestions.append(button)
            suggestion_row.addWidget(button, 1)
        root.addLayout(suggestion_row)
        recent_row = QHBoxLayout()
        recent_row.addWidget(QLabel("最近选择"))
        self.recent_buttons = []
        for _ in range(5):
            button = QPushButton()
            button.setObjectName("suggestion")
            button.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            button.clicked.connect(
                lambda checked=False, b=button: self.select_item(b.property("recent_item")))
            self.recent_buttons.append(button)
            recent_row.addWidget(button, 1)
        root.addLayout(recent_row)
        self._update_recent_buttons()

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
        self.reference.setWordWrap(True)
        self.reference.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        route.addWidget(self.reference, 1)
        root.addLayout(route)

        body = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self.body_layout = body
        body.setSpacing(12)
        left = QVBoxLayout()
        left.addWidget(self._section("当前订单"))
        self.rows = {role: QuoteRow(role) for role in ("买入", "卖出", "换回")}
        for row in self.rows.values():
            row.requested.connect(self.capture_quote)
            row.changed.connect(self._quote_changed)
            left.addWidget(row)
        self.capture_hint = QLabel("交易物品金币手动填写；换得基础通货的金币费自动计算。")
        self.capture_hint.setWordWrap(True)
        self.capture_hint.setObjectName("muted")
        left.addWidget(self.capture_hint)
        left.addStretch()
        body.addLayout(left, 3)

        result = QFrame()
        result.setObjectName("resultCard")
        right = QVBoxLayout(result)
        right.setSpacing(10)
        right.addWidget(self._section("利润测算"))
        self.result_state = QLabel("等待报价")
        self.result_state.setObjectName("resultState")
        right.addWidget(self.result_state)
        self.progress = QLabel("先选择物品与两种不同通货")
        self.progress.setObjectName("muted")
        self.progress.setWordWrap(True)
        right.addWidget(self.progress)
        self.profit = QLabel("—")
        self.profit.setObjectName("profit")
        right.addWidget(self.profit)
        self.profit_caption = QLabel("换回起始通货后计算")
        self.profit_caption.setObjectName("muted")
        right.addWidget(self.profit_caption)
        self.metrics = {}
        for label in ("本次投入", "买入获得物品", "订单手数（买/卖/换）", "最多完整轮数", "收益率", "三笔金币", "每百万金币收益"):
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

        journal = QFrame()
        journal.setObjectName("quoteRow")
        journal_layout = QVBoxLayout(journal)
        journal_layout.addWidget(self._section("实际交易记录 · 当前路线"))
        self.trade_route = QLabel("先选择交易物品和买卖通货")
        self.trade_route.setObjectName("direction")
        self.trade_route.setWordWrap(True)
        journal_layout.addWidget(self.trade_route)
        self.trade_leg = QLabel("选择买入、卖出或换回后，填写游戏里实际成交的支付与获得。")
        self.trade_leg.setObjectName("muted")
        self.trade_leg.setWordWrap(True)
        journal_layout.addWidget(self.trade_leg)
        journal_entry = QHBoxLayout()
        self.trade_role = QComboBox()
        self.trade_role.addItems(ROLES)
        self.trade_role.currentIndexChanged.connect(self._trade_role_changed)
        journal_entry.addWidget(self.trade_role)
        self.trade_fields = []
        for label in ("实际支付", "实际获得"):
            field = QLineEdit()
            field.setPlaceholderText(label)
            field.setFixedWidth(112)
            journal_entry.addWidget(field)
            self.trade_fields.append(field)
        self._trade_role_changed()
        use_quote = QPushButton("带入当前报价")
        use_quote.setToolTip("仅填入表单，请核对游戏实际成交数量后再记录")
        use_quote.clicked.connect(self._prefill_trade)
        journal_entry.addWidget(use_quote)
        record = QPushButton("记录实际成交")
        record.clicked.connect(self._record_trade)
        journal_entry.addWidget(record)
        journal_entry.addStretch()
        journal_layout.addLayout(journal_entry)
        self.trade_status = QLabel("只记录已在游戏中完成的交易；金币单独统计，不折算成通货。")
        self.trade_status.setObjectName("muted")
        journal_layout.addWidget(self.trade_status)
        self.trade_profit = QLabel("暂无交易记录")
        self.trade_profit.setObjectName("resultState")
        journal_layout.addWidget(self.trade_profit)
        self.trade_balances = QLabel("")
        self.trade_balances.setObjectName("muted")
        self.trade_balances.setWordWrap(True)
        journal_layout.addWidget(self.trade_balances)
        history = QHBoxLayout()
        self.trade_list = QListWidget()
        self.trade_list.setMaximumHeight(120)
        history.addWidget(self.trade_list, 1)
        remove = QPushButton("删除选中记录")
        remove.clicked.connect(self._remove_trade)
        history.addWidget(remove)
        journal_layout.addLayout(history)
        root.addWidget(journal)

        footer = QHBoxLayout()
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
            QLabel { background:transparent; }
            QFrame#panel { background:#111918; }
            QLabel#heading { font-size:18px; font-weight:600; color:#f0f7f1; }
            QLabel#section { font-size:15px; font-weight:600; }
            QLabel#muted, QLabel#detail { color:#96aaa0; }
            QLabel#direction { font-size:14px; font-weight:600; }
            QLabel#rowTitle { color:#78d4a2; font-weight:600; }
            QLabel#fieldLabel { color:#96aaa0; font-size:11px; }
            QPushButton, QComboBox, QLineEdit { background:#1c2925; border:1px solid #31463c;
                border-radius:7px; padding:7px; min-height:23px; }
            QPushButton:hover { background:#28513d; border-color:#5eae81; }
            QComboBox QAbstractItemView { background:#1c2925; selection-background-color:#315c44; }
            QListWidget { background:#1c2925; border:1px solid #31463c; border-radius:7px; }
            QListWidget::item { padding:5px; }
            QListWidget::item:selected { background:#315c44; }
            QFrame#rateCard, QFrame#quoteRow, QFrame#resultCard, QFrame#rateEditor {
                background:#18231f; border:1px solid #30423a; border-radius:11px; }
            QFrame#rateDirection { background:#1c2a23; border:1px solid #31483a; border-radius:7px; }
            QLabel#rateDirectionTitle { color:#b9d9c4; font-size:12px; }
            QLabel#rateAmount { font-size:14px; font-weight:600; color:#eaf3ed; }
            QLabel#rateMeta { color:#9ab2a2; font-size:11px; }
            QLabel#rateState { color:#80d1a1; font-size:11px; }
            QLabel#rateState[stale="true"] { color:#e9c170; }
            QPushButton#rateAction { padding:4px 8px; min-height:18px; }
            QPushButton#suggestion { text-align:left; }
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
        if item in catalog() and item not in CORE:
            self.select_item(item)
        for selector, key in ((self.start_select, "start"), (self.exit_select, "exit")):
            index = selector.findData(self.settings.get(key))
            if index >= 0:
                selector.setCurrentIndex(index)
        if self.start_select.currentData() == self.exit_select.currentData():
            self.exit_select.setCurrentIndex(1)
        self._update_reference()
        self._route_changed()

    def _save_settings(self):
        data = {"league": self.league.currentText(), "item": self.selected_item,
                "gold_per_item_version": 1,
                "recent_items": self.recent_items,
                "start": self.start_select.currentData(), "exit": self.exit_select.currentData(),
                "roi": self.settings.get("roi"), "stock_roi": self.settings.get("stock_roi"),
                "stock_mode": self.stock_mode.currentData(),
                "ignore_stock": self.ignore_stock.isChecked(),
                "quotes": [asdict(quote) for quote in self.quotes.values()],
                "depth_books": [asdict(book) for book in self.depth_books.values()],
                "trades": [asdict(trade) for trade in self.trades]}
        settings_path().write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def _restore_quotes(self) -> dict[tuple[str, str], Quote]:
        quotes = {}
        for row in self.settings.get("quotes", []):
            try:
                quote = Quote(**row)
                if quote.target in CORE:
                    quote = replace(quote, gold=None)
                elif not self.settings.get("gold_per_item_version") and quote.gold is not None:
                    quote = replace(quote, gold=item_unit_gold(quote.gold, quote.receive))
                quote.validate()
                quotes[(quote.source, quote.target)] = quote
            except (TypeError, ValueError):
                continue
        return quotes

    def _restore_depth_books(self) -> dict[tuple[str, str], Book]:
        books = {}
        for saved in self.settings.get("depth_books", []):
            try:
                pair = saved["source"], saved["target"]
                levels = tuple(Level(**level) for level in saved["levels"])
                book = Book(*pair, levels, int(saved["observed_at"]))
                quote = self.quotes[pair]
                if len(levels) < 2 or (levels[0].pay, levels[0].receive, levels[0].stock) != (
                        quote.pay, quote.receive, quote.stock):
                    continue
                if any(min(level.pay, level.receive, level.stock) <= 0 for level in levels):
                    continue
                books[pair] = book
            except (KeyError, TypeError, ValueError):
                continue
        return books

    def _restore_trades(self) -> list[Trade]:
        trades = []
        for saved in self.settings.get("trades", []):
            try:
                trade = Trade(**saved)
                trade.validate()
                trades.append(trade)
            except (TypeError, ValueError):
                continue
        return trades

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

    def resizeEvent(self, event):
        if hasattr(self, "body_layout"):
            direction = (QBoxLayout.Direction.TopToBottom if event.size().width() < 1310
                         else QBoxLayout.Direction.LeftToRight)
            if self.body_layout.direction() != direction:
                self.body_layout.setDirection(direction)
        super().resizeEvent(event)

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

    def refresh_leads(self):
        self.update_history()
        self.update_scout()

    def _history_done(self, snapshot):
        self.snapshot = snapshot
        self._load_leagues()
        self._update_suggestions()
        self.status.setText("小时历史已更新")

    def update_scout(self):
        league = self.league.currentText() or None
        if any(job.function is fetch_scout_snapshot and job.args == (league,)
               for job in self.jobs):
            return
        self._job(fetch_scout_snapshot, self._scout_done, league)

    def _scout_done(self, snapshot):
        if self.league.currentText() and snapshot["league"] != self.league.currentText():
            QTimer.singleShot(0, self.update_scout)
            return
        self.scout_snapshot = snapshot
        self._load_leagues()
        self._update_suggestions()
        age = snapshot_age(snapshot)
        if 0 <= age <= MAX_AGE_SECONDS:
            message = f"Scout 快照 {age_text(age)}前"
        elif self._recent_history_hour() is not None:
            message = f"Scout 快照已过期（{age_text(age)}前），改用小时历史"
        else:
            message = f"Scout 快照已过期（{age_text(age)}前），暂无近期线索"
        self.status.setText(message)

    def _league_changed(self):
        self.quotes.clear()
        self.depth_books.clear()
        self.pending_depth.clear()
        self._save_settings()
        self._update_suggestions()
        self._route_changed()
        QTimer.singleShot(0, self.update_scout)

    def _stock_mode_changed(self):
        self._save_settings()
        self.refresh()

    def _route_trades(self) -> list[tuple[int, Trade]]:
        route = (self.league.currentText(), self.selected_item,
                 self.start_select.currentData(), self.exit_select.currentData())
        return [(index, trade) for index, trade in enumerate(self.trades)
                if (trade.league, trade.item, trade.start, trade.exit_currency) == route]

    def _trade_role_changed(self):
        for field in self.trade_fields:
            field.clear()
        self._update_trade_path()

    def _update_trade_path(self):
        item = self.selected_item
        start = self.start_select.currentData()
        exit_currency = self.exit_select.currentData()
        if not item or not start or not exit_currency or start == exit_currency:
            self.trade_route.setText("先选择交易物品和两种不同的通货")
            self.trade_leg.setText("填写游戏里实际成交的支付与获得，不是报价预测值。")
            return
        self.trade_route.setText(
            f"完整路线：{item_name(start)} → {item_name(item)} → "
            f"{item_name(exit_currency)} → {item_name(start)}"
        )
        role = self.trade_role.currentText()
        pair = self.rows[role].pair
        if pair:
            self.trade_leg.setText(
                f"当前记录 · {role}：支付 {item_name(pair[0])} → 获得 {item_name(pair[1])}"
                "（按游戏实际成交数量填写）"
            )

    def _prefill_trade(self):
        row = self.rows[self.trade_role.currentText()]
        quote = self.quotes.get(row.pair) if row.pair else None
        if quote is None:
            self.trade_status.setText("该方向没有当前报价，请手动填写实际成交数量。")
            return
        for field, value in zip(self.trade_fields, (quote.pay, quote.receive)):
            field.setText(str(value) if value is not None else "")
        self.trade_status.setText("已带入报价；请按游戏实际成交数量核对，再点击记录。")

    def _record_trade(self):
        role = self.trade_role.currentText()
        row = self.rows[role]
        if row.pair is None or not self.selected_item:
            self.trade_status.setText("请先选择物品及买卖通货。")
            return
        try:
            values = [field.text().strip().replace(",", "") for field in self.trade_fields]
            pay, receive = int(values[0]), int(values[1])
            gold = None
            if row.pair[1] not in CORE:
                quote = self.quotes.get(row.pair)
                if quote is not None and quote.gold is not None:
                    gold = receive * quote.gold
            trade = Trade(self.league.currentText(), self.selected_item,
                          self.start_select.currentData(), self.exit_select.currentData(),
                          role, *row.pair, pay, receive, gold, int(time.time()))
            trade.validate()
        except ValueError as exc:
            self.trade_status.setText(f"实际成交无效：{exc}")
            return
        self.trades.append(trade)
        self._save_settings()
        for field in self.trade_fields:
            field.clear()
        self.trade_status.setText("实际成交已记录；未完成换回时显示按当前报价的持仓估值。")
        self._refresh_trades(int(time.time()))

    def _remove_trade(self):
        selected = self.trade_list.currentItem()
        if selected is None:
            self.trade_status.setText("请先选中一条交易记录。")
            return
        index = selected.data(Qt.ItemDataRole.UserRole)
        if not isinstance(index, int) or not 0 <= index < len(self.trades):
            return
        del self.trades[index]
        self._save_settings()
        self.trade_status.setText("已删除选中的交易记录。")
        self._refresh_trades(int(time.time()))

    def _refresh_trades(self, now: int):
        entries = self._route_trades()
        item_fees = {quote.target: quote.gold for quote in self.quotes.values()
                     if quote.target not in CORE and quote.gold is not None}
        key = (self.league.currentText(), self.selected_item,
               self.start_select.currentData(), self.exit_select.currentData(),
               tuple(entries), tuple(sorted(item_fees.items())))
        if key != getattr(self, "_trade_list_key", None):
            self._trade_list_key = key
            self.trade_list.clear()
            for index, trade in reversed(entries):
                stamp = time.strftime("%H:%M:%S", time.localtime(trade.observed_at))
                fee = trade_gold(trade, item_fees)
                gold = f" · 金币 {fee:,}" if fee is not None else " · 金币未填"
                label = (f"{stamp}  {trade.role}  付 {trade.pay:,} {item_name(trade.source)}"
                         f" → 得 {trade.receive:,} {item_name(trade.target)}{gold}")
                entry = QListWidgetItem(label)
                entry.setData(Qt.ItemDataRole.UserRole, index)
                self.trade_list.addItem(entry)
        if not entries:
            self.trade_profit.setText("暂无交易记录")
            self.trade_balances.setText("录入实际成交后，这里显示本路线的净变动与持仓估值。")
            return
        start = self.start_select.currentData()
        summary = summarize_trades([trade for _, trade in entries], start, self.quotes,
                                   now, item_fees)
        if summary.profit is None:
            self.trade_profit.setText("持仓暂无法估值")
        else:
            amount = (f"{summary.profit.numerator:+,}" if summary.profit.denominator == 1
                      else f"{float(summary.profit):+,.2f}")
            if summary.settled:
                self.trade_profit.setText(f"已换回净收益 {amount} {item_name(start)}")
            else:
                self.trade_profit.setText(f"持仓参考估值 {amount} {item_name(start)}")
        parts = [f"{item_name(currency)} {amount:+,}"
                 for currency, amount in summary.balances.items()]
        parts.append(f"金币支出 {summary.gold:,}" if summary.gold is not None else "金币未填齐")
        if summary.profit is None:
            parts.append("存在未覆盖的支付或缺少持仓→起始通货报价")
        elif not summary.settled:
            age = summary.oldest_quote_age or 0
            parts.append(f"按录入汇率估值 · 最老报价 {age_text(age)}前"
                         + (" · 已过期，仅供参考" if age > MAX_QUOTE_AGE else " · 非已实现利润"))
        if summary.profit is not None and summary.gold:
            efficiency = float(summary.profit * 1_000_000 / summary.gold)
            parts.append(f"每百万金币 {'净收益' if summary.settled else '估值收益'} {efficiency:+,.1f} {item_name(start)}")
        self.trade_balances.setText(" · ".join(parts))

    def _scout_is_recent(self, now: int | None = None) -> bool:
        return bool(
            self.scout_snapshot
            and self.scout_snapshot.get("league") == self.league.currentText()
            and 0 <= snapshot_age(self.scout_snapshot, now) <= MAX_AGE_SECONDS
        )

    def _recent_history_hour(self, now: int | None = None) -> int | None:
        hour = latest_market_hour(self.snapshot.get("markets", []), self.league.currentText())
        if hour is None:
            return None
        current = now if now is not None else int(time.time())
        return hour if 0 <= current - hour <= MAX_HOURLY_LEAD_AGE_SECONDS else None

    def _lead_source(self, now: int | None = None) -> str:
        if self._scout_is_recent(now):
            return "scout"
        if self._recent_history_hour(now) is not None:
            return "history"
        return "none"

    def _edges(self):
        source = self._lead_source()
        if source == "scout":
            return scout_edges(self.scout_snapshot)
        if source == "history":
            return historical_edges(self.snapshot.get("markets", []), self.league.currentText())
        return {}

    def _update_lead_source_label(self, now: int):
        if self._active_lead_source == "scout":
            source = f"Scout 快照 {age_text(snapshot_age(self.scout_snapshot, now))}前"
        elif self._active_lead_source == "history":
            hour = latest_market_hour(self.snapshot.get("markets", []), self.league.currentText())
            source = f"小时历史 {age_text(max(0, now - hour))}前" if hour else "暂无近期数据"
        else:
            source = "暂无近期数据"
        label = f"历史线索 · {source} · 点击即选"
        if self.suggestion_label.text() != label:
            self.suggestion_label.setText(label)

    def _update_suggestions(self):
        self._active_lead_source = self._lead_source()
        self._update_lead_source_label(int(time.time()))
        edges = self._edges()
        scores = []
        for item in {target for source, target in edges if target not in CORE}:
            paths = indicative_paths(edges, item)
            if paths and paths[0][2] > 0:
                scores.append((paths[0][2], item, paths[0][0], paths[0][1]))
        scores.sort(reverse=True)
        self._populate_item_select(scores)
        visible_scores = scores[:len(self.suggestions)]
        for button, recommendation in zip(self.suggestions, visible_scores):
            gain, item, start, exit_currency = recommendation
            button.setText(
                f"{item_name(item)} · {item_name(start)}买→{item_name(exit_currency)}卖 · "
                f"{float(gain):+.1%}"
            )
            button.setIcon(item_icon(item))
            button.setToolTip(
                f"历史线索：{item_name(start)} → {item_name(item)} → "
                f"{item_name(exit_currency)} · {float(gain):+.1%}；实际利润需当前报价核验"
            )
            button.setProperty("suggestion", (item, start, exit_currency))
            button.setEnabled(True)
        for button in self.suggestions[len(visible_scores):]:
            button.setText("暂无历史线索")
            button.setIcon(item_icon(EXALTED))
            button.setProperty("suggestion", None)
            button.setEnabled(False)
        self._update_reference()

    def _populate_item_select(self, scores: list[tuple]):
        leads = {item: (gain, start, exit_currency)
                 for gain, item, start, exit_currency in scores}
        items = [item for item in catalog() if item not in CORE]
        items.sort(key=lambda item: (-leads[item][0] if item in leads else Fraction(1),
                                     item_name(item)))
        self.item_select.blockSignals(True)
        self.item_select.clear()
        self.item_select.addItem("按历史线索选择物品…", None)
        for item in items:
            label = item_name(item)
            if item in leads:
                gain, start, exit_currency = leads[item]
                label += (f" · {item_name(start)}买→{item_name(exit_currency)}卖"
                          f" · 参考 {float(gain):+.1%}")
            self.item_select.addItem(item_icon(item), label, item)
        index = self.item_select.findData(self.selected_item)
        self.item_select.setCurrentIndex(max(0, index))
        self.item_select.blockSignals(False)

    def _item_combo_changed(self):
        item = self.item_select.currentData()
        if item:
            self.select_item(item)

    def _update_recent_buttons(self):
        for button, item in zip(self.recent_buttons, self.recent_items):
            button.setText(item_name(item))
            button.setIcon(item_icon(item))
            button.setToolTip(f"再次选择 {item_name(item)}")
            button.setProperty("recent_item", item)
            button.show()
        for button in self.recent_buttons[len(self.recent_items):]:
            button.setProperty("recent_item", None)
            button.hide()

    def _apply_suggestion(self, selected):
        if not selected:
            return
        item, start, exit_currency = selected
        self.select_item(item, (start, exit_currency))

    def _filter_items(self, query: str):
        self.item_results.clear()
        query = query.strip().casefold()
        if not query:
            self.item_results.hide()
            return
        matches = []
        for item, record in catalog().items():
            if item in CORE:
                continue
            name = item_name(item)
            english = record.get("en", "")
            if query in name.casefold() or query in english.casefold():
                matches.append((name, english, item))
        for name, english, item in sorted(matches)[:8]:
            label = f"{name} · {english}" if english and english != name else name
            entry = QListWidgetItem(item_icon(item), label)
            entry.setData(Qt.ItemDataRole.UserRole, item)
            self.item_results.addItem(entry)
        self.item_results.setVisible(self.item_results.count() > 0)

    def _choose_first_match(self):
        if self.item_results.isVisible() and self.item_results.count():
            self._choose_item_result(self.item_results.item(0))

    def _choose_item_result(self, entry: QListWidgetItem):
        self.select_item(entry.data(Qt.ItemDataRole.UserRole))

    def select_item(self, item: str, route: tuple[str, str] | None = None):
        if item not in catalog() or item in CORE:
            return
        if route is None:
            paths = indicative_paths(self._edges(), item)
            if paths and paths[0][2] > 0:
                route = paths[0][:2]
        self.selected_item = item
        self.recent_items = [item] + [saved for saved in self.recent_items if saved != item][:4]
        self._update_recent_buttons()
        index = self.item_select.findData(item)
        if index >= 0:
            self.item_select.blockSignals(True)
            self.item_select.setCurrentIndex(index)
            self.item_select.blockSignals(False)
        self.item_search.setText(item_name(item))
        self.item_results.clear()
        self.item_results.hide()
        if route is not None:
            for selector, currency in ((self.start_select, route[0]),
                                       (self.exit_select, route[1])):
                index = selector.findData(currency)
                if index >= 0:
                    selector.blockSignals(True)
                    selector.setCurrentIndex(index)
                    selector.blockSignals(False)
        self._update_reference()
        self._route_changed()

    def _update_reference(self):
        if not self.selected_item:
            self.reference.setText("选择物品后显示历史线索")
            return
        if self._lead_source() == "none":
            self.reference.setText("近期行情已过期，暂不推荐历史线索；请刷新后再查看")
            return
        paths = indicative_paths(self._edges(), self.selected_item)
        hour = latest_market_hour(self.snapshot.get("markets", []), self.league.currentText())
        source = "Scout 快照" if self._scout_is_recent() else "小时历史"
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
            quote = self.quotes.get(pair) if pair else None
            row.set_pair(pair, quote)
            if role == "买入" and pair and quote is None:
                known_fee = next((saved.gold for saved in self.quotes.values()
                                  if saved.target == item and saved.gold is not None), None)
                if known_fee is not None:
                    row.gold.setText(str(known_fee))
            row.set_depth(self.depth_books.get(pair) if pair else None)
        self._update_trade_path()
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
        if not all(values[:2]):
            self.quotes.pop(row.pair, None)
            self.depth_books.pop(row.pair, None)
            self.pending_depth.pop(row.pair, None)
            self._save_settings()
            self.refresh()
            return
        try:
            pay, receive, normalized = normalize_quote_amounts(values[0], values[1])
            stock = int(values[2]) if values[2] else None
            gold = None
            if row.pair[1] not in CORE and values[3]:
                gold = int(values[3])
            quote = Quote(*row.pair, pay, receive, stock, int(time.time()), gold)
            quote.validate()
            if normalized:
                row.pay.setText(str(pay))
                row.receive.setText(str(receive))
            self.quotes[row.pair] = quote
            self._commit_depth(row.pair, quote)
            self._save_settings()
            row.update_age(quote, quote.observed_at)
        except ValueError as exc:
            self.capture_hint.setText(f"{row.role}报价无效：{exc}")
            return
        if row.pair[1] in CORE:
            message = "报价已录入；基础通货金币费按获得数量自动计算。"
        elif normalized:
            message = "小数已换算为最小整数订单；每个物品金币按实际获得数量计算。"
        else:
            message = "报价已录入；请填写换得 1 个交易物品所需金币。"
        self.capture_hint.setText(message)
        row.set_depth(self.depth_books.get(row.pair))
        self.refresh()

    def _commit_depth(self, pair: tuple[str, str], quote: Quote):
        pending = self.pending_depth.pop(pair, None)
        if pending is not None:
            levels = pending.levels
            first = levels[0]
            if (first.pay, first.receive, first.stock) == (quote.pay, quote.receive, quote.stock):
                self.depth_books[pair] = Book(*pair, levels, quote.observed_at)
                return
        existing = self.depth_books.get(pair)
        if existing is not None:
            first = existing.levels[0]
            if (first.pay, first.receive, first.stock) != (quote.pay, quote.receive, quote.stock):
                self.depth_books.pop(pair, None)

    def edit_core_rate(self, source: str, target: str):
        self.core_pair = (source, target)
        for card in self.rate_cards:
            card.editor.hide()
            if {card.a, card.b} == {source, target}:
                self.active_core_card = card
        self.core_fields = self.active_core_card.fields
        self.active_core_card.edit(source, target, self.quotes.get(self.core_pair))

    def save_core_rate(self):
        values = [field.text().strip().replace(",", "") for field in self.core_fields]
        try:
            pay, receive, normalized = normalize_quote_amounts(values[0], values[1])
            stock = int(values[2]) if values[2] else None
            quote = Quote(*self.core_pair, pay, receive, stock, int(time.time()))
            quote.validate()
        except (ValueError, AttributeError) as exc:
            self.status.setText(f"核心汇率无效：{exc}")
            self.active_core_card.edit_title.setText(f"报价无效：{exc}")
            return
        if normalized:
            self.core_fields[0].setText(str(pay))
            self.core_fields[1].setText(str(receive))
        self.quotes[self.core_pair] = quote
        self._commit_depth(self.core_pair, quote)
        self._save_settings()
        self.active_core_card.editor.hide()
        self._route_changed()
        self.capture_hint.setText("基础通货金币费按获得数量自动计算，无需逐笔填写。")

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
            pair = self.core_pair
        else:
            row = self.rows.get(self.capture_role)
            if row is None or row.pair is None:
                return
            fields = (row.pay, row.receive, row.stock, row.gold)
            pair = row.pair
        order = resolve_capture_order(panel.get("selected_order"),
                                      ladder.get("best_quote") if ladder else None)
        if not order:
            self.capture_hint.setText("未可靠识别订单；请手动填写，或调整游戏交易栏后重读。")
            return
        fields[0].setText(str(order["pay"]))
        fields[1].setText(str(order["receive"]))
        fields[2].setText(str(ladder["stock"]) if ladder and ladder.get("stock") else "")
        item_gold = None
        if pair[1] not in CORE and order.get("gold") is not None:
            try:
                item_gold = item_unit_gold(int(order["gold"]), int(order["receive"]))
            except (TypeError, ValueError):
                pass
        if len(fields) > 3:
            fields[3].setText(str(item_gold) if item_gold is not None else "")
        self.pending_depth.pop(pair, None)
        depth_count = 0
        if ladder and ladder.get("stock"):
            try:
                selected = Quote(*pair, int(order["pay"]), int(order["receive"]),
                                 int(ladder["stock"]), int(time.time()), item_gold)
                selected.validate()
                book = captured_book(selected, ladder)
                if book:
                    self.pending_depth[pair] = book
                    depth_count = len(book.levels)
            except (TypeError, ValueError):
                pass
        # The game direction cannot be inferred reliably from a numeric crop.
        # Keep the OCR values visible for the user to check before they are used.
        next_step = ("然后点击「保存报价」。" if self.capture_role == "核心" else
                     "然后点击本行「保存读取」。")
        if depth_count > 1:
            depth_hint = f"同时识别到 {depth_count} 档价格/库存；多档利润仍需核对游戏合计订单。"
        elif not ladder or not ladder.get("stock"):
            depth_hint = "未读到库存梯度，请校准窗口底部的「比率/库存」区域，或手动填写库存。"
        else:
            depth_hint = "仅读到首档库存，暂按单档计算。"
        self.capture_hint.setText(
            f"已读到支付 {order['pay']}、获得 {order['receive']}。{depth_hint}请核对方向和库存，{next_step}"
        )

    def refresh(self, now: int | None = None):
        if not hasattr(self, "rows"):
            return
        now = now or int(time.time())
        if self._active_lead_source != self._lead_source(now):
            self._update_suggestions()
        self._update_lead_source_label(now)
        references = self._edges()
        for card in self.rate_cards:
            card.refresh(self.quotes, references, now, self.depth_books)
        for row in self.rows.values():
            row.update_age(self.quotes.get(row.pair) if row.pair else None, now)
        self._refresh_trades(now)
        item = self.selected_item
        start, exit_currency = self.start_select.currentData(), self.exit_select.currentData()
        if not item or start == exit_currency:
            self.progress.setText("先选择物品与两种不同的核心通货")
            self._empty_result("选好后，依次录入买入、卖出、换回三笔游戏订单。", "待选择交易方向")
            return
        pairs = ((start, item), (item, exit_currency), (exit_currency, start))
        steps = list(zip(("买入", "卖出", "换回"), pairs))
        missing = [(role, pair) for role, pair in steps if pair not in self.quotes]
        self.progress.setText("   ·   ".join(
            f"{'✓' if pair in self.quotes else '○'} {role} {item_name(pair[0])}→{item_name(pair[1])}"
            for role, pair in steps
        ))
        if missing:
            self._empty_result(
                "请在游戏中核对并填写：" + "、".join(role for role, _ in missing)
                + ("。忽略库存模式只需支付和获得；金币用于每百万金币收益。"
                   if self.ignore_stock.isChecked() else
                   "。每笔需支付和获得；库存决定可成交轮数，金币用于每百万金币收益。"),
                f"已录入 {3 - len(missing)}/3 笔报价",
            )
            return
        buy, sell, convert = (self.quotes[pair] for pair in pairs)
        ignore_stock = self.ignore_stock.isChecked()
        depth_notice = ""
        if not ignore_stock and all(quote.stock is not None for quote in (buy, sell, convert)):
            books = tuple(self.depth_books.get(pair) or Book.from_quote(quote)
                          for pair, quote in zip(pairs, (buy, sell, convert)))
            if any(len(book.levels) > 1 for book in books):
                try:
                    depth_plan = estimate_depth(*books, self.stock_mode.currentData())
                except ValueError as exc:
                    self._empty_result(str(exc), "多档报价无法连接")
                    return
                if depth_plan is not None:
                    self._show_depth_plan(depth_plan, (buy, sell, convert), books, now)
                    return
                depth_notice = "已读取多档，但仍未找到可完成换回的整数订单；以下仅按首档比例测算。"
        try:
            outcome = evaluate(buy, sell, convert, now=now,
                               require_fresh=False, require_stock=False)
        except ValueError as exc:
            self._empty_result(str(exc), "报价无法完成整数交易")
            return
        reference_only = ignore_stock or not outcome.is_fresh or outcome.max_rounds < 1
        if ignore_stock:
            state = "理论价差 · 忽略库存" + (" · 报价需更新" if not outcome.is_fresh else "")
        elif any(quote.stock is None for quote in (buy, sell, convert)):
            state = "参考测算 · 库存未录入"
        elif outcome.max_rounds < 1:
            state = "参考测算 · 库存不足"
        elif not outcome.is_fresh:
            state = "参考测算 · 请重新核价"
        elif outcome.profit <= 0:
            state = "近期报价 · 无利润"
        elif outcome.exact_gold is None:
            state = "通货盈利 · 金币待核验"
        else:
            state = "近期报价 · 盈利待核验"
        self.result_state.setText(state)
        self.profit.setText(f"{outcome.profit:+,} {item_name(start)}")
        if reference_only:
            color = "#e9c170"
        elif outcome.profit <= 0:
            color = "#f29a8d"
        elif outcome.exact_gold is None:
            color = "#e9c170"
        else:
            color = "#89e4ae"
        self.profit.setStyleSheet(f"color:{color}")
        self.profit_caption.setText(
            f"三笔换回后的测算 · 最老报价 {age_text(outcome.oldest_age)}前"
            + (" · 仅供参考" if reference_only else " · 下单前复核")
        )
        values = {
            "本次投入": f"{outcome.start_amount:,} {item_name(start)}",
            "买入获得物品": f"{outcome.item_amount:,} {item_name(item)}",
            "订单手数（买/卖/换）": (f"买 {outcome.buy_lots} · 卖 {outcome.sell_lots}"
                              f" · 换 {outcome.convert_lots}"),
            "最多完整轮数": "未校验" if ignore_stock else str(outcome.max_rounds),
            "收益率": f"{float(outcome.roi):+.2%}",
            "三笔金币": f"{outcome.exact_gold:,}" if outcome.exact_gold is not None else "请填每个物品金币",
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
        stock_needs = (
            ("买入", outcome.item_amount, buy.stock, item),
            ("卖出", outcome.exit_amount, sell.stock, exit_currency),
            ("换回", outcome.final_amount, convert.stock, start),
        )
        shortages = [
            f"{role}需 {needed:,} {item_name(currency)}、"
            + (f"当前可获 {available:,}" if available is not None else "库存未录入")
            for role, needed, available, currency in stock_needs
            if not ignore_stock and (available is None or available < needed)
        ]
        stock_warning = ""
        if shortages:
            stock_warning = "库存不足：" + "；".join(shortages) + "。以上仅为理论价差。"
        if ignore_stock:
            stock_warning = "已忽略库存与多档撮合，只按首档汇率计算；不代表可成交利润。"
        self.result_detail.setText(
            order_plan
            + depth_notice
            + stock_warning
            + ("报价或录入间隔已超过 3 分钟；数字保留供比较，不代表当前可成交利润。请更新三笔报价。"
               if not outcome.is_fresh else "")
            + ("请填写每个交易物品所需金币；基础通货金币按获得数量计算。"
               if outcome.exact_gold is None else "报价与库存随时可能变化，下单前再核对。")
        )

    def _show_depth_plan(self, plan: DepthPlan, quotes: tuple[Quote, Quote, Quote],
                         books: tuple[Book, Book, Book], now: int):
        buy, sell, convert = quotes
        oldest = max(0, now - min(book.observed_at for book in books))
        estimated_profit = plan.estimated_profit
        if oldest > MAX_QUOTE_AGE:
            state = "多档预估 · 报价需更新"
        elif estimated_profit > 0:
            state = "多档预估 · 待游戏核验"
        else:
            state = "多档预估 · 无利润"
        self.result_state.setText(state)
        profit_text = f"{float(estimated_profit):+,.4f}".rstrip("0").rstrip(".")
        self.profit.setText(f"≈{profit_text} {item_name(buy.source)}")
        self.profit.setStyleSheet("color:#e9c170" if estimated_profit > 0 else "color:#f29a8d")
        self.profit_caption.setText(
            f"整手成交 + 剩余持仓估值 · 最老报价 {age_text(oldest)}前"
        )
        fills = plan.buy, plan.sell, plan.convert
        gold = None
        if buy.gold is not None:
            gold = (plan.buy.received * buy.gold
                    + core_gold_cost(sell.target, plan.sell.received)
                    + core_gold_cost(convert.target, plan.convert.received))
        values = {
            "本次投入": f"{plan.buy.spent:,} {item_name(buy.source)}",
            "买入获得物品": f"{plan.buy.received:,} {item_name(buy.target)}",
            "订单手数（买/卖/换）": (f"买 {plan.buy.lots} · 卖 {plan.sell.lots}"
                              f" · 换 {plan.convert.lots}"),
            "最多完整轮数": "待游戏核验",
            "收益率": f"≈{float(plan.estimated_roi):+.2%}（含剩余估值）",
            "三笔金币": f"≈{gold:,}（估）" if gold is not None else "金币未填齐",
            "每百万金币收益": (f"≈{float(estimated_profit * 1_000_000 / gold):+,.1f} {item_name(buy.source)}（估）"
                              if gold else "金币待核验"),
        }
        for label, value in values.items():
            self.metrics[label].setText(value)
        level_details = []
        for role, fill, book in zip(("买入", "卖出", "换回"), fills, books):
            levels = effective_levels(book, self.stock_mode.currentData())
            used = " + ".join(
                f"{index + 1}档×{count}（付{levels[index].pay}得{levels[index].receive}）"
                for index, count, _ in fill.used
            )
            level_details.append(f"{role}：{used}")
        limitations = "只扫描前 10,000 个买入手数。" if not plan.scanned_all else ""
        self.result_detail.setText(
            f"逐档：{'；'.join(level_details)}。"
            f"支付 {plan.buy.spent:,} {item_name(buy.source)}，整手实际可换回 "
            f"{plan.convert.received:,} {item_name(buy.source)}。"
            f"剩余 {plan.remaining_item:,} {item_name(buy.target)}、"
            f"{plan.remaining_exit:,} {item_name(sell.target)}，"
            f"按首档有向汇率折算约 {float(plan.remaining_value):,.4f} {item_name(buy.source)}；"
            "这部分小数仅为估值，未实际兑换，也未计入额外兑换金币。"
            f"库存口径：{self.stock_mode.currentText()}；金币按各项实际获得数量与单位费率计算。"
            "OCR 比率和库存可能有误，请在游戏里输入合计数量核对实际支付、获得及金币。"
            + limitations
        )

    def _empty_result(self, reason: str, state: str):
        self.result_state.setText(state)
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

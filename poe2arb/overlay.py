from __future__ import annotations

import io
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QAbstractNativeEventFilter, QPoint, QRect, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QBrush, QColor, QFont, QFontMetrics, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QLineEdit, QMenu, QMessageBox, QPushButton, QScrollArea, QSystemTrayIcon,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from .core import LiveQuote, find_candidates, historical_edges, latest_market_hour, simulate_exact, simulate_reference_units
from .catalog import item_icon, item_name, item_pixmap, item_tooltip, record
from .data import app_data_dir, leagues, load_snapshot, sync_recent
from .workflow import QuoteBook, CORE
from .roundtrip import analyze_round_trip, analyze_sized_cycle, find_single_item_candidates
from .scout import MAX_AGE_SECONDS, fetch_scout_snapshot, load_scout_snapshot, scout_candidates, snapshot_age

DEFAULT_CAPTURE_DELAY_MS = 650
HOVER_CAPTURE_DELAY_MS = 2500


def short_name(item_id: str) -> str:
    return item_name(item_id)


def compact_number(value: float | None) -> str:
    if value is None:
        return "—"
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:+.1f}M"
    if abs(value) >= 1_000:
        return f"{value / 1_000:+.1f}k"
    return f"{value:+.1f}"


def parse_integer_field(text: str, label: str, minimum: int = 1) -> int:
    normalized = text.strip().replace(",", "")
    if not normalized:
        raise ValueError(f"{label}未填写")
    try:
        value = int(normalized)
    except ValueError:
        raise ValueError(f"{label}必须是整数") from None
    if value < minimum:
        requirement = "不能为负数" if minimum == 0 else "必须为正整数"
        raise ValueError(f"{label}{requirement}")
    return value


def settings_path() -> Path:
    return app_data_dir() / "settings.json"


def read_settings() -> dict:
    try:
        return json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_settings(settings: dict) -> None:
    settings_path().write_text(json.dumps(settings, ensure_ascii=False), encoding="utf-8")


class Job(QThread):
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, function, *args):
        super().__init__()
        self.function, self.args = function, args

    def run(self):
        try:
            self.done.emit(self.function(*self.args))
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class CalibrationWindow(QWidget):
    selected = Signal(object)
    finished = Signal()

    def __init__(self, image, label="交易报价区域"):
        super().__init__()
        self.image = image
        self.label = label
        self.start: QPoint | None = None
        self.end: QPoint | None = None
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.showFullScreen()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.drawPixmap(self.rect(), self.image)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 70))
        painter.setPen(QColor("#ffffff"))
        painter.setFont(QFont("Microsoft YaHei", 16))
        painter.drawText(30, 44, f"拖动框选{self.label} · Esc 取消")
        if self.start and self.end:
            rect = QRect(self.start, self.end).normalized()
            painter.drawPixmap(rect, self.image, QRect(
                round(rect.x() * self.image.width() / self.width()),
                round(rect.y() * self.image.height() / self.height()),
                round(rect.width() * self.image.width() / self.width()),
                round(rect.height() * self.image.height() / self.height()),
            ))
            painter.setPen(QPen(QColor("#76ddb0"), 3))
            painter.drawRect(rect)

    def mousePressEvent(self, event):
        self.start = event.position().toPoint()
        self.end = self.start
        self.update()

    def mouseMoveEvent(self, event):
        if self.start:
            self.end = event.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, event):
        if not self.start:
            return
        rect = QRect(self.start, event.position().toPoint()).normalized()
        if rect.width() < 30 or rect.height() < 20:
            return
        sx, sy = self.image.width() / self.width(), self.image.height() / self.height()
        bbox = [round(rect.left() * sx), round(rect.top() * sy), round(rect.right() * sx), round(rect.bottom() * sy)]
        self.selected.emit({"bbox": bbox, "screen": [self.image.width(), self.image.height()]})
        self.close()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.close()

    def closeEvent(self, event):
        self.finished.emit()
        super().closeEvent(event)


class WinHotkeyFilter(QAbstractNativeEventFilter):
    def __init__(self, overlay):
        super().__init__()
        self.overlay = overlay

    def nativeEventFilter(self, event_type, message):
        if sys.platform != "win32":
            return False, 0
        import ctypes
        from ctypes import wintypes
        try:
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == 0x0312:  # WM_HOTKEY
                if msg.wParam == 1:
                    QTimer.singleShot(0, self.overlay.toggle_visible)
                    return True, 0
                if msg.wParam == 2:
                    QTimer.singleShot(0, self.overlay.toggle_compact)
                    return True, 0
        except (TypeError, ValueError):
            pass
        return False, 0


@dataclass
class LegFields:
    pay: QLineEdit
    receive: QLineEdit
    stock: QLineEdit
    gold: QLineEdit
    observed_at: int = 0


class RouteStrip(QWidget):
    """A compact sequence of real item icons and names for one cycle."""

    def __init__(self, path: tuple[str, ...]):
        super().__init__()
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setStyleSheet("QWidget,QLabel { background:transparent; border:0; }")
        row = QHBoxLayout(self)
        row.setContentsMargins(4, 2, 4, 2)
        row.setSpacing(2)
        for index, item_id in enumerate(path):
            if index:
                arrow = QLabel("›")
                arrow.setStyleSheet("color:#889b92; font-size:18px;")
                arrow.setFixedWidth(10)
                row.addWidget(arrow)
            token = QWidget()
            token.setToolTip(item_tooltip(item_id))
            token.setFixedWidth(68)
            stack = QVBoxLayout(token)
            stack.setContentsMargins(0, 0, 0, 0)
            stack.setSpacing(1)
            picture = QLabel()
            picture.setPixmap(item_pixmap(item_id, 23))
            picture.setAlignment(Qt.AlignmentFlag.AlignCenter)
            name = QLabel()
            name.setAlignment(Qt.AlignmentFlag.AlignCenter)
            name.setStyleSheet("color:#dce8df; font-size:10px;")
            name.setText(QFontMetrics(name.font()).elidedText(item_name(item_id), Qt.TextElideMode.ElideRight, 66))
            stack.addWidget(picture)
            stack.addWidget(name)
            row.addWidget(token)
        row.addStretch()


def item_badge(item_id: str) -> QWidget:
    badge = QWidget()
    badge.setToolTip(item_tooltip(item_id))
    row = QHBoxLayout(badge)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(4)
    picture = QLabel()
    picture.setPixmap(item_pixmap(item_id, 20))
    picture.setFixedSize(21, 21)
    row.addWidget(picture)
    name = QLabel(item_name(item_id))
    row.addWidget(name)
    return badge


class Overlay(QWidget):
    def __init__(self):
        super().__init__()
        self.settings = read_settings()
        if "roi" not in self.settings:
            self.settings["roi"] = {"bbox": [588, 160, 1328, 345], "screen": [1920, 1080], "layout": "selected_trade_v1", "preset": True}
        self.snapshot = load_snapshot()
        self.scout_snapshot = load_scout_snapshot()
        self.cross_source = "历史线索"
        self.quote_book = QuoteBook()
        self.capture_pair = None
        self.ocr_stock = None
        self.management = False
        self.focus_pair = None
        self.focus_cross = None
        self.cross_rows = []
        self.rows = []
        self.reference_finals = {}
        self.reference_initial = 1000
        self.verified = {}
        self.current = None
        self.fields: list[LegFields] = []
        self.ocr_leg = 0
        self.ocr_order = None
        self.workers: list[Job] = []
        self.calibrator = None
        self.compact = False
        self._drag: QPoint | None = None
        self.setWindowTitle("PoE2 套利研究台")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMinimumWidth(520)
        self._build_ui()
        self._style()
        self._load_leagues()
        self._set_compact(False)
        self.move(self.settings.get("x", 40), self.settings.get("y", 80))
        self.show()
        self._setup_tray_and_hotkeys()
        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self.refresh_game)
        self.refresh_timer.start(15_000)

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.panel = QFrame()
        self.panel.setObjectName("panel")
        outer.addWidget(self.panel)
        root = QVBoxLayout(self.panel)
        root.setContentsMargins(14, 12, 14, 14)
        root.setSpacing(9)
        root.setAlignment(Qt.AlignmentFlag.AlignTop)
        header = QHBoxLayout()
        self.title = QLabel("◈  PoE2 通货路线助手")
        self.title.setObjectName("title")
        header.addWidget(self.title, 1)
        self.expand_button = self._button("收起", self.toggle_compact)
        header.addWidget(self.expand_button)
        self.manage_button = self._button("设置", self.toggle_management)
        header.addWidget(self.manage_button)
        header.addWidget(self._button("隐藏", self.hide))
        header.addWidget(self._button("×", QApplication.instance().quit))
        root.addLayout(header)
        self.summary = QLabel("最新成交小时供排查 · 游戏内订单决定盈亏")
        self.summary.setWordWrap(True)
        root.addWidget(self.summary)
        self._build_game_ui(root)
        self.details = QWidget()
        self.details.setMinimumHeight(850)
        self.details_scroll = QScrollArea()
        self.details_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.details_scroll.setWidgetResizable(True)
        self.details_scroll.setWidget(self.details)
        root.addWidget(self.details_scroll)
        layout = QVBoxLayout(self.details)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        filters = QHBoxLayout()
        self.league = QComboBox()
        self.base = QComboBox()
        self.hops = QComboBox()
        self.hops.addItems(["3 步", "4 步"])
        self.sort = QComboBox()
        self.sort.addItems(["整数收益线索", "小时成交量", "已核验净收益", "已核验金币效率"])
        filters.addWidget(self.league, 2)
        filters.addWidget(self.base, 2)
        filters.addWidget(self.hops, 1)
        layout.addLayout(filters)
        sort_row = QHBoxLayout()
        sort_row.addWidget(QLabel("排序"))
        sort_row.addWidget(self.sort, 1)
        sort_row.addWidget(QLabel("最薄弱成交量≥"))
        self.min_volume = QLineEdit("0")
        self.min_volume.setMaximumWidth(90)
        sort_row.addWidget(self.min_volume)
        layout.addLayout(sort_row)
        self.sort.currentIndexChanged.connect(self.scan)
        self.hops.currentIndexChanged.connect(self.scan)
        self.base.currentIndexChanged.connect(self.scan)
        self.league.currentIndexChanged.connect(self.scan)
        self.min_volume.editingFinished.connect(self.scan)
        controls = QHBoxLayout()
        controls.addWidget(self._button("更新历史行情", self.sync_history))
        controls.addWidget(self._button("更新 Scout 快照", self.sync_scout))
        controls.addWidget(self._button("演示数据", self.load_demo))
        controls.addWidget(self._button("查找路线", self.scan))
        layout.addLayout(controls)
        self.scout_label = QLabel(self._scout_status_text())
        self.scout_label.setObjectName("muted")
        layout.addWidget(self.scout_label)
        self.table = QTreeWidget()
        self.table.setHeaderLabels(["通货路线", "整数线索", "参考容量", "净收益", "每百万金"])
        self.table.headerItem().setToolTip(1, "按所填起始量逐跳取整；基于单小时成交均价与每跳 2% 假设价差，未扣金币费")
        self.table.headerItem().setToolTip(2, "同一小时最薄弱一跳的成交量，换算为起始通货；并非当前库存")
        self.table.headerItem().setToolTip(3, "只有核对游戏当前订单并复算后才显示")
        self.table.headerItem().setToolTip(4, "每消耗 100 万金币净增加的起始通货；只有复算后才显示")
        self.table.setRootIsDecorated(False)
        self.table.setAlternatingRowColors(True)
        self.table.setIndentation(0)
        self.table.setMinimumHeight(200)
        self.table.setMaximumHeight(230)
        for col, width in enumerate((405, 70, 70, 65, 85)):
            self.table.setColumnWidth(col, width)
        self.table.itemSelectionChanged.connect(self.show_route)
        layout.addWidget(self.table)
        self.warning = QLabel("单小时成交价差只是线索，不是预计利润。逐步打开游戏交易栏并核对当前订单。")
        self.warning.setObjectName("warning")
        self.warning.setWordWrap(True)
        layout.addWidget(self.warning)
        self.route_title = QLabel("选择上方路线，逐步核对游戏订单")
        self.route_title.setObjectName("section")
        layout.addWidget(self.route_title)
        guide = QLabel("每一步：游戏右侧「我拥有的」= 支付，左侧「我需要的」= 获得；可获数量另行确认。")
        guide.setObjectName("muted")
        guide.setWordWrap(True)
        layout.addWidget(guide)
        self.leg_host = QWidget()
        self.leg_layout = QVBoxLayout(self.leg_host)
        self.leg_layout.setContentsMargins(0, 0, 0, 0)
        self.leg_layout.addWidget(QLabel("选择路线后，在此核对每一步当前订单。"))
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setWidget(self.leg_host)
        self.scroll.setMinimumHeight(280)
        self.scroll.setMaximumHeight(290)
        layout.addWidget(self.scroll)
        capture_row = QHBoxLayout()
        capture_row.addWidget(self._button("校准交易栏位置", self.calibrate))
        self.roi_label = QLabel(self._roi_text())
        capture_row.addWidget(self.roi_label, 1)
        layout.addLayout(capture_row)
        stock_calibration = QHBoxLayout()
        stock_calibration.addWidget(self._button("校准比率/库存浮层", self.calibrate_stock))
        self.stock_roi_label = QLabel(self._stock_roi_text())
        stock_calibration.addWidget(self.stock_roi_label, 1)
        layout.addLayout(stock_calibration)
        self.ocr_text = QLabel("OCR 将读取游戏上方已选交易栏的数量和金币费。")
        self.ocr_text.setObjectName("muted")
        self.ocr_text.setWordWrap(True)
        layout.addWidget(self.ocr_text)
        amount_row = QHBoxLayout()
        self.amount_text = QLabel("已选交易：等待 OCR")
        amount_row.addWidget(self.amount_text, 1)
        amount_row.addWidget(self._button("填入已选交易", self.apply_order))
        layout.addLayout(amount_row)
        result_row = QHBoxLayout()
        result_row.addWidget(self._button("复算闭环收益", self.simulate))
        result_row.addWidget(self._button("Jev 辅助评估", self.evaluate_jev))
        result_row.addStretch()
        layout.addLayout(result_row)
        self.result = QLabel("先核对每一步的支付量、获得量、可获数量和金币费。")
        self.result.setObjectName("result")
        self.result.setWordWrap(True)
        layout.addWidget(self.result)
        self.last_model_state = None
        self.footer = QLabel("Ctrl+Shift+F8 显示/隐藏 · Ctrl+Shift+F9 展开/收起 · 拖动标题移动")
        self.footer.setObjectName("muted")
        layout.addWidget(self.footer)

    def _build_game_ui(self, root):
        self.game_host = QWidget()
        root.addWidget(self.game_host, alignment=Qt.AlignmentFlag.AlignTop)
        game = QVBoxLayout(self.game_host)
        game.setContentsMargins(0, 0, 0, 0)
        game.setSpacing(5)
        game.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.step_heading = QLabel("第 1 步 · 更新行情")
        self.step_heading.setObjectName("section")
        game.addWidget(self.step_heading)
        self.progress = QLabel("")
        self.progress.setObjectName("muted")
        game.addWidget(self.progress)
        self.start_sync = self._button("更新最近一小时行情", self.sync_history)
        game.addWidget(self.start_sync)
        mode_row = QHBoxLayout()
        self.view_mode = QComboBox()
        self.view_mode.addItems(["闭环路线", "基础双向", "单品跨币种"])
        self.view_mode.currentIndexChanged.connect(self.refresh_game)
        mode_row.addWidget(self.view_mode, 1)
        mode_row.addWidget(QLabel("起始量"))
        self.initial = QLineEdit("1000")
        self.initial.setToolTip("闭环路线按这个起始数量逐跳取整并排序；单位为所选起始通货")
        self.initial.setFixedWidth(65)
        self.initial.editingFinished.connect(self.scan)
        mode_row.addWidget(self.initial)
        mode_row.addWidget(QLabel("本金缓冲"))
        self.min_roi = QLineEdit("2.0")
        self.min_roi.setToolTip("单轮模式的风险缓冲阈值；单位 %，不等于保证利润")
        self.min_roi.setFixedWidth(55)
        self.min_roi.editingFinished.connect(self.refresh_game)
        mode_row.addWidget(self.min_roi)
        mode_row.addWidget(QLabel("%"))
        self.mode_host = QWidget()
        self.mode_host.setLayout(mode_row)
        game.addWidget(self.mode_host)
        self.task_scroll = QScrollArea()
        self.task_scroll.setWidgetResizable(True)
        self.task_scroll.setFixedHeight(198)
        self.task_host = QWidget()
        self.task_layout = QVBoxLayout(self.task_host)
        self.task_layout.setContentsMargins(4, 4, 4, 4)
        self.task_scroll.setWidget(self.task_host)
        game.addWidget(self.task_scroll)
        stock_row = QHBoxLayout()
        self.game_capture = QLabel("在游戏中选好当前方向，再点「读取」")
        self.game_capture.setObjectName("muted")
        self.game_capture.setWordWrap(True)
        stock_row.addWidget(self.game_capture, 1)
        self.stock_input = QLineEdit()
        self.stock_input.setPlaceholderText("可获库存")
        self.stock_input.setFixedWidth(80)
        stock_row.addWidget(self.stock_input)
        self.gold_input = QLineEdit()
        self.gold_input.setPlaceholderText("金币费")
        self.gold_input.setFixedWidth(80)
        stock_row.addWidget(self.gold_input)
        stock_row.addWidget(self._button("确认这笔报价", self.confirm_capture))
        self.capture_row = QWidget()
        self.capture_row.setLayout(stock_row)
        game.addWidget(self.capture_row)
        self.route_heading = QLabel("基础核价完成后显示推荐路线")
        game.addWidget(self.route_heading)
        self.game_routes = QTreeWidget()
        self.game_routes.setHeaderLabels(["路线", "进度", "整数线索"])
        self.game_routes.setRootIsDecorated(False)
        self.game_routes.setFixedHeight(160)
        self.game_routes.setColumnWidth(0, 300)
        self.game_routes.setColumnWidth(1, 60)
        self.game_routes.itemClicked.connect(self.select_game_route)
        game.addWidget(self.game_routes)
        self.round_table = QTreeWidget()
        self.round_table.setHeaderLabels(["单轮交易", "状态", "操作净赚", "每百万金"])
        self.round_table.setRootIsDecorated(False)
        self.round_table.setFixedHeight(160)
        self.round_table.headerItem().setToolTip(3, "每消耗 100 万金币净赚多少起始通货；按起始通货分组比较")
        for column, width in enumerate((210, 87, 76, 96)):
            self.round_table.setColumnWidth(column, width)
        self.round_table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.round_table.itemClicked.connect(self.select_round_row)
        game.addWidget(self.round_table)
        self.game_result = QLabel("")
        self.game_result.setObjectName("result")
        self.game_result.setWordWrap(True)
        game.addWidget(self.game_result)
        game.addStretch(1)
        self.refresh_game()

    def _button(self, title, callback):
        button = QPushButton(title)
        button.clicked.connect(callback)
        return button

    def _style(self):
        self.setStyleSheet("""
        QWidget { color:#e7eced; font:12px 'Microsoft YaHei','Segoe UI'; }
        QFrame#panel { background-color:rgba(18,22,26,165); border:1px solid rgba(129,147,145,105); border-radius:13px; }
        QLabel#title { font-size:15px; font-weight:bold; color:#eef9ef; }
        QLabel#section { font-size:13px; font-weight:bold; color:#d4e6d8; }
        QLabel#warning { color:#e0c27b; background:rgba(110,85,40,55); border-radius:6px; padding:6px; }
        QLabel#muted { color:#a8b8b5; font-size:11px; }
        QLabel#result { color:#8ee1b8; font-weight:bold; }
        QPushButton,QComboBox,QLineEdit { background:rgba(34,42,47,170); border:1px solid #52615f; border-radius:6px; padding:5px 7px; }
        QPushButton:hover { background:#3c5d50; }
        QTreeWidget,QScrollArea { background:rgba(15,20,24,125); border:1px solid rgba(70,84,81,130); border-radius:6px; }
        QTreeWidget { alternate-background-color:rgba(32,38,42,115); selection-background-color:#355849; }
        QHeaderView::section { background:#283038; color:#dce8e1; border:0; border-right:1px solid #40504b; padding:4px; }
        QScrollArea QWidget { background:transparent; }
        QTreeWidget::item:selected { background:#355849; }
        """)

    def _set_compact(self, compact):
        self.compact = compact
        self.game_host.setVisible(not compact and not self.management)
        self.details_scroll.setVisible(not compact and self.management)
        self.expand_button.setText("展开" if compact else "收起")
        self.setFixedSize(520 if not self.management else 760,
                          82 if compact else (790 if self.management else
                                             {"market": 195,
                                              "core": 295 if getattr(self, "capture_active", False) else 210,
                                              "choose": 405,
                                              "read": 535 if getattr(self, "capture_active", False) else 475,
                                              "result": 465}.get(getattr(self, "ui_stage", "market"), 210)))

    def toggle_management(self):
        self.management = not self.management
        self.manage_button.setText("清单" if self.management else "设置")
        self._set_compact(False)

    def toggle_compact(self):
        self._set_compact(not self.compact)

    def toggle_visible(self):
        if self.isVisible():
            self.hide()
        else:
            self.show()
            self.raise_()
            self.activateWindow()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and event.position().y() < 45:
            self._drag = event.globalPosition().toPoint() - self.pos()

    def mouseMoveEvent(self, event):
        if self._drag is not None:
            self.move(event.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, event):
        if self._drag is not None:
            self._drag = None
            self.settings.update({"x": self.x(), "y": self.y()})
            write_settings(self.settings)

    def _setup_tray_and_hotkeys(self):
        pix = QPixmap(32, 32)
        pix.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor("#69c699"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(3, 3, 26, 26)
        painter.end()
        self.tray = QSystemTrayIcon(QIcon(pix), self)
        menu = QMenu()
        show_action = QAction("显示/隐藏", self)
        show_action.triggered.connect(self.toggle_visible)
        menu.addAction(show_action)
        quit_action = QAction("退出", self)
        quit_action.triggered.connect(QApplication.instance().quit)
        menu.addAction(quit_action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self.toggle_visible() if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
        self.tray.show()
        self.hotkey_filter = None
        if sys.platform == "win32":
            import ctypes
            self.hotkey_filter = WinHotkeyFilter(self)
            QApplication.instance().installNativeEventFilter(self.hotkey_filter)
            hwnd = int(self.winId())
            mods = 0x0002 | 0x0004 | 0x4000  # Ctrl + Shift + no repeat
            self.hotkey_ok = bool(ctypes.windll.user32.RegisterHotKey(hwnd, 1, mods, 0x77))
            self.hotkey_ok2 = bool(ctypes.windll.user32.RegisterHotKey(hwnd, 2, mods, 0x78))
            if not self.hotkey_ok:
                self.summary.setText("热键注册失败：可用系统托盘图标呼出悬浮窗")

    def _load_leagues(self):
        current = self.league.currentText()
        self.league.clear()
        available = set(leagues(self.snapshot))
        if self.scout_snapshot:
            available.add(self.scout_snapshot["league"])
        self.league.addItems(sorted(available))
        if current:
            index = self.league.findText(current)
            if index >= 0:
                self.league.setCurrentIndex(index)
        if self.league.count():
            self.scan()

    def _job(self, function, on_done, *args):
        job = Job(function, *args)
        self.workers.append(job)
        job.done.connect(on_done)
        job.failed.connect(lambda message: self._error(message))
        job.finished.connect(lambda: self.workers.remove(job) if job in self.workers else None)
        job.start()

    def _error(self, message):
        self.summary.setText(message)
        self.result.setText(message)
        self.result.setStyleSheet("color:#ef9d8f; font-weight:bold;")

    def sync_history(self):
        self.summary.setText("正在更新 GGG 每小时历史数据…")
        self._job(sync_recent, self._synced)

    def _scout_status_text(self) -> str:
        if not self.scout_snapshot:
            return "Poe2Scout：尚无快照 · 单品候选使用历史数据"
        age = snapshot_age(self.scout_snapshot)
        freshness = "可筛选" if 0 <= age <= MAX_AGE_SECONDS else "已过期，停止参与排序"
        return (f"Poe2Scout {self.scout_snapshot['league']} · "
                f"快照 {max(0, age) // 60} 分钟前 · "
                f"{len(self.scout_snapshot['pairs']):,} 个交易对 · {freshness}")

    def sync_scout(self):
        league = self.league.currentText()
        if league.startswith("演示") or not league:
            league = None
        self.scout_label.setText("正在读取 Poe2Scout 快照…")
        self._job(fetch_scout_snapshot, self._scout_synced, league)

    def _scout_synced(self, snapshot):
        self.scout_snapshot = snapshot
        self._load_leagues()
        index = self.league.findText(snapshot["league"])
        if index >= 0:
            self.league.setCurrentIndex(index)
        self.scout_label.setText(self._scout_status_text())
        self.scan()

    def _synced(self, snapshot):
        self.snapshot = snapshot
        self.verified.clear()
        self.quote_book.quotes.clear()
        self.summary.setText(f"历史数据已更新：{len(snapshot.get('markets', []))} 条市场记录")
        self._load_leagues()

    def load_demo(self):
        path = Path(__file__).with_name("demo.json")
        demo = json.loads(path.read_text(encoding="utf-8"))
        self.snapshot = {"fetched_at": int(time.time()), "markets": demo["markets"], "last_id": demo["next_change_id"]}
        self.verified.clear()
        self.quote_book.quotes.clear()
        self._load_leagues()
        self.summary.setText("演示数据已加载 · 仅测试界面和计算")

    def scan(self):
        league = self.league.currentText()
        if not league:
            self.summary.setText("请先更新历史数据，或加载演示数据")
            return
        self.quote_book.reset(league)
        markets = self.snapshot.get("markets", [])
        hour = latest_market_hour(markets, league)
        hour_label = (time.strftime("%m-%d %H:00", time.localtime(hour))
                      if hour is not None else "演示数据")
        edges = historical_edges(markets, league)
        snapshot_is_usable = (self.scout_snapshot is not None
                              and self.scout_snapshot["league"] == league
                              and 0 <= snapshot_age(self.scout_snapshot) <= MAX_AGE_SECONDS)
        if snapshot_is_usable:
            self.cross_rows = scout_candidates(self.scout_snapshot)
            self.cross_source = f"Scout 参考价 {snapshot_age(self.scout_snapshot) // 60} 分钟前"
        else:
            self.cross_rows = find_single_item_candidates(edges)
            self.cross_source = f"{hour_label} 单小时线索"
        self.scout_label.setText(self._scout_status_text())
        old_base = self.base.currentData()
        currencies = sorted({edge.source for edge in edges.values()}, key=lambda x: sum(e.source_volume for e in edges.values() if e.source == x), reverse=True)
        self.base.blockSignals(True)
        self.base.clear()
        for currency in currencies:
            self.base.addItem(item_icon(currency, 20), short_name(currency), currency)
            self.base.setItemData(self.base.count() - 1, item_tooltip(currency), Qt.ItemDataRole.ToolTipRole)
        if old_base in currencies:
            self.base.setCurrentIndex(currencies.index(old_base))
        self.base.blockSignals(False)
        base = self.base.currentData()
        hops = 3 if self.hops.currentIndex() == 0 else 4
        try:
            min_volume = max(0, int(self.min_volume.text() or "0"))
        except ValueError:
            self.summary.setText("最低历史量须为整数")
            return
        rows = find_candidates(edges, base, lengths=(hops,)) if base else []
        try:
            initial = parse_integer_field(self.initial.text(), "起始数量")
        except ValueError as exc:
            self.summary.setText(str(exc))
            return
        self.reference_initial = initial
        self.reference_finals = {}
        for row in rows:
            if row.limiting_start_units < min_volume:
                continue
            final = simulate_reference_units(row.path, initial, edges)
            if final is not None and final > initial:
                self.reference_finals[row.path] = final
        rows = [row for row in rows if row.path in self.reference_finals]
        sort_index = self.sort.currentIndex()
        if sort_index == 0:
            rows.sort(key=lambda row: (self.reference_finals[row.path] - initial,
                                       row.reference_gain, row.limiting_start_units), reverse=True)
        elif sort_index == 1:
            rows.sort(key=lambda row: (row.limiting_start_units, row.reference_gain), reverse=True)
        elif sort_index == 2:
            rows.sort(key=lambda row: self.verified.get((league, row.key), {}).get("profit", float("-inf")), reverse=True)
        elif sort_index == 3:
            rows.sort(key=lambda row: self.verified.get((league, row.key), {}).get("gold_efficiency") if self.verified.get((league, row.key), {}).get("gold_efficiency") is not None else float("-inf"), reverse=True)
        self.rows = rows[:80]
        visible_paths = {row.path for row in self.rows}
        self.quote_book.selected_routes = [path for path in self.quote_book.selected_routes
                                           if path in visible_paths]
        selected_key = self.current.key if self.current else None
        self.table.blockSignals(True)
        self.table.clear()
        for row in self.rows:
            verified = self.verified.get((league, row.key), {})
            item = QTreeWidgetItem([
                "",
                f"+{(self.reference_finals[row.path] - initial) / initial:.1%}",
                f"{float(row.limiting_start_units):,.0f}",
                str(verified.get("profit", "—")),
                compact_number(verified.get("gold_efficiency")),
            ])
            item.setSizeHint(0, QSize(405, 58))
            item.setToolTip(0, " → ".join(item_tooltip(x).splitlines()[0] for x in row.path))
            item.setToolTip(1, f"{initial:,} → {self.reference_finals[row.path]:,} {short_name(row.path[0])}；"
                                "单小时成交均价逐跳取整，未扣金币费，需在游戏内核价")
            if verified.get("gold_efficiency") is not None:
                item.setToolTip(4, f"{verified['gold_efficiency']:+,.2f} {short_name(row.path[0])} / 100 万金币")
            item.setForeground(1, QBrush(QColor("#e0c27b")))
            if verified:
                item.setForeground(3, QBrush(QColor("#89deb1" if verified["profit"] > 0 else "#ef9d8f")))
            self.table.addTopLevelItem(item)
            self.table.setItemWidget(item, 0, RouteStrip(row.path))
            if row.key == selected_key:
                self.table.setCurrentItem(item)
        self.table.blockSignals(False)
        if selected_key and all(row.key != selected_key for row in self.rows):
            self.current = None
            self.fields.clear()
            self.last_model_state = None
            self.route_title.setText("选择上方路线，逐步核对游戏订单")
            self.result.setText("先核对每一步的支付量、获得量、可获数量和金币费。")
        checked = sum((league, row.key) in self.verified for row in self.rows)
        cross_summary = (f" · {len(self.cross_rows)} 条 Scout 单品候选"
                         if snapshot_is_usable else "")
        no_rows_hint = " · 可调整起始量或起始通货" if not self.rows else ""
        self.summary.setText(f"{league} · {hour_label} 成交小时 · 起始量 {initial:,} · "
                             f"{len(self.rows)} 条整数线索{cross_summary} · {checked} 条已核验{no_rows_hint}")
        self.refresh_game()

    def show_route(self):
        items = self.table.selectedItems()
        if not items:
            return
        index = self.table.indexOfTopLevelItem(items[0])
        if index < 0 or index >= len(self.rows):
            return
        self.current = self.rows[index]
        self.quote_book.select(self.current)
        self.last_model_state = None
        self.route_title.setText(
            f"当前路线 · {len(self.current.path) - 1} 步 · 整数线索 +{(self.reference_finals[self.current.path] - self.reference_initial) / self.reference_initial:.1%}"
        )
        self.route_title.setWordWrap(True)
        self.fields.clear()
        while self.leg_layout.count():
            item = self.leg_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for i in range(len(self.current.path)-1):
            line = QWidget()
            grid = QGridLayout(line)
            grid.setContentsMargins(0, 3, 0, 6)
            line.setMinimumHeight(88)
            leg_header = QWidget()
            leg_row = QHBoxLayout(leg_header)
            leg_row.setContentsMargins(0, 0, 0, 0)
            leg_row.setSpacing(6)
            leg_row.addWidget(QLabel(f"第 {i+1} 步"))
            leg_row.addWidget(item_badge(self.current.path[i]))
            leg_row.addWidget(QLabel("→"))
            leg_row.addWidget(item_badge(self.current.path[i+1]))
            leg_row.addStretch()
            grid.addWidget(leg_header, 0, 0, 1, 5)
            fields = []
            for col, label in enumerate(("支付数量", "获得数量", "可获数量", "金币费用")):
                grid.addWidget(QLabel(label), 1, col)
                box = QLineEdit()
                box.setPlaceholderText(label)
                if col == 2:
                    box.setToolTip("截图上方没有显示市场可获总量；请在游戏中核实后输入")
                box.textEdited.connect(lambda _text, n=i: self._mark_observed(n))
                grid.addWidget(box, 2, col)
                grid.setColumnStretch(col, 1)
                fields.append(box)
            button = self._button("OCR", lambda _checked=False, n=i: self.capture_fixed(n))
            button.setMinimumWidth(56)
            grid.addWidget(button, 2, 4)
            self.leg_layout.addWidget(line)
            self.fields.append(LegFields(*fields))
            cached = self.quote_book.get((self.current.path[i], self.current.path[i + 1]))
            if cached:
                self.fields[-1].pay.setText(str(cached.pay))
                self.fields[-1].receive.setText(str(cached.receive))
                self.fields[-1].stock.setText(str(cached.available_receive))
                self.fields[-1].gold.setText(str(cached.gold))
                self.fields[-1].observed_at = cached.observed_at
        self.leg_layout.addStretch()
        self.ocr_text.setText("OCR 将读取游戏上方已选交易栏的数量和金币费。")
        self.ocr_order = None
        self.amount_text.setText("已选交易：等待 OCR")
        self.result.setText("逐步填写当前订单的整数数量，并确认可获数量与金币费。")
        self.result.setStyleSheet("color:#a8b8b5; font-weight:normal;")
        self.refresh_game()

    def _mark_observed(self, index):
        if index < len(self.fields):
            self.fields[index].observed_at = int(time.time())

    def _roi_text(self) -> str:
        roi = self.settings.get("roi")
        if not roi:
            return "未校准"
        label = "示例预设" if roi.get("preset") else "已校准"
        return f"{label} {roi['bbox']}"

    def _stock_roi_text(self) -> str:
        roi = self.settings.get("stock_roi")
        return f"最优档 OCR {roi['bbox']}" if roi else "待框选浮层最上方比率与库存"

    def calibrate(self):
        self.calibration_kind = "trade"
        self.hide()
        QTimer.singleShot(DEFAULT_CAPTURE_DELAY_MS, self._open_calibrator)

    def calibrate_stock(self):
        self.calibration_kind = "stock"
        self.stock_roi_label.setText("2.5 秒后截图；请悬停显示比率/库存浮层")
        self.hide()
        QTimer.singleShot(HOVER_CAPTURE_DELAY_MS, self._open_calibrator)

    def _open_calibrator(self):
        from PIL import ImageGrab
        try:
            image = ImageGrab.grab(all_screens=False)
            stream = io.BytesIO()
            image.save(stream, format="PNG")
            pix = QPixmap()
            pix.loadFromData(stream.getvalue())
            label = ("浮层最上方比率与库存（可包含表头）"
                     if self.calibration_kind == "stock" else "交易报价区域")
            self.calibrator = CalibrationWindow(pix, label)
            self.calibrator.selected.connect(self._save_roi)
            self.calibrator.finished.connect(self.show)
        except Exception as exc:
            self.show()
            self._error(f"校准截图失败：{exc}")

    def _save_roi(self, roi):
        if self.calibration_kind == "stock":
            self.settings["stock_roi"] = {**roi, "layout": "stock_number_v1"}
            self.stock_roi_label.setText(self._stock_roi_text())
        else:
            self.settings["roi"] = {**roi, "layout": "selected_trade_v1"}
            self.roi_label.setText(self._roi_text())
        write_settings(self.settings)

    def capture_fixed(self, leg):
        self.ocr_leg = leg
        self.capture_pair = ((self.current.path[leg], self.current.path[leg+1])
                             if self.current and leg < len(self.current.path)-1 else None)
        roi = self.settings.get("roi")
        if not roi:
            self._error("请先校准固定 OCR 区域")
            return
        if leg < len(self.fields):
            self.fields[leg].observed_at = 0
        self.ocr_order = None
        self.amount_text.setText("已选交易：等待 OCR")
        delay = HOVER_CAPTURE_DELAY_MS if self.settings.get("stock_roi") else DEFAULT_CAPTURE_DELAY_MS
        if delay == HOVER_CAPTURE_DELAY_MS:
            self.game_capture.setText("2.5 秒后截图；请立即悬停游戏比率，显示最优比率与库存")
        self.hide()
        QTimer.singleShot(delay, self._capture_and_ocr)

    def _capture_and_ocr(self):
        from PIL import ImageGrab
        try:
            roi = self.settings["roi"]
            full = ImageGrab.grab(all_screens=False)
            if list(full.size) != roi["screen"]:
                raise ValueError("屏幕分辨率已变化，请重新校准 OCR 区域")
            image = full.crop(tuple(roi["bbox"]))
            stream = io.BytesIO()
            image.save(stream, format="PNG")
            stock_data = None
            stock_roi = self.settings.get("stock_roi")
            if stock_roi:
                if list(full.size) != stock_roi["screen"]:
                    raise ValueError("最优档 OCR 校准分辨率已变化，请重新校准")
                stock_stream = io.BytesIO()
                full.crop(tuple(stock_roi["bbox"])).save(stock_stream, format="PNG")
                stock_data = stock_stream.getvalue()
            self.show()
            self.ocr_text.setText("正在读取固定区域 OCR…")
            self._job(self._read_capture, self._ocr_done, stream.getvalue(), stock_data)
        except Exception as exc:
            self.show()
            self._error(f"OCR 截图失败：{exc}")

    @staticmethod
    def _read_capture(trade_data, stock_data):
        from .ocr import read_exchange_panel, read_stock_region
        result = read_exchange_panel(trade_data)
        result["stock_result"] = read_stock_region(stock_data) if stock_data else None
        return result

    def _ocr_done(self, result):
        stock_result = result.get("stock_result") or {}
        best_quote = stock_result.get("best_quote")
        self.ocr_stock = stock_result.get("stock")
        if best_quote:
            self.ocr_order = {
                "pay": best_quote["pay"],
                "receive": best_quote["receive"],
                "gold": None,
                "confidence": best_quote["confidence"],
            }
        else:
            self.ocr_order = result.get("selected_order")
        text = " | ".join(result["lines"]) or "没有识别到文本"
        self.ocr_text.setText(f"OCR {result['confidence']:.2f}：{text}。请确认方向与实际数量。")
        if self.ocr_order:
            order = self.ocr_order
            gold_text = f"{order['gold']:,}" if order.get("gold") is not None else "待手填"
            ratio_text = f"；最优档 {best_quote['ratio']}" if best_quote else ""
            self.amount_text.setText(
                f"支付右侧 {order['pay']} → 获得左侧 {order['receive']}；"
                f"金币 {gold_text}（{order['confidence']:.2f}）{ratio_text}"
            )
            if self.ocr_stock:
                self.stock_input.setText(str(self.ocr_stock))
            if order.get("gold") is not None:
                self.gold_input.setText(str(order["gold"]))
            self.game_capture.setText(
                f"已读 {order['pay']} → {order['receive']}，金币 {gold_text}；"
                + (f"库存 {self.ocr_stock}。核对方向后确认" if self.ocr_stock else "库存待 OCR 校准或手动确认")
            )
            if self.capture_pair and self.ocr_stock and order.get("gold") is not None:
                recognized = "".join(result.get("lines", [])).replace(" ", "")
                name_sets = [
                    {short_name(item_id), record(item_id).get("zh_tw", "")}
                    for item_id in self.capture_pair
                ]
                if all(any(name and name.replace(" ", "") in recognized for name in names)
                       for names in name_sets):
                    self.confirm_capture()
                else:
                    self.game_capture.setText(
                        self.game_capture.text() + "；未能同时核对通货名称，请手动确认方向"
                    )
        else:
            self.amount_text.setText("已选交易未可靠识别，请手动填写")
            self.game_capture.setText("报价 OCR 未可靠识别，请在设置页填写")
        if self.capture_pair:
            self.refresh_game()

    def apply_order(self):
        if not self.ocr_order or self.ocr_leg >= len(self.fields):
            return
        fields = self.fields[self.ocr_leg]
        fields.pay.setText(str(self.ocr_order["pay"]))
        fields.receive.setText(str(self.ocr_order["receive"]))
        if self.ocr_order.get("gold") is not None:
            fields.gold.setText(str(self.ocr_order["gold"]))
        self._mark_observed(self.ocr_leg)
        if self.ocr_stock:
            fields.stock.setText(str(self.ocr_stock))
        if self.ocr_order.get("gold") is None:
            fields.gold.setFocus()
            self.result.setText(
                f"第 {self.ocr_leg + 1} 跳最优报价与库存已填入；金币费用被浮层遮挡，请手动填写。"
            )
        elif self.ocr_stock:
            self.confirm_capture()
        else:
            fields.stock.setFocus()
            self.result.setText(
                f"第 {self.ocr_leg + 1} 跳报价已填入；可获数量未识别，请手动填写或重新校准库存数字位置。"
            )

    def confirm_capture(self):
        if not self.capture_pair or not self.ocr_order:
            self.game_capture.setText("先对清单中的交易对截图识别报价")
            return
        try:
            stock = parse_integer_field(self.stock_input.text(), "可获数量")
            gold = self.ocr_order.get("gold")
            if gold is None:
                gold = parse_integer_field(self.gold_input.text(), "金币费用", minimum=0)
            quote = LiveQuote(*self.capture_pair, self.ocr_order["pay"],
                              self.ocr_order["receive"], stock,
                              gold, int(time.time()))
            self.quote_book.put(quote)
        except (ValueError, TypeError) as exc:
            self.game_capture.setText(f"报价需确认：{exc}")
            return
        for index, fields in enumerate(self.fields):
            if self.current and (self.current.path[index], self.current.path[index+1]) == self.capture_pair:
                fields.pay.setText(str(quote.pay))
                fields.receive.setText(str(quote.receive))
                fields.stock.setText(str(quote.available_receive))
                fields.gold.setText(str(quote.gold))
                fields.observed_at = quote.observed_at
        self.game_capture.setText("✓ 已核对交易对；相同方向的路线已同步")
        self.capture_pair = None
        self.ocr_order = None
        self.stock_input.clear()
        self.gold_input.clear()
        self.refresh_game()

    def capture_task(self, pair):
        self.capture_pair = pair
        self.ocr_leg = -1
        self.ocr_order = None
        self.ocr_stock = None
        self.stock_input.clear()
        self.gold_input.clear()
        self.game_capture.setText(f"请在游戏中选好 {short_name(pair[0])} → {short_name(pair[1])}，正在截图…")
        if not self.settings.get("roi"):
            self.game_capture.setText("先在设置页校准交易栏")
            return
        delay = HOVER_CAPTURE_DELAY_MS if self.settings.get("stock_roi") else DEFAULT_CAPTURE_DELAY_MS
        if delay == HOVER_CAPTURE_DELAY_MS:
            self.game_capture.setText(
                f"请在游戏中选好 {short_name(pair[0])} → {short_name(pair[1])}；"
                "2.5 秒后截图，请立即悬停比率以显示最优档库存"
            )
        self.hide()
        QTimer.singleShot(delay, self._capture_and_ocr)

    def select_game_route(self, item, column):
        index = item.data(0, Qt.ItemDataRole.UserRole)
        if index is None or index >= len(self.rows):
            return
        candidate = self.rows[index]
        self.quote_book.select(candidate)
        self.current = candidate
        self.refresh_game()

    def select_round_trip(self, item, column):
        pair = item.data(0, Qt.ItemDataRole.UserRole)
        if pair:
            self.focus_pair = tuple(pair)
            self.refresh_game()

    def select_round_row(self, item, column):
        if self.view_mode.currentIndex() == 2:
            index = item.data(0, Qt.ItemDataRole.UserRole)
            if index is not None and 0 <= index < len(self.cross_rows):
                self.focus_cross = self.cross_rows[index]
                self.quote_book.select(self.focus_cross)
                self.refresh_game()
        else:
            self.select_round_trip(item, column)

    def _round_status(self, pair, now):
        first = self.quote_book.quotes.get(pair)
        second = self.quote_book.quotes.get((pair[1], pair[0]))
        if not first or not second:
            return "待读双向报价", "—", None
        try:
            result = analyze_round_trip(first, second, now=now)
        except ValueError as exc:
            return str(exc), "—", None
        try:
            threshold = float(self.min_roi.text()) / 100
        except ValueError:
            threshold = 0.02
        status = ("金币费待确认" if result.estimated_gold == 0 else
                  "可复核" if float(result.roi) >= threshold else "低于风险缓冲")
        return status, f"{float(result.roi):+.1%}", result

    def _cross_status(self, candidate, now):
        quotes = [self.quote_book.quotes.get(pair) for pair in zip(candidate.path, candidate.path[1:])]
        if any(quote is None for quote in quotes):
            return "待核价", "—", None
        try:
            result = analyze_sized_cycle(quotes, now=now)
        except ValueError as exc:
            return str(exc), "—", None
        try:
            threshold = float(self.min_roi.text()) / 100
        except ValueError:
            threshold = 0.02
        status = ("金币费待确认" if result.estimated_gold == 0 else
                  "可复核" if float(result.roi) >= threshold else "低于风险缓冲")
        return status, f"{float(result.roi):+.1%}", result

    def refresh_game(self):
        if not hasattr(self, "task_layout"):
            return
        while self.task_layout.count():
            part = self.task_layout.takeAt(0)
            widget = part.widget()
            if widget:
                widget.setParent(None)
                widget.deleteLater()
        pending = self.quote_book.pending()
        now = int(time.time())
        if self.focus_pair and self.view_mode.currentIndex() == 1:
            forward = self.focus_pair
            backward = (forward[1], forward[0])
            priority = []
            quotes = [self.quote_book.quotes.get(forward), self.quote_book.quotes.get(backward)]
            for pair, quote in zip((forward, backward), quotes):
                if quote is None or now - quote.observed_at > 45:
                    priority.append(pair)
            if not priority and quotes[0] and quotes[1] and abs(quotes[0].observed_at - quotes[1].observed_at) > 25:
                priority.append(forward if quotes[0].observed_at < quotes[1].observed_at else backward)
            pending = priority + [pair for pair in pending if pair not in priority]
        if self.focus_cross and self.view_mode.currentIndex() == 2:
            path_pairs = list(zip(self.focus_cross.path, self.focus_cross.path[1:]))
            quotes = [self.quote_book.quotes.get(pair) for pair in path_pairs]
            priority = [pair for pair, quote in zip(path_pairs, quotes)
                        if quote is None or now - quote.observed_at > 45]
            times = [quote.observed_at for quote in quotes if quote]
            if not priority and len(times) == len(quotes) and max(times) - min(times) > 25:
                priority = [path_pairs[times.index(min(times))]]
            pending = priority + [pair for pair in pending if pair not in priority]
        core_pairs = [(a, b) for a in CORE for b in CORE if a != b]
        core_done = sum(self.quote_book.get(pair) is not None for pair in core_pairs)
        self.core_complete = core_done == 6
        self.has_pending = bool(pending)
        show_rounds = self.view_mode.currentIndex() == 1
        show_cross = self.view_mode.currentIndex() == 2
        has_candidates = bool(self.cross_rows) if show_cross else (True if show_rounds else bool(self.rows))
        has_market = bool(self.snapshot.get("markets"))
        has_choice = (bool(self.focus_pair) if show_rounds else
                      bool(self.focus_cross) if show_cross else
                      bool(self.quote_book.selected_routes))
        if show_rounds and self.focus_pair:
            active_pairs = {self.focus_pair, (self.focus_pair[1], self.focus_pair[0])}
        elif show_cross and self.focus_cross:
            active_pairs = set(zip(self.focus_cross.path, self.focus_cross.path[1:]))
        else:
            active_pairs = {pair for path in self.quote_book.selected_routes
                            for pair in zip(path, path[1:])}
        active_pending = [pair for pair in pending if pair in active_pairs]
        if not has_market:
            self.ui_stage = "market"
        elif core_done < 6:
            self.ui_stage = "core"
        elif not has_choice:
            self.ui_stage = "choose"
        elif active_pending:
            self.ui_stage = "read"
        else:
            self.ui_stage = "result"
        stage = self.ui_stage
        current_pair = (pending[0] if stage == "core" and pending else
                        active_pending[0] if stage == "read" and active_pending else None)
        self.capture_active = stage in ("core", "read") and self.capture_pair is not None
        self.step_heading.setText({
            "market": "第 1 步 · 更新行情",
            "core": f"第 2 步 · 基础报价 {core_done}/6",
            "choose": "第 3 步 · 选择交易路线",
            "read": "第 4 步 · 补读当前报价",
            "result": "第 5 步 · 查看整数复算",
        }[stage])
        self.start_sync.setVisible(stage == "market")
        self.mode_host.setVisible(stage in ("choose", "read", "result"))
        self.task_scroll.setVisible(stage in ("core", "read"))
        self.task_scroll.setFixedHeight(63)
        self.capture_row.setVisible(self.capture_active)
        self.route_heading.setVisible(stage in ("choose", "read", "result"))
        self.game_result.setVisible(stage == "result")
        self.game_host.setFixedHeight({"market": 90,
                                       "core": 185 if self.capture_active else 100,
                                       "choose": 290,
                                       "read": 420 if self.capture_active else 360,
                                       "result": 350}[stage])
        if hasattr(self, "details_scroll") and not self.compact and not self.management:
            self._set_compact(False)
        self.progress.setText({
            "market": "加载后开始读取游戏内报价",
            "core": (f"游戏右侧支付 {short_name(current_pair[0])}，左侧获得 {short_name(current_pair[1])}"
                     if current_pair else "选好游戏中的通货交换方向"),
            "choose": ("点击下方候选项；选中后会出现需要补读的报价" if has_candidates
                       else "暂无候选项；在设置中调整起始通货、起始量或更新快照"),
            "read": (f"游戏右侧支付 {short_name(current_pair[0])}，左侧获得 {short_name(current_pair[1])}"
                     if current_pair else "读取当前报价"),
            "result": "本轮报价已齐；交易前重新确认库存和金币费",
        }[stage])
        for pair in ([current_pair] if stage in ("core", "read") and current_pair else []):
            line = QWidget()
            row = QHBoxLayout(line)
            row.setContentsMargins(2, 1, 2, 1)
            row.addWidget(item_badge(pair[0]))
            arrow = QLabel("→")
            arrow.setStyleSheet("color:#93d7b1")
            row.addWidget(arrow)
            row.addWidget(item_badge(pair[1]))
            row.addStretch()
            row.addWidget(self._button("读取", lambda _checked=False, p=pair: self.capture_task(p)))
            self.task_layout.addWidget(line)
        self.task_layout.addStretch()
        self.route_heading.setText(f"单品跨币种 · {self.cross_source} · 点击核价" if show_cross else
                                   "双向报价 · 点击优先复核这两个方向" if show_rounds else
                                   "推荐路线 · 点击加入待核价清单" if core_done == 6 else
                                   "基础核价完成后显示推荐路线")
        self.game_routes.setVisible(stage in ("choose", "read", "result") and not show_rounds and not show_cross)
        self.round_table.setVisible(stage in ("choose", "read", "result") and (show_rounds or show_cross))
        self.game_routes.blockSignals(True)
        self.game_routes.clear()
        for index, candidate in enumerate(self.rows[:8]):
            done, count = self.quote_book.coverage(candidate)
            label = " → ".join(short_name(x) for x in candidate.path)
            selected = candidate.path in self.quote_book.selected_routes
            item = QTreeWidgetItem([
                ("✓ " if selected else "＋ ") + label,
                f"{done}/{count}",
                f"+{(self.reference_finals[candidate.path] - self.reference_initial) / self.reference_initial:.1%}",
            ])
            item.setData(0, Qt.ItemDataRole.UserRole, index)
            item.setToolTip(0, "点击加入清单。百分比已按起始量逐跳取整，尚未计入金币费和游戏内库存；最终按实时订单复算。")
            self.game_routes.addTopLevelItem(item)
        self.game_routes.blockSignals(False)
        self.round_table.blockSignals(True)
        self.round_table.clear()
        if show_cross:
            self.round_table.setHeaderLabels(["买入 → 卖出 → 换回", "状态", "操作净赚", "每百万金"])
            entries = [(index, candidate, *self._cross_status(candidate, now))
                       for index, candidate in enumerate(self.cross_rows[:30])]
            entries.sort(key=lambda entry: (
                CORE.index(entry[1].path[0]),
                entry[4] is None,
                -(entry[4].profit_per_million_gold or 0) if entry[4] else 0,
            ))
            for index, candidate, status, roi, opportunity in entries:
                row = QTreeWidgetItem([
                    " → ".join(short_name(x) for x in candidate.path), status,
                    f"{opportunity.profit:+,}" if opportunity else "—",
                    compact_number(opportunity.profit_per_million_gold) if opportunity else "—",
                ])
                row.setData(0, Qt.ItemDataRole.UserRole, index)
                row.setToolTip(0, " → ".join(item_tooltip(x).splitlines()[0] for x in candidate.path))
                row.setToolTip(1, status)
                if opportunity and opportunity.profit_per_million_gold is not None:
                    row.setToolTip(3, f"{opportunity.profit_per_million_gold:+,.2f} {short_name(candidate.path[0])} / 100 万金币；本金收益率 {roi}")
                if candidate == self.focus_cross:
                    row.setForeground(0, QBrush(QColor("#89deb1")))
                self.round_table.addTopLevelItem(row)
        else:
            self.round_table.setHeaderLabels(["单轮交易", "状态", "操作净赚", "每百万金"])
            pairs = [(source, target) for source in CORE for target in CORE if source != target]
            entries = [(pair, *self._round_status(pair, now)) for pair in pairs]
            entries.sort(key=lambda entry: (
                CORE.index(entry[0][0]),
                entry[3] is None,
                -(entry[3].profit_per_million_gold or 0) if entry[3] else 0,
            ))
            for pair, status, roi, opportunity in entries:
                source, target = pair
                row = QTreeWidgetItem([
                    f"{short_name(source)} → {short_name(target)} → {short_name(source)}",
                    status,
                    f"{opportunity.profit:+,}" if opportunity else "—",
                    compact_number(opportunity.profit_per_million_gold) if opportunity else "—",
                ])
                row.setData(0, Qt.ItemDataRole.UserRole, pair)
                row.setToolTip(1, status)
                if opportunity and opportunity.profit_per_million_gold is not None:
                    row.setToolTip(3, f"{opportunity.profit_per_million_gold:+,.2f} {short_name(source)} / 100 万金币；本金收益率 {roi}")
                if pair == self.focus_pair:
                    row.setForeground(0, QBrush(QColor("#89deb1")))
                self.round_table.addTopLevelItem(row)
        self.round_table.blockSignals(False)
        if show_cross:
            if self.focus_cross:
                status, roi, opportunity = self._cross_status(self.focus_cross, now)
                if opportunity:
                    label = "需按份数重读金币费" if not opportunity.single_order_each else "按当前三笔订单"
                    efficiency = (f"每 100 万金币约净 {opportunity.profit_per_million_gold:+,.2f} "
                                  f"{short_name(self.focus_cross.path[0])}" if opportunity.profit_per_million_gold is not None
                                  else "金币费用为 0，无法计算金币效率")
                    self.game_result.setText(
                        f"{status} · {short_name(self.focus_cross.path[1])} · "
                        f"{opportunity.start:,} → {opportunity.finish:,} "
                        f"{short_name(self.focus_cross.path[0])}，本次净 {opportunity.profit:+,}（本金 {roi}）。"
                        f"{efficiency}；消耗金币约 {opportunity.estimated_gold:,}，"
                        f"份数 {' / '.join(map(str, opportunity.all_lots))}，{label}。"
                    )
                else:
                    self.game_result.setText(f"{status}。清单会复用基础汇率，并优先核对该通货的买卖方向。")
            else:
                self.game_result.setText("点击一种通货，核对买入、卖出和换回起始通货的三个方向。")
        elif show_rounds:
            if self.focus_pair:
                status, roi, opportunity = self._round_status(self.focus_pair, now)
                if opportunity:
                    label = "需按数量重读金币费" if not opportunity.single_order_each else "按当前两笔订单"
                    efficiency = (f"每 100 万金币约净 {opportunity.profit_per_million_gold:+,.2f} "
                                  f"{short_name(self.focus_pair[0])}" if opportunity.profit_per_million_gold is not None
                                  else "金币费用为 0，无法计算金币效率")
                    self.game_result.setText(
                        f"{status} · {opportunity.start:,} → {opportunity.finish:,} "
                        f"{short_name(self.focus_pair[0])}，本次净 {opportunity.profit:+,}（本金 {roi}）。"
                        f"{efficiency}；消耗金币约 {opportunity.estimated_gold:,}，{label}。"
                        f"最早报价 {opportunity.age_seconds} 秒前。"
                    )
                else:
                    self.game_result.setText(f"{status}。点击清单优先重读双向报价。")
            else:
                self.game_result.setText("点击一个单轮交易，优先复核买入和卖出两个方向。")
        elif self.quote_book.selected_routes:
            findings = []
            for path in self.quote_book.selected_routes:
                candidate = next((row for row in self.rows if row.path == path), None)
                if candidate is None:
                    continue
                quotes = self.quote_book.route_quotes(candidate)
                if not quotes:
                    done, count = self.quote_book.coverage(candidate)
                    findings.append(f"{short_name(path[0])}路线 {done}/{count} 跳")
                    continue
                try:
                    initial = int(self.initial.text()) if hasattr(self, "initial") else quotes[0].pay
                    result = simulate_exact(path, initial, quotes)
                    findings.append(
                        f"{short_name(path[0])}本次净 {result.profit:+,}（本金 {result.profit/initial:+.1%}） · "
                        + (f"每 100 万金币净 {result.profit_per_million_gold:+,.2f} {short_name(path[0])} · "
                           if result.profit_per_million_gold is not None else "")
                        + f"金币 {result.gold:,}"
                    )
                    self.verified[(self.league.currentText(), candidate.key)] = {
                        "profit": result.profit,
                        "gold_efficiency": result.profit_per_million_gold,
                    }
                except (ValueError, TypeError) as exc:
                    findings.append(f"{short_name(path[0])}路线待调整交易量：{exc}")
            self.game_result.setText("  |  ".join(findings) or "选路线后显示进度与收益")
        else:
            self.game_result.setText("点击候选路线；已核对的交易对会自动复用。" if self.core_complete
                                     else "先核对基础交易对，再点击推荐路线加入清单。")

    def simulate(self):
        if not self.current:
            self._error("先选择一条闭环路线")
            return
        try:
            quotes = []
            now = int(time.time())
            for i, fields in enumerate(self.fields):
                if not fields.observed_at or now - fields.observed_at > 180:
                    raise ValueError(f"第 {i+1} 跳报价未确认或已超过 3 分钟")
                quote = LiveQuote(
                    self.current.path[i], self.current.path[i+1],
                    parse_integer_field(fields.pay.text(), f"第 {i+1} 跳支付数量"),
                    parse_integer_field(fields.receive.text(), f"第 {i+1} 跳获得数量"),
                    parse_integer_field(fields.stock.text(), f"第 {i+1} 跳可获数量"),
                    parse_integer_field(fields.gold.text(), f"第 {i+1} 跳金币费用", minimum=0),
                    fields.observed_at,
                )
                quotes.append(quote)
            initial = parse_integer_field(self.initial.text(), "起始数量")
            result = simulate_exact(self.current.path, initial, quotes)
            leftovers = ", ".join(f"{value} {short_name(currency)}" for currency, value in result.holdings.items() if value > 0 and currency != self.current.path[0])
            sign = "+" if result.profit >= 0 else ""
            base_name = short_name(self.current.path[0])
            verdict = "账面为正，仍须确认可成交量" if result.profit > 0 else "当前订单无正收益"
            detail = (
                f"{verdict}：{initial:,} → {result.final:,} {base_name}，"
                f"本次净 {sign}{result.profit:,}（本金 {result.profit / initial:+.1%}）；"
                f"消耗金币 {result.gold:,}。"
            )
            if result.profit_per_million_gold is not None:
                detail += f" 每 100 万金币净 {result.profit_per_million_gold:+,.2f} {base_name}。"
            self.result.setText(detail)
            self.result.setStyleSheet(f"color:{'#89deb1' if result.profit > 0 else '#ef9d8f'}; font-weight:bold;")
            if leftovers:
                self.result.setText(self.result.text() + f" 其他通货剩余：{leftovers}。")
            self.last_model_state = {
                "route": list(self.current.path),
                "orders": [quote.__dict__ for quote in quotes],
                "closed_profit": result.profit,
                "gold_cost": result.gold,
                "historical_reference_gain_pct": float(self.current.reference_gain) * 100,
            }
            self.verified[(self.league.currentText(), self.current.key)] = {"profit": result.profit, "gold_efficiency": result.profit_per_million_gold}
            for quote in quotes:
                self.quote_book.put(quote)
            self.scan()
            self.summary.setText(f"已核验：净 {sign}{result.profit:,} {base_name} · 金币 {result.gold:,}")
        except (ValueError, TypeError) as exc:
            self._error(str(exc))

    def evaluate_jev(self):
        if self.last_model_state is None:
            self._error("请先完成整数复算")
            return
        if not os.environ.get("TYPESAFE_API_KEY"):
            self._error("未设置 TYPESAFE_API_KEY；Jev 辅助评估可选")
            return
        self.result.setText("Jev 正在评估已核验路线…")
        self._job(self._call_jev, self._jev_done, self.last_model_state)

    @staticmethod
    def _call_jev(state):
        import ssl
        import urllib.request
        import certifi
        payload = {
            "model": "jev-latest", "state": json.dumps(state, ensure_ascii=False),
            "questions": {"review": {
                "type": "choice",
                "instructions": "Should a human recheck these manually entered PoE2 exchange orders now? Do not redo arithmetic or imply guaranteed profit.",
                "criteria": {
                    "check_now": "Positive closed profit and complete recent quotes; worth checking again",
                    "wait": "Marginal or uncertain opportunity",
                    "skip": "Negative or implausible opportunity",
                },
            }},
        }
        request = urllib.request.Request(
            "https://api.typesafe.ai/v1/systemone",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {os.environ['TYPESAFE_API_KEY']}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=15, context=ssl.create_default_context(cafile=certifi.where())) as response:
            return json.load(response)["answers"]["review"]

    def _jev_done(self, answer):
        self.result.setText(f"Jev：{answer.get('choice', '无结果')}；置信度 {answer.get('confidence', '—')}。模型只辅助复核，收益以整数计算与实际成交为准。")

    def closeEvent(self, event):
        if sys.platform == "win32":
            import ctypes
            hwnd = int(self.winId())
            ctypes.windll.user32.UnregisterHotKey(hwnd, 1)
            ctypes.windll.user32.UnregisterHotKey(hwnd, 2)
        super().closeEvent(event)


def run():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    overlay = Overlay()
    return app.exec()

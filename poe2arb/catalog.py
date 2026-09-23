"""Offline exchange item catalog: localized names and bundled GGG CDN icons."""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap

ROOT = Path(__file__).resolve().parent
COMMON_ZH = {
    "CurrencyAddModToRare": "崇高石", "CurrencyModValues": "神圣石",
    "CurrencyRerollRare": "混沌石", "CurrencyCorrupt": "瓦尔宝珠",
    "CurrencyRemoveMod": "剥离石", "CurrencyUpgradeToRare": "富豪石",
    "CurrencyUpgradeToMagic": "蜕变石", "CurrencyRerollMagic": "改造石",
    "CurrencyAddModToMagic": "增幅石",
}


@lru_cache(maxsize=1)
def catalog() -> dict:
    try:
        return json.loads((ROOT / "catalog.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def record(item_id: str) -> dict:
    return catalog().get(item_id, {})


def item_name(item_id: str) -> str:
    suffix = item_id.rsplit("/", 1)[-1]
    if suffix in COMMON_ZH:
        return COMMON_ZH[suffix]
    entry = record(item_id)
    if entry.get("zh_tw"):
        return entry["zh_tw"]
    if entry.get("en"):
        return entry["en"]
    name = re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Za-z])(?=\d)", " ", suffix)
    return re.sub(r"^Currency ", "", name)


def item_tooltip(item_id: str) -> str:
    entry = record(item_id)
    localized, english = item_name(item_id), entry.get("en")
    heading = localized if not english or english == localized else f"{localized} · {english}"
    description = entry.get("description")
    return "\n".join(x for x in (heading, description, item_id) if x)


def icon_file(item_id: str) -> Path | None:
    relative = record(item_id).get("icon")
    if not relative:
        return None
    path = ROOT / "icons" / relative
    return path if path.is_file() else None


def item_pixmap(item_id: str, size: int = 24) -> QPixmap:
    path = icon_file(item_id)
    if path:
        pix = QPixmap(str(path))
        if not pix.isNull():
            return pix.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    hue = sum(ord(c) for c in item_id) % 360
    painter.setPen(QColor.fromHsv(hue, 75, 170))
    painter.setBrush(QColor.fromHsv(hue, 70, 57))
    painter.drawRoundedRect(1, 1, size - 2, size - 2, 5, 5)
    painter.setPen(QColor("#e7eee9"))
    painter.setFont(QFont("Segoe UI", max(8, size // 2), QFont.Weight.Bold))
    painter.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, item_name(item_id)[:1].upper())
    painter.end()
    return pix


def item_icon(item_id: str, size: int = 24) -> QIcon:
    return QIcon(item_pixmap(item_id, size))

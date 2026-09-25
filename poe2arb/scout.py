"""Poe2Scout exchange snapshots: indicative cross-book leads, never firm orders."""

from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.request
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from urllib.parse import quote

from .core import HistoricalEdge
from .data import app_data_dir

BASE_URL = "https://api.poe2scout.com/poe2/Leagues"
MAX_AGE_SECONDS = 2 * 3600
MIN_BOOK_VOLUME = 10_000
MIN_TARGET_STOCK = 1_000


def _get_json(url: str):
    import certifi

    request = urllib.request.Request(
        url,
        headers={"User-Agent": "PoE2ArbDesk/0.3 (desktop market research; manual trades)",
                 "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(
            request, timeout=20, context=ssl.create_default_context(cafile=certifi.where())
        ) as response:
            raw = response.read(8_000_001)
            if len(raw) > 8_000_000:
                raise ValueError("Poe2Scout 响应过大")
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise RuntimeError("Poe2Scout 接口限流，请稍后重试") from exc
        raise RuntimeError(f"Poe2Scout 返回 HTTP {exc.code}") from exc


def _positive(value) -> bool:
    try:
        return Decimal(str(value)) > 0
    except (InvalidOperation, TypeError, ValueError):
        return False


def normalize_pairs(raw_pairs: list) -> list[dict]:
    """Keep only genuine independently observed books with exact item IDs."""
    if not isinstance(raw_pairs, list) or len(raw_pairs) > 10_000:
        raise ValueError("Poe2Scout 交易对格式无效")
    by_pair = {}
    for book in raw_pairs:
        try:
            a_info, b_info = book["CurrencyOne"], book["CurrencyTwo"]
            a, b = a_info["BaseItemTypeId"], b_info["BaseItemTypeId"]
            a_data, b_data = book["CurrencyOneData"], book["CurrencyTwoData"]
            pa, pb = str(a_data["RelativePrice"]), str(b_data["RelativePrice"])
            if not (a and b and a != b and _positive(pa) and _positive(pb)):
                continue
            pair = tuple(sorted((a, b)))
            volume = int(Decimal(str(book.get("Volume") or 0)))
            row = {
                "a": a, "b": b, "pa": pa, "pb": pb,
                "volume": volume,
                "va": int(a_data.get("VolumeTraded") or 0),
                "vb": int(b_data.get("VolumeTraded") or 0),
                "sa": str(a_data.get("StockValue") or "0"),
                "sb": str(b_data.get("StockValue") or "0"),
            }
            previous = by_pair.get(pair)
            if previous is None or volume > previous["volume"]:
                by_pair[pair] = row
        except (KeyError, TypeError, ValueError, InvalidOperation):
            continue
    return list(by_pair.values())


def _pick_league(leagues: list) -> str:
    for row in leagues:
        if row.get("IsCurrent") and row.get("Value") and not str(row["Value"]).startswith("HC "):
            return str(row["Value"])
    raise ValueError("Poe2Scout 没有返回当前普通联赛")


def fetch_scout_snapshot(league: str | None = None) -> dict:
    if not league:
        league = _pick_league(_get_json(BASE_URL))
    url = BASE_URL + "/" + quote(league, safe="")
    for _ in range(2):
        before = _get_json(url + "/ExchangeSnapshot")
        books = _get_json(url + "/SnapshotPairs")
        after = _get_json(url + "/ExchangeSnapshot")
        if not isinstance(before, dict) or not isinstance(after, dict):
            raise ValueError("Poe2Scout 快照格式无效")
        epoch = int(after.get("Epoch") or 0)
        if epoch <= 0:
            raise ValueError("Poe2Scout 缺少快照时间")
        if before.get("Epoch") == after.get("Epoch"):
            pairs = normalize_pairs(books)
            if not pairs:
                raise ValueError("Poe2Scout 没有有效的交易对")
            snapshot = {"league": league, "epoch": epoch, "fetched_at": int(time.time()),
                        "raw_pairs": len(books), "pairs": pairs}
            save_scout_snapshot(snapshot)
            return snapshot
    raise RuntimeError("Poe2Scout 快照更新中，请稍后重试")


def _snapshot_path() -> Path:
    return app_data_dir() / "scout_snapshot.json"


def save_scout_snapshot(snapshot: dict) -> None:
    path = _snapshot_path()
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def load_scout_snapshot() -> dict | None:
    try:
        snapshot = json.loads(_snapshot_path().read_text(encoding="utf-8"))
        if isinstance(snapshot.get("pairs"), list) and isinstance(snapshot.get("epoch"), int):
            return snapshot
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return None


def snapshot_age(snapshot: dict, now: int | None = None) -> int:
    return (now or int(time.time())) - int(snapshot["epoch"])


def scout_edges(snapshot: dict) -> dict[tuple[str, str], HistoricalEdge]:
    """A book supplies an indicative ratio in both directions, not a bid/ask."""
    edges = {}
    for book in snapshot.get("pairs", []):
        try:
            a, b = book["a"], book["b"]
            pa, pb = Fraction(Decimal(book["pa"])), Fraction(Decimal(book["pb"]))
            if pa <= 0 or pb <= 0:
                continue
            if int(book.get("volume") or 0) < MIN_BOOK_VOLUME:
                continue
            if int(book["va"]) > 0 and Decimal(book["sb"]) >= MIN_TARGET_STOCK:
                edges[a, b] = HistoricalEdge(a, b, pa / pb, int(book["va"]), 1)
            if int(book["vb"]) > 0 and Decimal(book["sa"]) >= MIN_TARGET_STOCK:
                edges[b, a] = HistoricalEdge(b, a, pb / pa, int(book["vb"]), 1)
        except (KeyError, TypeError, ValueError, InvalidOperation, ZeroDivisionError):
            continue
    return edges

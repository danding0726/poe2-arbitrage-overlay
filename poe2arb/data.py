from __future__ import annotations

import json
import os
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "https://web.poecdn.com/api/currency-exchange/poe2"


def app_data_dir() -> Path:
    override = os.environ.get("POE2ARB_DATA_DIR")
    if override:
        path = Path(override)
        path.mkdir(parents=True, exist_ok=True)
        return path
    root = os.environ.get("LOCALAPPDATA")
    path = Path(root) / "PoE2ArbDesk" if root else Path.home() / ".poe2arbdesk"
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_snapshot() -> dict:
    path = app_data_dir() / "snapshot.json"
    if not path.exists():
        return {"fetched_at": None, "markets": [], "last_id": None}
    with path.open(encoding="utf-8") as file:
        return json.load(file)


def save_snapshot(snapshot: dict) -> None:
    path = app_data_dir() / "snapshot.json"
    temp = path.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as file:
        json.dump(snapshot, file, ensure_ascii=False)
    temp.replace(path)


def fetch_page(change_id: int) -> dict:
    req = urllib.request.Request(
        f"{API}/{change_id}",
        headers={"User-Agent": "PoE2ArbDesk/0.1 (local research tool)", "Accept": "application/json"},
    )
    try:
        try:
            import certifi
            context = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            context = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=20, context=context) as res:
            return json.load(res)
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise RuntimeError("官方接口限流，请稍后重试") from exc
        raise RuntimeError(f"官方接口返回 HTTP {exc.code}") from exc


def sync_recent(start_id: int | None = None, max_pages: int = 12) -> dict:
    """Fetch a bounded recent window; never crawl the full historical stream."""
    previous = load_snapshot()
    now_hour = int(time.time() // 3600 * 3600)
    change_id = int(start_id or previous.get("last_id") or now_hour - 6 * 3600)
    pages = []
    seen = set()
    for _ in range(max_pages):
        if change_id in seen:
            break
        seen.add(change_id)
        page = fetch_page(change_id)
        markets = page.get("markets")
        next_id = page.get("next_change_id")
        if not isinstance(markets, list) or not isinstance(next_id, int):
            raise RuntimeError("官方接口格式发生变化，请检查更新")
        pages.extend([{**market, "_hour_id": change_id} for market in markets])
        if next_id == change_id:
            break
        change_id = next_id
    # Merge by hour and pair so a second refresh cannot double-count trades.
    all_rows = {}
    for market in previous.get("markets", []) + pages:
        hour = market.get("_hour_id")
        if not isinstance(hour, int) or hour < now_hour - 24 * 3600:
            continue
        key = (hour, market.get("league"), market.get("market_id") or "|".join(market.get("market_pair", [])))
        all_rows[key] = market
    snapshot = {"fetched_at": int(time.time()), "markets": list(all_rows.values()), "last_id": change_id}
    save_snapshot(snapshot)
    return snapshot


def leagues(snapshot: dict) -> list[str]:
    return sorted({m.get("league", "") for m in snapshot.get("markets", []) if m.get("league")})

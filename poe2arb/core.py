"""Published hourly exchange prices used only to rank items for inspection."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Iterable


# The published volume-weighted average is not a live order. Reserve an
# indicative 2% spread on each direction before ranking possible paths.
REFERENCE_SPREAD = Fraction(50, 51)
MAX_HOURLY_PRICE_RANGE = Fraction(3, 2)
MIN_TRADED_UNITS_PER_SIDE = 100


@dataclass(frozen=True)
class HistoricalEdge:
    source: str
    target: str
    rate: Fraction
    source_volume: int
    hours: int


def latest_market_hour(markets: Iterable[dict], league: str) -> int | None:
    return max(
        (market["_hour_id"] for market in markets
         if market.get("league") == league and isinstance(market.get("_hour_id"), int)),
        default=None,
    )


def historical_edges(markets: Iterable[dict], league: str) -> dict[tuple[str, str], HistoricalEdge]:
    """Read the newest published hour with price-range quality checks."""
    markets = list(markets)
    hour = latest_market_hour(markets, league)
    edges = {}
    for market in markets:
        if market.get("league") != league or (hour is not None and market.get("_hour_id") != hour):
            continue
        pair = market.get("market_pair") or str(market.get("market_id", "")).split("|")
        if len(pair) != 2 or pair[0] == pair[1]:
            continue
        a, b = map(str, pair)
        volumes = market.get("volume_traded") or {}
        try:
            va, vb = int(volumes[a]), int(volumes[b])
            low = market["lowest_ratio"]
            high = market["highest_ratio"]
            prices = (Fraction(int(low[b]), int(low[a])),
                      Fraction(int(high[b]), int(high[a])))
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            continue
        if min(va, vb) < MIN_TRADED_UNITS_PER_SIDE or min(prices) <= 0:
            continue
        if hour is not None and (min(prices) == max(prices)
                                 or max(prices) / min(prices) > MAX_HOURLY_PRICE_RANGE):
            continue
        edges[a, b] = HistoricalEdge(a, b, Fraction(vb, va) * REFERENCE_SPREAD, va, 1)
        edges[b, a] = HistoricalEdge(b, a, Fraction(va, vb) * REFERENCE_SPREAD, vb, 1)
    return edges

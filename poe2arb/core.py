from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Iterable


# Hourly traded amounts yield a volume-weighted average price. Reserve an
# assumed fill spread because that average is not an executable order.
REFERENCE_SPREAD = Fraction(50, 51)  # divide the observed price by 1.02
MAX_REFERENCE_GAIN = Fraction(1, 2)
MAX_HOURLY_PRICE_RANGE = Fraction(3, 2)


@dataclass(frozen=True)
class HistoricalEdge:
    source: str
    target: str
    rate: Fraction  # target units per source unit; historical reference only
    source_volume: int
    hours: int


@dataclass(frozen=True)
class Candidate:
    path: tuple[str, ...]  # start is repeated at the end
    reference_gain: Fraction
    limiting_start_units: Fraction

    @property
    def key(self) -> str:
        return " → ".join(self.path)


def latest_market_hour(markets: Iterable[dict], league: str) -> int | None:
    """Return the newest published hourly bucket for a league."""
    return max((market["_hour_id"] for market in markets
                if market.get("league") == league and isinstance(market.get("_hour_id"), int)),
               default=None)


def historical_edges(markets: Iterable[dict], league: str) -> dict[tuple[str, str], HistoricalEdge]:
    """Use one published hour's VWAP, with price-range quality checks."""
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
        if va <= 0 or vb <= 0 or min(prices) <= 0:
            continue
        # Identical endpoints provide no evidence that both sides traded.
        # Very scattered fills are a weak guide to a current order price.
        if hour is not None and (min(prices) == max(prices)
                                 or max(prices) / min(prices) > MAX_HOURLY_PRICE_RANGE):
            continue
        edges[a, b] = HistoricalEdge(a, b, Fraction(vb, va) * REFERENCE_SPREAD, va, 1)
        edges[b, a] = HistoricalEdge(b, a, Fraction(va, vb) * REFERENCE_SPREAD, vb, 1)
    return edges


def find_candidates(
    edges: dict[tuple[str, str], HistoricalEdge],
    base: str,
    lengths: tuple[int, ...] = (3, 4),
    min_gain: Fraction = Fraction(0),
) -> list[Candidate]:
    """Search reference-price cycles. Results are leads requiring live confirmation."""
    neighbors: dict[str, set[str]] = {}
    for source, target in edges:
        neighbors.setdefault(source, set()).add(target)
    found = []

    def visit(path: tuple[str, ...], length: int) -> None:
        if len(path) == length:
            if (path[-1], base) not in edges:
                return
            cycle = (*path, base)
            legs = [edges[a, b] for a, b in zip(cycle, cycle[1:])]
            product = Fraction(1)
            limiting = None
            for leg in legs:
                capacity_in_base = Fraction(leg.source_volume, 1) / product
                limiting = capacity_in_base if limiting is None else min(limiting, capacity_in_base)
                product *= leg.rate
            gain = product - 1
            if min_gain < gain <= MAX_REFERENCE_GAIN:
                found.append(Candidate(cycle, gain, limiting or Fraction(0)))
            return
        for target in neighbors.get(path[-1], ()):
            if target != base and target not in path:
                visit((*path, target), length)

    for length in lengths:
        if length >= 2:
            visit((base,), length)
    return sorted(found, key=lambda c: (c.reference_gain, c.limiting_start_units, c.path), reverse=True)


def simulate_reference_units(
    path: tuple[str, ...], initial: int,
    edges: dict[tuple[str, str], HistoricalEdge],
) -> int | None:
    """Floor every hourly-VWAP leg; return None when a whole-unit route cannot run.

    Hourly traded volume only limits a reference simulation. It is not live
    stock, and the result does not include gold fees.
    """
    if initial <= 0 or len(path) < 3 or path[0] != path[-1]:
        return None
    held = initial
    for pair in zip(path, path[1:]):
        edge = edges.get(pair)
        if edge is None or held > edge.source_volume:
            return None
        held = held * edge.rate.numerator // edge.rate.denominator
        if held <= 0:
            return None
    return held


@dataclass(frozen=True)
class LiveQuote:
    source: str
    target: str
    pay: int
    receive: int
    available_receive: int
    gold: int
    observed_at: int  # UTC Unix seconds

    def validate(self) -> None:
        if self.source == self.target or not self.source or not self.target:
            raise ValueError("支付和获得通货必须不同")
        if min(self.pay, self.receive, self.available_receive) <= 0 or self.gold < 0:
            raise ValueError("数量、库存必须为正整数；金币费用不能为负")
        if self.receive > self.available_receive:
            raise ValueError("计划获得量超过当前显示库存")


@dataclass(frozen=True)
class Simulation:
    initial: int
    final: int
    gold: int
    holdings: dict[str, int]

    @property
    def profit(self) -> int:
        return self.final - self.initial

    @property
    def profit_per_100k_gold(self) -> float | None:
        return self.profit * 100000 / self.gold if self.gold else None

    @property
    def profit_per_million_gold(self) -> float | None:
        return self.profit * 1_000_000 / self.gold if self.gold else None


def simulate_exact(path: tuple[str, ...], initial: int, quotes: list[LiveQuote]) -> Simulation:
    """Simulate the exact whole-item orders shown in the game, preserving leftovers."""
    if initial <= 0 or len(quotes) != len(path) - 1 or path[0] != path[-1]:
        raise ValueError("闭环或起始数量无效")
    holdings = {path[0]: initial}
    total_gold = 0
    for i, quote in enumerate(quotes):
        quote.validate()
        if (quote.source, quote.target) != (path[i], path[i + 1]):
            raise ValueError(f"第 {i + 1} 跳的通货方向与路线不一致")
        if holdings.get(quote.source, 0) < quote.pay:
            raise ValueError(f"第 {i + 1} 跳没有足够的 {quote.source}")
        holdings[quote.source] -= quote.pay
        holdings[quote.target] = holdings.get(quote.target, 0) + quote.receive
        total_gold += quote.gold
    return Simulation(initial, holdings[path[0]], total_gold, holdings)

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from itertools import permutations
from typing import Iterable


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


def historical_edges(markets: Iterable[dict], league: str) -> dict[tuple[str, str], HistoricalEdge]:
    """Derive indicative mid rates from paired executed volumes, never executable quotes."""
    totals: dict[tuple[str, str], list[int]] = {}
    for market in markets:
        if market.get("league") != league:
            continue
        pair = market.get("market_pair") or str(market.get("market_id", "")).split("|")
        if len(pair) != 2 or pair[0] == pair[1]:
            continue
        a, b = map(str, pair)
        volumes = market.get("volume_traded") or {}
        try:
            va, vb = int(volumes[a]), int(volumes[b])
        except (KeyError, TypeError, ValueError):
            continue
        if va <= 0 or vb <= 0:
            continue
        key = tuple(sorted((a, b)))
        row = totals.setdefault(key, [0, 0, 0])
        if (a, b) == key:
            row[0] += va
            row[1] += vb
        else:
            row[0] += vb
            row[1] += va
        row[2] += 1
    edges = {}
    for (a, b), (va, vb, hours) in totals.items():
        edges[a, b] = HistoricalEdge(a, b, Fraction(vb, va), va, hours)
        edges[b, a] = HistoricalEdge(b, a, Fraction(va, vb), vb, hours)
    return edges


def find_candidates(
    edges: dict[tuple[str, str], HistoricalEdge],
    base: str,
    lengths: tuple[int, ...] = (3, 4),
    min_gain: Fraction = Fraction(0),
    max_currencies: int = 35,
) -> list[Candidate]:
    """Search reference-price cycles. Results are leads requiring live confirmation."""
    currencies = {base}
    for edge in edges.values():
        currencies.add(edge.source)
        currencies.add(edge.target)
    ranked = sorted(
        (x for x in currencies if x != base),
        key=lambda x: sum(e.source_volume for e in edges.values() if e.source == x),
        reverse=True,
    )[: max_currencies - 1]
    found = []
    for length in lengths:
        for middle in permutations(ranked, length - 1):
            path = (base, *middle, base)
            legs = [edges.get((path[i], path[i + 1])) for i in range(length)]
            if any(leg is None for leg in legs):
                continue
            product = Fraction(1)
            limiting = None
            for leg in legs:
                assert leg is not None
                capacity_in_base = Fraction(leg.source_volume, 1) / product
                limiting = capacity_in_base if limiting is None else min(limiting, capacity_in_base)
                product *= leg.rate
            gain = product - 1
            if gain > min_gain:
                found.append(Candidate(path, gain, limiting or Fraction(0)))
    return sorted(found, key=lambda c: (c.reference_gain, c.limiting_start_units), reverse=True)


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

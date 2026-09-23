"""Two-direction exchange checks using independently observed live orders."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from math import gcd

from .core import LiveQuote
from .core import Candidate, HistoricalEdge
from .workflow import CORE


@dataclass(frozen=True)
class RoundTrip:
    start: int
    finish: int
    first_lots: int
    second_lots: int
    all_lots: tuple[int, ...]
    estimated_gold: int
    age_seconds: int
    observation_gap_seconds: int

    @property
    def profit(self) -> int:
        return self.finish - self.start

    @property
    def roi(self) -> Fraction:
        return Fraction(self.profit, self.start)

    @property
    def profit_per_million_gold(self) -> float | None:
        return self.profit * 1_000_000 / self.estimated_gold if self.estimated_gold else None

    @property
    def single_order_each(self) -> bool:
        return all(count == 1 for count in self.all_lots)


def analyze_round_trip(
    first: LiveQuote,
    second: LiveQuote,
    *,
    now: int,
    max_age: int = 45,
    max_observation_gap: int = 25,
) -> RoundTrip:
    """Find the smallest whole-order round trip with no intermediate leftovers.

    Multiplying displayed orders also multiplies their shown gold fee as an
    *estimate*. The game must be checked again at the proposed quantity.
    """
    first.validate()
    second.validate()
    if (first.source, first.target) != (second.target, second.source):
        raise ValueError("两条订单方向不构成双向兑换")
    age = max(0, now - min(first.observed_at, second.observed_at))
    gap = abs(first.observed_at - second.observed_at)
    if age > max_age or gap > max_observation_gap:
        raise ValueError("双向报价已过期或读取间隔过大")
    divisor = gcd(first.receive, second.pay)
    first_lots = second.pay // divisor
    second_lots = first.receive // divisor
    if first_lots * first.receive > first.available_receive:
        raise ValueError("第一方向可获库存不足")
    if second_lots * second.receive > second.available_receive:
        raise ValueError("第二方向可获库存不足")
    return RoundTrip(
        start=first_lots * first.pay,
        finish=second_lots * second.receive,
        first_lots=first_lots,
        second_lots=second_lots,
        all_lots=(first_lots, second_lots),
        estimated_gold=first_lots * first.gold + second_lots * second.gold,
        age_seconds=age,
        observation_gap_seconds=gap,
    )


def find_single_item_candidates(
    edges: dict[tuple[str, str], HistoricalEdge], limit: int = 80
) -> list[Candidate]:
    """Find A→item→B→A leads, with A and B different core currencies."""
    items = {source for source, _ in edges} - set(CORE)
    found = []
    for item in items:
        for start in CORE:
            for sell_into in CORE:
                if start == sell_into:
                    continue
                path = (start, item, sell_into, start)
                legs = [edges.get(pair) for pair in zip(path, path[1:])]
                if any(leg is None for leg in legs):
                    continue
                rate = Fraction(1)
                capacity = None
                for leg in legs:
                    assert leg is not None
                    supported = Fraction(leg.source_volume, 1) / rate
                    capacity = supported if capacity is None else min(capacity, supported)
                    rate *= leg.rate
                found.append(Candidate(path, rate - 1, capacity or Fraction(0)))
    found.sort(key=lambda row: (row.reference_gain, row.limiting_start_units), reverse=True)
    return found[:limit]


def analyze_sized_cycle(
    quotes: list[LiveQuote], *, now: int, max_age: int = 45,
    max_observation_gap: int = 25,
) -> RoundTrip:
    """Size a 2 or 3 order cycle to whole lots with no middle-currency remainder."""
    if len(quotes) not in (2, 3):
        raise ValueError("只支持两步或三步单轮")
    for quote in quotes:
        quote.validate()
    for left, right in zip(quotes, quotes[1:]):
        if left.target != right.source:
            raise ValueError("订单方向不连续")
    if quotes[-1].target != quotes[0].source:
        raise ValueError("订单未回到起始通货")
    times = [quote.observed_at for quote in quotes]
    age = max(0, now - min(times))
    gap = max(times) - min(times)
    if age > max_age or gap > max_observation_gap:
        raise ValueError("报价已过期或读取间隔过大")
    lots = [1]
    for index in range(len(quotes) - 1):
        held = lots[index] * quotes[index].receive
        needed = quotes[index + 1].pay
        factor = needed // gcd(held, needed)
        lots = [count * factor for count in lots]
        lots.append(lots[index] * quotes[index].receive // needed)
    for quote, count in zip(quotes, lots):
        if quote.receive * count > quote.available_receive:
            raise ValueError("可获库存不足")
    return RoundTrip(
        start=lots[0] * quotes[0].pay,
        finish=lots[-1] * quotes[-1].receive,
        first_lots=lots[0], second_lots=lots[-1],
        all_lots=tuple(lots),
        estimated_gold=sum(count * quote.gold for quote, count in zip(quotes, lots)),
        age_seconds=age, observation_gap_seconds=gap,
    )

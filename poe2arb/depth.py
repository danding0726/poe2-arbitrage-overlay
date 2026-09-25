"""Conservative, whole-lot estimates across visible exchange ladder levels."""

from __future__ import annotations

from dataclasses import dataclass, replace
from fractions import Fraction
from functools import lru_cache

from .single_item import Quote

MAX_BUY_LOTS = 10_000


@dataclass(frozen=True)
class Level:
    pay: int
    receive: int
    stock: int
    ratio: str = ""

    @property
    def rate(self) -> Fraction:
        return Fraction(self.receive, self.pay)


@dataclass(frozen=True)
class Book:
    source: str
    target: str
    levels: tuple[Level, ...]
    observed_at: int

    @classmethod
    def from_quote(cls, quote: Quote) -> Book:
        return cls(quote.source, quote.target,
                   (Level(quote.pay, quote.receive, quote.stock),), quote.observed_at)


@dataclass(frozen=True)
class Fill:
    spent: int
    received: int
    lots: int
    used: tuple[tuple[int, int, int], ...]  # level index, lot count, received count

    @property
    def levels_used(self) -> int:
        return len(self.used)


@dataclass(frozen=True)
class DepthPlan:
    buy: Fill
    sell: Fill
    convert: Fill
    scanned_all: bool
    item_to_exit_rate: Fraction
    exit_to_start_rate: Fraction

    @property
    def profit(self) -> int:
        return self.convert.received - self.buy.spent

    @property
    def roi(self) -> Fraction:
        return Fraction(self.profit, self.buy.spent)

    @property
    def remaining_item(self) -> int:
        return self.buy.received - self.sell.spent

    @property
    def remaining_exit(self) -> int:
        return self.sell.received - self.convert.spent

    @property
    def remaining_value(self) -> Fraction:
        """Fractional mark-to-market in the starting currency, not an executed fill."""
        return (self.remaining_item * self.item_to_exit_rate * self.exit_to_start_rate
                + self.remaining_exit * self.exit_to_start_rate)

    @property
    def estimated_profit(self) -> Fraction:
        return self.profit + self.remaining_value

    @property
    def estimated_roi(self) -> Fraction:
        return self.estimated_profit / self.buy.spent

    @property
    def used_extra_levels(self) -> bool:
        return any(any(index > 0 for index, _, _ in fill.used)
                   for fill in (self.buy, self.sell, self.convert))


def captured_book(quote: Quote, ladder: dict | None) -> Book | None:
    """Keep OCR depth only when its first row agrees with the selected order."""
    if not ladder:
        return None
    rows = ladder.get("levels") or []
    if len(rows) < 2:
        return None
    levels = []
    for row in rows:
        try:
            level = Level(int(row["pay"]), int(row["receive"]),
                          int(row["stock"]), str(row.get("ratio", "")))
        except (TypeError, ValueError, KeyError):
            continue
        if min(level.pay, level.receive, level.stock) <= 0:
            continue
        try:
            confidence = float(row.get("confidence", 0))
        except (TypeError, ValueError):
            continue
        if confidence < 0.7:
            continue
        levels.append(level)
    if len(levels) < 2:
        return None
    levels.sort(key=lambda level: level.rate, reverse=True)
    best = levels[0]
    if abs(best.rate - quote.rate) > quote.rate / 50:
        return None
    levels[0] = Level(quote.pay, quote.receive, best.stock, best.ratio)
    return Book(quote.source, quote.target, tuple(levels), quote.observed_at)


def effective_levels(book: Book, stock_mode: str) -> tuple[Level, ...]:
    if stock_mode == "per_level":
        return book.levels
    if stock_mode != "cumulative":
        raise ValueError("未知库存口径")
    levels = []
    previous = 0
    for level in book.levels:
        incremental = max(0, level.stock - previous)
        previous = max(previous, level.stock)
        if incremental >= level.receive:
            levels.append(Level(level.pay, level.receive, incremental, level.ratio))
    return tuple(levels)


def fill_whole_lots(levels: tuple[Level, ...], budget: int) -> Fill:
    spent = received = lots = 0
    used = []
    for index, level in enumerate(levels):
        count = min(level.stock // level.receive, (budget - spent) // level.pay)
        if count <= 0:
            continue
        level_spent = count * level.pay
        level_received = count * level.receive
        spent += level_spent
        received += level_received
        lots += count
        used.append((index, count, level_received))
    return Fill(spent, received, lots, tuple(used))


@lru_cache(maxsize=64)
def estimate_depth(buy_book: Book, sell_book: Book, convert_book: Book,
                   stock_mode: str = "per_level") -> DepthPlan | None:
    if (buy_book.target != sell_book.source or
            sell_book.target != convert_book.source or
            convert_book.target != buy_book.source):
        raise ValueError("多档交易方向无法连接")
    buy_levels = effective_levels(buy_book, stock_mode)
    sell_levels = effective_levels(sell_book, stock_mode)
    convert_levels = effective_levels(convert_book, stock_mode)
    spent = received = lots = 0
    buy_used = []
    best = None
    for index, level in enumerate(buy_levels):
        for _ in range(level.stock // level.receive):
            if lots >= MAX_BUY_LOTS:
                return replace(best, scanned_all=False) if best else None
            spent += level.pay
            received += level.receive
            lots += 1
            if buy_used and buy_used[-1][0] == index:
                _, count, quantity = buy_used[-1]
                buy_used[-1] = (index, count + 1, quantity + level.receive)
            else:
                buy_used.append((index, 1, level.receive))
            sell = fill_whole_lots(sell_levels, received)
            convert = fill_whole_lots(convert_levels, sell.received)
            if convert.received <= 0:
                continue
            buy = Fill(spent, received, lots, tuple(buy_used))
            candidate = DepthPlan(buy, sell, convert, True,
                                  sell_levels[0].rate, convert_levels[0].rate)
            if best is None or (candidate.estimated_profit, candidate.estimated_roi,
                                -candidate.buy.spent) > (
                    best.estimated_profit, best.estimated_roi, -best.buy.spent):
                best = candidate
    return best

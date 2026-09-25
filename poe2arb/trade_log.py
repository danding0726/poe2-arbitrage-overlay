"""Actual trade journal and mark-to-market accounting for one route."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from .single_item import CORE, Quote, core_gold_cost


ROLES = ("买入", "卖出", "换回")


@dataclass(frozen=True)
class Trade:
    league: str
    item: str
    start: str
    exit_currency: str
    role: str
    source: str
    target: str
    pay: int
    receive: int
    gold: int | None
    observed_at: int

    def validate(self) -> None:
        directions = {
            "买入": (self.start, self.item),
            "卖出": (self.item, self.exit_currency),
            "换回": (self.exit_currency, self.start),
        }
        if (not self.league or not self.item or self.start == self.exit_currency
                or self.role not in directions
                or (self.source, self.target) != directions[self.role]):
            raise ValueError("交易方向无效")
        if (type(self.pay) is not int or type(self.receive) is not int
                or self.pay <= 0 or self.receive <= 0):
            raise ValueError("实际支付和获得必须是正整数")
        if self.gold is not None and (type(self.gold) is not int or self.gold < 0):
            raise ValueError("金币必须是非负整数")


@dataclass(frozen=True)
class TradeSummary:
    balances: dict[str, int]
    gold: int | None
    profit: Fraction | None
    settled: bool
    oldest_quote_age: int | None


def trade_gold(trade: Trade, item_fees: dict[str, int] | None = None) -> int | None:
    if trade.target in CORE:
        return core_gold_cost(trade.target, trade.receive)
    if trade.gold is None and item_fees and trade.target in item_fees:
        return trade.receive * item_fees[trade.target]
    return trade.gold


def summarize_trades(trades: list[Trade], start: str,
                     quotes: dict[tuple[str, str], Quote], now: int,
                     item_fees: dict[str, int] | None = None) -> TradeSummary:
    """Value net session flows; never infer a reverse rate or convert gold."""
    balances: dict[str, int] = {}
    for trade in trades:
        trade.validate()
        balances[trade.source] = balances.get(trade.source, 0) - trade.pay
        balances[trade.target] = balances.get(trade.target, 0) + trade.receive
    balances = {currency: amount for currency, amount in balances.items() if amount}
    fees = [trade_gold(trade, item_fees) for trade in trades]
    gold = sum(fees) if all(fee is not None for fee in fees) else None
    open_balances = {currency: amount for currency, amount in balances.items()
                     if currency != start}
    if not open_balances:
        return TradeSummary(balances, gold, Fraction(balances.get(start, 0)), True, None)
    if any(amount < 0 for amount in open_balances.values()):
        return TradeSummary(balances, gold, None, False, None)

    profit = Fraction(balances.get(start, 0))
    quote_ages = []
    for currency, amount in open_balances.items():
        valued = _rate_to_start(currency, start, quotes, now)
        if valued is None:
            return TradeSummary(balances, gold, None, False, None)
        rate, age = valued
        profit += amount * rate
        quote_ages.append(age)
    return TradeSummary(balances, gold, profit, False, max(quote_ages))


def _rate_to_start(currency: str, start: str,
                   quotes: dict[tuple[str, str], Quote],
                   now: int) -> tuple[Fraction, int] | None:
    frontier = [(currency, Fraction(1), 0, frozenset((currency,)))]
    for _ in range(3):
        following = []
        for source, rate, oldest_age, visited in frontier:
            for (edge_source, edge_target), quote in quotes.items():
                if edge_source != source or edge_target in visited:
                    continue
                edge_age = max(0, now - quote.observed_at)
                next_rate = rate * quote.rate
                next_age = max(oldest_age, edge_age)
                if edge_target == start:
                    return next_rate, next_age
                following.append((edge_target, next_rate, next_age,
                                  visited | {edge_target}))
        frontier = following
    return None

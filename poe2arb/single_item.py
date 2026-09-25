"""Exact whole-order arithmetic for a single-item currency flip."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from math import gcd
import re


EXALTED = "Metadata/Items/Currency/CurrencyAddModToRare"
DIVINE = "Metadata/Items/Currency/CurrencyModValues"
CHAOS = "Metadata/Items/Currency/CurrencyRerollRare"
CORE = (EXALTED, DIVINE, CHAOS)
DEFAULT_CORE_GOLD = {EXALTED: 120, CHAOS: 160, DIVINE: 800}
MAX_QUOTE_AGE = 180
MAX_OBSERVATION_GAP = 180


def core_gold_cost(target: str, receive: int) -> int:
    """Fixed gold cost for receiving a core currency."""
    return receive * DEFAULT_CORE_GOLD[target]


def item_unit_gold(order_gold: int | None, receive: int) -> int | None:
    """Convert a captured whole-order fee to a per-item fee when exact."""
    if order_gold is None or receive <= 0 or order_gold < 0:
        return None
    return order_gold // receive if order_gold % receive == 0 else None


def normalize_quote_amounts(pay_text: str, receive_text: str) -> tuple[int, int, bool]:
    """Convert a decimal ratio to its smallest whole order; keep integer orders exact."""
    pay_text = pay_text.strip().replace(",", "")
    receive_text = receive_text.strip().replace(",", "")
    if not all(re.fullmatch(r"\d+(?:\.\d+)?", value) for value in (pay_text, receive_text)):
        raise ValueError("支付和获得请填写正数，可使用小数点")
    pay, receive = Fraction(pay_text), Fraction(receive_text)
    if pay <= 0 or receive <= 0:
        raise ValueError("支付和获得必须大于 0")
    normalized = "." in pay_text or "." in receive_text
    if normalized:
        ratio = pay / receive
        return ratio.numerator, ratio.denominator, True
    return int(pay), int(receive), False


@dataclass(frozen=True)
class Quote:
    source: str
    target: str
    pay: int
    receive: int
    stock: int | None
    observed_at: int
    gold: int | None = None  # Per received item; ignored for core-currency targets.

    def validate(self) -> None:
        if not self.source or not self.target or self.source == self.target:
            raise ValueError("交易方向无效")
        if min(self.pay, self.receive) <= 0 or (self.stock is not None and self.stock <= 0):
            raise ValueError("支付、获得和已填库存必须是有效正整数")
        if self.gold is not None and self.gold < 0:
            raise ValueError("金币不能为负")

    @property
    def rate(self) -> Fraction:
        return Fraction(self.receive, self.pay)


@dataclass(frozen=True)
class Opportunity:
    item: str
    start: str
    exit_currency: str
    buy_lots: int
    sell_lots: int
    convert_lots: int
    start_amount: int
    item_amount: int
    exit_amount: int
    final_amount: int
    max_rounds: int
    oldest_age: int
    observation_gap: int
    exact_gold: int | None

    @property
    def is_fresh(self) -> bool:
        return self.oldest_age <= MAX_QUOTE_AGE and self.observation_gap <= MAX_OBSERVATION_GAP

    @property
    def profit(self) -> int:
        return self.final_amount - self.start_amount

    @property
    def roi(self) -> Fraction:
        return Fraction(self.profit, self.start_amount)

    @property
    def profit_per_million_gold(self) -> Fraction | None:
        if self.exact_gold is None or self.exact_gold <= 0:
            return None
        return Fraction(self.profit * 1_000_000, self.exact_gold)


def evaluate(buy: Quote, sell: Quote, convert: Quote, *, now: int,
             require_fresh: bool = True, require_stock: bool = True) -> Opportunity:
    """Find the smallest residue-free A→item→B→A exchange."""
    for quote in (buy, sell, convert):
        quote.validate()
    if buy.source not in CORE or sell.target not in CORE or buy.source == sell.target:
        raise ValueError("请选择不同的买入和卖出核心通货")
    if buy.target != sell.source or sell.target != convert.source or convert.target != buy.source:
        raise ValueError("三个交易方向无法连接")
    times = (buy.observed_at, sell.observed_at, convert.observed_at)
    age = max(0, now - min(times))
    gap = max(times) - min(times)
    if require_fresh and age > MAX_QUOTE_AGE:
        raise ValueError("有报价超过 3 分钟，请重新读取")
    if require_fresh and gap > MAX_OBSERVATION_GAP:
        raise ValueError("三条报价读取间隔超过 3 分钟，请重新读取")

    buy_lots = sell.pay // gcd(buy.receive, sell.pay)
    sell_lots = buy.receive * buy_lots // sell.pay
    factor = convert.pay // gcd(sell.receive * sell_lots, convert.pay)
    buy_lots *= factor
    sell_lots *= factor
    convert_lots = sell.receive * sell_lots // convert.pay

    max_rounds = min(
        quote.stock // needed if quote.stock is not None else 0
        for quote, needed in (
            (buy, buy.receive * buy_lots),
            (sell, sell.receive * sell_lots),
            (convert, convert.receive * convert_lots),
        )
    )
    if require_stock and max_rounds < 1:
        raise ValueError("当前库存未录齐或无法完成一轮整数交易")
    gold = None
    if buy.gold is not None:
        gold = (buy.receive * buy_lots * buy.gold
                + core_gold_cost(sell.target, sell.receive * sell_lots)
                + core_gold_cost(convert.target, convert.receive * convert_lots))
    return Opportunity(
        item=buy.target, start=buy.source, exit_currency=sell.target,
        buy_lots=buy_lots, sell_lots=sell_lots, convert_lots=convert_lots,
        start_amount=buy.pay * buy_lots,
        item_amount=buy.receive * buy_lots,
        exit_amount=sell.receive * sell_lots,
        final_amount=convert.receive * convert_lots,
        max_rounds=max_rounds, oldest_age=age, observation_gap=gap, exact_gold=gold,
    )


def indicative_paths(edges: dict, item: str) -> list[tuple[str, str, Fraction]]:
    """Rank cross-currency directions from historical ratios, as leads only."""
    paths = []
    for start in CORE:
        for exit_currency in CORE:
            if start == exit_currency:
                continue
            legs = (edges.get((start, item)), edges.get((item, exit_currency)),
                    edges.get((exit_currency, start)))
            if all(legs):
                gain = legs[0].rate * legs[1].rate * legs[2].rate - 1
                paths.append((start, exit_currency, gain))
    return sorted(paths, key=lambda row: row[2], reverse=True)

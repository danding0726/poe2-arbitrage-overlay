"""Shared live quote checklist for the in-game overlay."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .core import Candidate, LiveQuote

EXALTED = "Metadata/Items/Currency/CurrencyAddModToRare"
CHAOS = "Metadata/Items/Currency/CurrencyRerollRare"
DIVINE = "Metadata/Items/Currency/CurrencyModValues"
CORE = (EXALTED, CHAOS, DIVINE)
Pair = tuple[str, str]


@dataclass
class QuoteBook:
    league: str = ""
    quotes: dict[Pair, LiveQuote] = field(default_factory=dict)
    selected_routes: list[tuple[str, ...]] = field(default_factory=list)

    def reset(self, league: str) -> None:
        if league != self.league:
            self.league = league
            self.quotes.clear()
            self.selected_routes.clear()

    def put(self, quote: LiveQuote) -> None:
        quote.validate()
        self.quotes[(quote.source, quote.target)] = quote

    def get(self, pair: Pair, max_age: int = 180, now: int | None = None) -> LiveQuote | None:
        quote = self.quotes.get(pair)
        if quote and (now or int(time.time())) - quote.observed_at <= max_age:
            return quote
        return None

    def pending(self) -> list[Pair]:
        # The six directed core pairs are the initial market check. Every
        # selected route contributes only pairs that are not already present.
        pairs = [(a, b) for a in CORE for b in CORE if a != b]
        for path in self.selected_routes:
            for pair in zip(path, path[1:]):
                if pair not in pairs:
                    pairs.append(pair)
        return [pair for pair in pairs if self.get(pair) is None]

    def select(self, candidate: Candidate) -> None:
        if candidate.path not in self.selected_routes:
            self.selected_routes.append(candidate.path)

    def coverage(self, candidate: Candidate) -> tuple[int, int]:
        pairs = list(zip(candidate.path, candidate.path[1:]))
        return sum(self.get(pair) is not None for pair in pairs), len(pairs)

    def route_quotes(self, candidate: Candidate) -> list[LiveQuote] | None:
        pairs = list(zip(candidate.path, candidate.path[1:]))
        quotes = [self.get(pair) for pair in pairs]
        return quotes if all(quotes) else None

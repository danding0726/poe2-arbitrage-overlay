"""Lifecycle state for the dashboard's continuous OCR polling loop."""

from __future__ import annotations

from fractions import Fraction


class OcrPollingState:
    """Issue generation tokens and reject results from an older OCR session."""

    def __init__(self):
        self.running = False
        self.in_flight = False
        self.generation = 0

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self.in_flight = False
        self.generation += 1

    def stop(self) -> None:
        if not self.running and not self.in_flight:
            return
        self.running = False
        self.in_flight = False
        self.generation += 1

    def restart(self) -> None:
        """Invalidate outstanding work while keeping monitoring enabled."""
        if not self.running:
            return
        self.in_flight = False
        self.generation += 1

    def begin(self) -> int | None:
        if not self.running or self.in_flight:
            return None
        self.in_flight = True
        return self.generation

    def finish(self, token: int) -> bool:
        """Return true only for the current live session's result."""
        if not self.running or token != self.generation:
            return False
        self.in_flight = False
        return True

    def fail(self, token: int) -> bool:
        return self.finish(token)


def complete_capture(captured, minimum_confidence: float = 0.90):
    """Return a complete, trusted order/stock capture or ``None``.

    Automatic mode deliberately does not use the ladder-only order fallback:
    both independently calibrated regions must agree that their values are
    readable before the dashboard is allowed to change a quote.
    """
    if not isinstance(captured, tuple) or len(captured) != 2:
        return None
    panel, ladder = captured
    if not isinstance(panel, dict) or not isinstance(ladder, dict):
        return None
    order = panel.get("selected_order")
    if not isinstance(order, dict):
        return None
    try:
        pay = int(order["pay"])
        receive = int(order["receive"])
        stock = int(ladder["stock"])
        order_confidence = float(order["confidence"])
        stock_confidence = float(ladder["confidence"])
    except (KeyError, TypeError, ValueError):
        return None
    if min(pay, receive, stock) <= 0:
        return None
    if min(order_confidence, stock_confidence) < minimum_confidence:
        return None
    best_quote = ladder.get("best_quote")
    if isinstance(best_quote, dict) and best_quote.get("pay") and best_quote.get("receive"):
        try:
            order_rate = Fraction(receive, pay)
            ladder_rate = Fraction(int(best_quote["receive"]), int(best_quote["pay"]))
        except (TypeError, ValueError, ZeroDivisionError):
            return None
        if abs(order_rate - ladder_rate) > order_rate / 50:
            return None
    gold = order.get("gold")
    if gold is not None:
        try:
            gold = int(gold)
        except (TypeError, ValueError):
            return None
        if gold < 0:
            return None
    return order, ladder, (pay, receive, stock, gold if gold is not None else -1)

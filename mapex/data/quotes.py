"""Live quotes: freshness (M10), spread stats and clock skew (P11)."""

from __future__ import annotations

from collections import deque
from statistics import median

from mapex.ctrader.client import Quote

QUOTE_MAX_AGE_S = 5.0  # shared-primitives §8
FROZEN_FEED_S = 120.0  # broker timestamp older than this = frozen feed
SKEW_WARN_S = 5.0
SKEW_BLOCK_S = 30.0


class Quotes:
    def __init__(self):
        self.last: dict[str, Quote] = {}
        self.spreads: dict[str, deque] = {}

    def update(self, quotes: dict[str, Quote]) -> None:
        for sym, q in quotes.items():
            self.last[sym] = q
            self.spreads.setdefault(sym, deque(maxlen=600)).append(q.spread)

    def age(self, sym: str, now: float) -> float | None:
        q = self.last.get(sym)
        return None if q is None else now - q.fetched_at

    def fresh(self, sym: str, now: float) -> Quote | None:
        """A quote fetched <= 5 s ago whose broker timestamp is not frozen and not skewed beyond 30 s."""
        q = self.last.get(sym)
        if q is None or now - q.fetched_at > QUOTE_MAX_AGE_S:
            return None
        if q.fetched_at - q.ts > FROZEN_FEED_S or q.ts - q.fetched_at > SKEW_BLOCK_S:
            return None
        return q

    def spread_median(self, sym: str) -> float | None:
        s = self.spreads.get(sym)
        return median(s) if s else None

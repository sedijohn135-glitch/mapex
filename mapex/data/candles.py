"""Candle fetching + cache. Every timeframe comes from the broker (never aggregated from M1, A3);
readers get closed bars only (A2) and a contiguity check on intraday series (shared-primitives §8)."""

from __future__ import annotations

import logging

from mapex.core.primitives import Bar, closed_bars
from mapex.core.timeutil import TF_SECONDS, market_open

log = logging.getLogger("mapex.data")

# bars kept per timeframe (M15 covers 10+ session occurrences for session_ATR)
HISTORY = {"MN1": 24, "W1": 60, "D1": 250, "H4": 250, "H1": 300, "M15": 1500, "M5": 300, "M1": 300}
INTRADAY = ("M1", "M5", "M15")


def missing_bars(bars: list[Bar], tf: str, symbol: str, lookback: int = 20) -> list[int]:
    """Open times of expected-but-absent bars (market open) among the last `lookback` bars."""
    step = TF_SECONDS[tf]
    tail = bars[-lookback:]
    out = []
    for a, b in zip(tail, tail[1:], strict=False):
        t = a.t + step
        while t < b.t:
            if market_open(symbol, t) and market_open(symbol, t + step - 1):
                out.append(t)
            t += step
    return out


class Candles:
    def __init__(self, client):
        self.client = client
        self.cache: dict[tuple[str, str], list[Bar]] = {}

    async def refresh(self, symbol: str, tf: str, now: float) -> list[Bar]:
        key = (symbol, tf)
        have = self.cache.get(key, [])
        span = TF_SECONDS[tf] * HISTORY[tf]
        if tf == "MN1":
            span = 31 * 86400 * HISTORY[tf]
        start = have[-2].t if len(have) >= 2 else int(now - span * 1.5 - 7 * 86400)
        fresh = None
        if len(have) < 2:  # start-up: one `count` request instead of dozens of 720 h chunks (D-68)
            fresh = await self.client.last_bars(symbol, tf, int(HISTORY[tf] * 1.2))
            if fresh is not None and len(fresh) < HISTORY[tf]:
                fresh = None  # short answer (server cap): load by ranges
        if fresh is None:
            fresh = await self.client.trendbars(symbol, tf, start, int(now) + TF_SECONDS[tf])
        merged = {b.t: b for b in have}
        merged.update({b.t: b for b in fresh})
        bars = [merged[t] for t in sorted(merged)][-int(HISTORY[tf] * 1.2):]
        self.cache[key] = bars
        return bars

    def closed(self, symbol: str, tf: str, now: float) -> list[Bar]:
        return closed_bars(self.cache.get((symbol, tf), []), tf, now)

    async def ensure_contiguous(self, symbol: str, tf: str, now: float) -> bool:
        """A missing intraday bar is refetched once; still missing -> False (skip the tick, logged)."""
        if tf not in INTRADAY:
            return True
        if not missing_bars(self.closed(symbol, tf, now), tf, symbol):
            return True
        self.cache.pop((symbol, tf), None)
        await self.refresh(symbol, tf, now)
        gaps = missing_bars(self.closed(symbol, tf, now), tf, symbol)
        if gaps:
            log.warning("%s %s: %d missing bar(s) after refetch, tick skipped", symbol, tf, len(gaps))
            return False
        return True

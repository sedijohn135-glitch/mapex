"""The candle helpers the Live Validator's rules read (copied from live-validator- app/market.py)."""

from __future__ import annotations

import statistics
from dataclasses import dataclass


@dataclass(frozen=True)
class Candle:
    t: float  # open time, epoch seconds
    o: float
    h: float
    l: float  # noqa: E741 - matches the OHLC vocabulary used throughout the rules
    c: float

    @property
    def body_top(self) -> float:
        return max(self.o, self.c)

    @property
    def body_bot(self) -> float:
        return min(self.o, self.c)

    @property
    def body(self) -> float:
        return self.body_top - self.body_bot

    @property
    def span(self) -> float:
        return self.h - self.l

    @property
    def bullish(self) -> bool:
        return self.c > self.o

    @property
    def bearish(self) -> bool:
        return self.c < self.o


def true_range(current: Candle, previous: Candle | None) -> float:
    if previous is None:
        return current.span
    return max(current.span, abs(current.h - previous.c), abs(current.l - previous.c))


def atr14(candles: list[Candle], period: int = 14) -> float | None:
    """Mean of the last `period` true ranges of closed candles. `None` when there is not enough data."""
    if len(candles) < period + 1:
        return None
    ranges = [true_range(candles[i], candles[i - 1]) for i in range(len(candles) - period, len(candles))]
    return sum(ranges) / period


def swing_high_indices(candles: list[Candle]) -> list[int]:
    """Strict swing highs: `h[i] > h[i-1]` and `h[i] > h[i+1]`."""
    return [
        i for i in range(1, len(candles) - 1) if candles[i].h > candles[i - 1].h and candles[i].h > candles[i + 1].h
    ]


def swing_low_indices(candles: list[Candle]) -> list[int]:
    return [
        i for i in range(1, len(candles) - 1) if candles[i].l < candles[i - 1].l and candles[i].l < candles[i + 1].l
    ]


def median_spread(samples: list[float], fallback_max_spread: float) -> float:
    """Median of the recent spread samples; fewer than 30 samples ⇒ max spread / 3."""
    if len(samples) < 30:
        return fallback_max_spread / 3.0
    return statistics.median(samples)


def session_levels(bars: dict[str, list[Candle]], now_ts: float) -> dict[str, float | None]:
    """Asian/London ranges, NY midnight & 6 AM opens, PDH/PDL, PWH/PWL (live-validator- app/market.py)."""
    from mapex.core.timeutil import ny, ny_at

    m5 = bars.get("M5", [])

    def m5_range(start_ts: float, end_ts: float) -> tuple[float | None, float | None]:
        inside = [c for c in m5 if start_ts <= c.t < end_ts]
        return (max(c.h for c in inside), min(c.l for c in inside)) if inside else (None, None)

    def m5_open_at(ts: float) -> float | None:
        return next((c.o for c in m5 if c.t == ts), None)

    asian_start, asian_end = ny_at(now_ts, 19, 0, -1), ny_at(now_ts, 0)
    if ny(now_ts).hour >= 19:  # before midnight the Asian session of the current NY day is still forming
        asian_start, asian_end = ny_at(now_ts, 19), ny_at(now_ts, 0, 0, 1)
    asian_high, asian_low = m5_range(asian_start, asian_end)
    london_high, london_low = m5_range(ny_at(now_ts, 2), ny_at(now_ts, 5))
    d1, w1 = bars.get("D1", []), bars.get("W1", [])
    return {
        "asian_high": asian_high, "asian_low": asian_low, "london_high": london_high, "london_low": london_low,
        "ny_midnight_open": m5_open_at(ny_at(now_ts, 0)), "six_am_open": m5_open_at(ny_at(now_ts, 6)),
        "pdh": d1[-1].h if d1 else None, "pdl": d1[-1].l if d1 else None,
        "pwh": w1[-1].h if w1 else None, "pwl": w1[-1].l if w1 else None,
    }

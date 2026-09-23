"""Hand-built candle fixtures."""

from __future__ import annotations

from datetime import datetime

from mapex.core.primitives import Bar
from mapex.core.timeutil import NY, TF_SECONDS


def ny_ts(y, m, d, hh=0, mm=0) -> int:
    return int(datetime(y, m, d, hh, mm, tzinfo=NY).timestamp())


def mk(rows, start: int = 1_700_000_000, tf: str = "M1") -> list[Bar]:
    """rows = [(o, h, l, c), ...] -> consecutive bars."""
    step = TF_SECONDS[tf]
    return [Bar(start + i * step, float(o), float(h), float(l), float(c)) for i, (o, h, l, c) in enumerate(rows)]


def flat(n: int, price: float = 100.0, wiggle: float = 0.2, start: int = 1_700_000_000, tf: str = "M1") -> list[Bar]:
    """Quiet alternating bars around `price` (range ~2 x wiggle, no swings beyond it)."""
    rows = []
    for i in range(n):
        o = price + (wiggle / 2 if i % 2 else -wiggle / 2)
        c = price - (wiggle / 2 if i % 2 else -wiggle / 2)
        rows.append((o, max(o, c) + wiggle / 2, min(o, c) - wiggle / 2, c))
    return mk(rows, start, tf)


def shift(bars: list[Bar], start: int, tf: str) -> list[Bar]:
    step = TF_SECONDS[tf]
    return [Bar(start + i * step, b.o, b.h, b.l, b.c) for i, b in enumerate(bars)]

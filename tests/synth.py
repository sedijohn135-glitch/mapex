"""Seeded synthetic market: an M1 or M15 random walk aggregated to every higher timeframe (tests only —
production never aggregates, A3)."""

from __future__ import annotations

import random
from datetime import UTC, datetime

from mapex.core.primitives import Bar
from mapex.core.timeutil import TF_SECONDS


def walk(n: int, start: int, tf: str, price: float, vol: float, seed: int, drift: float = 0.0) -> list[Bar]:
    rnd = random.Random(seed)
    step = TF_SECONDS[tf]
    out, p = [], price
    for i in range(n):
        o = p
        moves = [rnd.gauss(drift, vol) * p for _ in range(4)]
        path = [o]
        for m in moves:
            path.append(path[-1] + m)
        c = path[-1]
        out.append(Bar(start + i * step, round(o, 2), round(max(path), 2), round(min(path), 2), round(c, 2)))
        p = c
    return out


def _key(t: int, tf: str) -> int:
    if tf == "W1":
        d = datetime.fromtimestamp(t, UTC)
        monday = t - d.weekday() * 86400 - (t % 86400)
        return monday
    if tf == "MN1":
        d = datetime.fromtimestamp(t, UTC)
        return int(datetime(d.year, d.month, 1, tzinfo=UTC).timestamp())
    step = TF_SECONDS[tf]
    return t - t % step


def aggregate(bars: list[Bar], tf: str) -> list[Bar]:
    groups: dict[int, list[Bar]] = {}
    for b in bars:
        groups.setdefault(_key(b.t, tf), []).append(b)
    return [Bar(k, g[0].o, max(x.h for x in g), min(x.l for x in g), g[-1].c) for k, g in sorted(groups.items())]


def market(days: int = 260, seed: int = 7, price: float = 60000.0, end: int = 1_790_000_000,
           base_tf: str = "M15", vol: float = 0.0012) -> dict[str, list[Bar]]:
    step = TF_SECONDS[base_tf]
    n = days * 86400 // step
    end -= end % 86400
    base = walk(n, end - n * step, base_tf, price, vol, seed)
    out = {base_tf: base}
    for tf in ("M5", "M15", "H1", "H4", "D1", "W1", "MN1"):
        if TF_SECONDS[tf] > step:
            out[tf] = aggregate(base, tf)
    return out


def market_m1(days_m1: int = 36, days_total: int = 380, seed: int = 5, price: float = 60000.0,
              end: int = 1_790_000_000, vol_m15: float = 0.0012, vol_m1: float = 0.0003) -> dict[str, list[Bar]]:
    """M15 walk for the long history, then an M1 walk for the last `days_m1` days; higher TFs aggregated."""
    end -= end % 86400
    m1_start = end - days_m1 * 86400
    n15 = (days_total - days_m1) * 96
    old = walk(n15, m1_start - n15 * 900, "M15", price, vol_m15, seed)
    m1 = walk(days_m1 * 1440, m1_start, "M1", old[-1].c, vol_m1, seed + 1)
    m15 = old + aggregate(m1, "M15")
    out = {"M1": m1, "M5": aggregate(m1, "M5"), "M15": m15}
    for tf in ("H1", "H4", "D1", "W1", "MN1"):
        out[tf] = aggregate(m15, tf)
    return out

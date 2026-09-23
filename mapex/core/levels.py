"""Liquidity-level helpers shared by the GEM1 registry and GEM2 raid detection (shared-primitives §1, §3, §7)."""

from __future__ import annotations

from dataclasses import dataclass

from mapex.core.primitives import Bar, atr
from mapex.core.timeutil import (
    SESSION_LABELS,
    SESSION_RANGES,
    TF_SECONDS,
    current_session,
    in_window,
    ny,
    ny_at,
    ny_time,
)

EQ_TOL_PCT = 0.0010  # GEM1 §0B "0.1% of price"
TOUCH_TOL_PCT = 0.0003  # GEM1 §0D "within 1 price unit" (1 / 3390)
CLUSTER_WIDTH_PCT = 0.0015  # GEM1 §0B "5 price units" (5 / 3390)
SESSION_ATR_OCCURRENCES = 10
SESSION_ATR_MIN_OCCURRENCES = 5
SESSION_ATR_FALLBACK_MULT = 4.0  # 4 x ATR14(H1)


def level_tol(price: float, atr_tf: float, tick: float) -> float:
    return max(TOUCH_TOL_PCT * price, 0.15 * atr_tf, 3 * tick)


def sweep_status(level: float, side: str, later: list[Bar], price: float) -> tuple[str, int | None]:
    """SWEPT_BODY if a later bar closes beyond; SWEPT_WICK if a wick reaches within tolerance; else UNTOUCHED.

    Returns the status and the index (in `later`) of the first bar producing it.
    """
    tol = TOUCH_TOL_PCT * price
    wick_at = None
    for k, b in enumerate(later):
        if (side == "high" and b.c > level) or (side == "low" and b.c < level):
            return "SWEPT_BODY", k
        if wick_at is None and ((side == "high" and b.h >= level - tol) or (side == "low" and b.l <= level + tol)):
            wick_at = k
    if wick_at is not None:
        return "SWEPT_WICK", wick_at
    return "UNTOUCHED", None


def group_equal(levels: list[tuple[float, int]], price: float) -> list[list[tuple[float, int]]]:
    """Group (level, idx) pairs whose values lie within EQ_TOL_PCT of the running group mean."""
    tol = EQ_TOL_PCT * price
    groups: list[list[tuple[float, int]]] = []
    for lv in sorted(levels):
        if groups and abs(lv[0] - sum(x[0] for x in groups[-1]) / len(groups[-1])) <= tol:
            groups[-1].append(lv)
        else:
            groups.append([lv])
    return groups


def touch_count(level: float, side: str, bars: list[Bar], tol: float, min_gap: int = 3) -> int:
    """Separate touches (wick within tol) spaced at least `min_gap` bars apart (algo-magnet rule)."""
    count, last = 0, -10**9
    for k, b in enumerate(bars):
        wick = b.h if side == "high" else b.l
        if abs(wick - level) <= tol and k - last >= min_gap:
            count += 1
            last = k
    return count


# ---------------------------------------------------------------- sessions

@dataclass(frozen=True)
class SessionRange:
    name: str
    start: int  # window start (UTC epoch)
    end: int
    high: float
    low: float
    high_t: int
    low_t: int


def _window_bars(bars: list[Bar], start: int, end: int, tf_s: int) -> list[Bar]:
    """Bars whose close time lies in [start, end) — windows are half-open on bar close time."""
    return [b for b in bars if start < b.t + tf_s <= end]


def session_window(name: str, day_ts: float) -> tuple[int, int]:
    """[start, end) of a named GEM1 session range on the NY day of `day_ts` (Asia 19:00 belongs to that day)."""
    s, e = SESSION_RANGES[name]
    return ny_at(day_ts, s.hour, s.minute), ny_at(day_ts, e.hour, e.minute)


def completed_sessions(m15: list[Bar], now: float, days: int = 4) -> list[SessionRange]:
    """Every completed named session window of today and the previous `days - 1` days (from M15 bars)."""
    out = []
    for d in range(days - 1, -1, -1):
        day = now - d * 86400
        for name in SESSION_RANGES:
            start, end = session_window(name, day)
            if end > now:
                continue
            w = _window_bars(m15, start, end, TF_SECONDS["M15"])
            if not w:
                continue
            hb = max(w, key=lambda b: (b.h, -b.t))
            lb = min(w, key=lambda b: (b.l, b.t))
            out.append(SessionRange(name, start, end, hb.h, lb.l, hb.t, lb.t))
    return out


def label_window_occurrences(name: str, now: float, count: int) -> list[tuple[int, int]]:
    """Completed occurrences of a current_session label window, most recent first."""
    for label, s, e in SESSION_LABELS:
        if label != name:
            continue
        out = []
        for d in range(0, count + 30):
            day = now - d * 86400
            start = ny_at(day, s.hour, s.minute)
            end = ny_at(day, e.hour, e.minute, 1 if e <= s else 0)
            if end <= now:
                out.append((start, end))
            if len(out) >= count + 10:
                break
        return out
    raise KeyError(name)


def session_atr(m15: list[Bar], h1: list[Bar], now: float) -> tuple[float, bool]:
    """Mean high-low of the current named session over its last 10 completed occurrences (M15 bars).

    Fallback when fewer than 5 occurrences have data: 4 x ATR14(H1). Returns (value, fallback_used).
    """
    name = current_session(now)
    ranges = []
    for start, end in label_window_occurrences(name, now, SESSION_ATR_OCCURRENCES):
        w = _window_bars(m15, start, end, TF_SECONDS["M15"])
        if w:
            ranges.append(max(b.h for b in w) - min(b.l for b in w))
        if len(ranges) == SESSION_ATR_OCCURRENCES:
            break
    if len(ranges) < SESSION_ATR_MIN_OCCURRENCES:
        return SESSION_ATR_FALLBACK_MULT * atr(h1, 14), True
    return sum(ranges) / len(ranges), False


def sessions_between(t0: float, t1: float) -> int:
    """Number of current_session label boundaries (02:00, 07:00, 19:00 NY) crossed in (t0, t1]."""
    if t1 <= t0:
        return 0
    n, day = 0, t0 - 86400
    while day <= t1 + 86400:
        for _, s, _e in SESSION_LABELS:
            b = ny_at(day, s.hour, s.minute)
            if t0 < b <= t1:
                n += 1
        day += 86400
    return n


def day_open_at(bars: list[Bar], ts: int) -> float | None:
    """Open of the first bar opening at or after `ts`."""
    for b in bars:
        if b.t >= ts:
            return b.o
    return None


def in_ny_window(ts: float, start, end) -> bool:
    return in_window(ny_time(ts), start, end)


def ny_date(ts: float):
    return ny(ts).date()

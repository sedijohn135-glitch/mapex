"""Candle primitives and PD-array detectors, exactly as pinned in shared-primitives.md §2-§6.

Every function is pure and only looks at the bars it is given; callers pass closed bars only.
Direction strings follow GEM: "buy" = bullish, "sell" = bearish.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field
from statistics import median
from typing import NamedTuple

from mapex.core.timeutil import TF_SECONDS, is_closed

DISP_BODY_ATR = 1.0  # displacement body >= 1.0 x ATR14
DISP_BODY_RANGE = 0.60  # "large body, shallow wicks"
LARGE_LEG_ATR = 3.0  # LARGE displacement: leg >= 3 x ATR14
PB_BODY_ATR = 1.5  # propulsion block body
VOID_ATR = 1.0  # VOID: unfilled FVG height >= 1 x ATR14


class Bar(NamedTuple):
    t: int  # open time, UTC epoch seconds
    o: float
    h: float
    l: float
    c: float

    @property
    def body_top(self) -> float:
        return max(self.o, self.c)

    @property
    def body_bot(self) -> float:
        return min(self.o, self.c)

    @property
    def body(self) -> float:
        return abs(self.c - self.o)

    @property
    def range(self) -> float:
        return self.h - self.l

    @property
    def bull(self) -> bool:
        return self.c > self.o

    @property
    def bear(self) -> bool:
        return self.c < self.o


def body_ratio(b: Bar) -> float:
    return b.body / b.range if b.range > 0 else 0.0


def closed_bars(bars: list[Bar], tf: str, now: float) -> list[Bar]:
    """Bars whose close (+2 s grace) is at or before `now` — the no-lookahead cut (§2)."""
    if tf == "MN1":
        return [b for b in bars if is_closed(b.t, tf, now)]
    cut = bisect.bisect_right(bars, now - TF_SECONDS[tf] - 2, key=lambda b: b.t)
    return bars[:cut]


def round_half_up(x: float) -> int:
    return int(math.floor(x + 0.5))


# ---------------------------------------------------------------- ATR (§3)

def true_range(bars: list[Bar], i: int) -> float:
    b = bars[i]
    if i == 0:
        return b.range
    pc = bars[i - 1].c
    return max(b.h - b.l, abs(b.h - pc), abs(b.l - pc))


def atr(bars: list[Bar], n: int = 14) -> float:
    """Mean of the last n true ranges of the given (closed) bars."""
    if not bars:
        return 0.0
    k = min(n, len(bars) - 1) if len(bars) > 1 else 1
    trs = [true_range(bars, i) for i in range(len(bars) - k, len(bars))]
    return sum(trs) / len(trs)


def atr_series(bars: list[Bar], n: int = 14) -> list[float]:
    """ATR14 as known at the close of each bar (no future bars)."""
    out, window, s = [], [], 0.0
    for i in range(len(bars)):
        tr = true_range(bars, i)
        if i == 0 and len(bars) > 1:
            out.append(tr)
            continue
        window.append(tr)
        s += tr
        if len(window) > n:
            s -= window.pop(0)
        out.append(s / len(window))
    return out


# ---------------------------------------------------------------- swings & structure (§4)

def swing_highs(bars: list[Bar]) -> list[int]:
    return [i for i in range(1, len(bars) - 1) if bars[i].h > bars[i - 1].h and bars[i].h > bars[i + 1].h]


def swing_lows(bars: list[Bar]) -> list[int]:
    return [i for i in range(1, len(bars) - 1) if bars[i].l < bars[i - 1].l and bars[i].l < bars[i + 1].l]


def major_swings(bars: list[Bar], idx: list[int], kind: str) -> list[int]:
    """Swing higher (lower) than the previous two and the next two swings of the same kind."""
    val = (lambda i: bars[i].h) if kind == "high" else (lambda i: -bars[i].l)
    out = []
    for k in range(2, len(idx) - 2):
        v = val(idx[k])
        if all(v > val(idx[j]) for j in (k - 2, k - 1, k + 1, k + 2)):
            out.append(idx[k])
    return out


@dataclass(frozen=True)
class Break:
    idx: int  # bar whose body closed beyond the swing
    direction: str  # buy | sell
    level: float
    swing_idx: int


def structure_breaks(bars: list[Bar]) -> list[Break]:
    """BOS/MSS: a body close beyond the most recent confirmed swing (wick-only never counts)."""
    hs, ls = set(swing_highs(bars)), set(swing_lows(bars))
    out: list[Break] = []
    last_h: tuple[float, int] | None = None
    last_l: tuple[float, int] | None = None
    for i, b in enumerate(bars):
        if last_h and b.c > last_h[0]:
            out.append(Break(i, "buy", last_h[0], last_h[1]))
            last_h = None
        if last_l and b.c < last_l[0]:
            out.append(Break(i, "sell", last_l[0], last_l[1]))
            last_l = None
        # the swing at i-1 is confirmed by the close of bar i
        if i - 1 in hs:
            last_h = (bars[i - 1].h, i - 1)
        if i - 1 in ls:
            last_l = (bars[i - 1].l, i - 1)
    return out


def structure_direction(bars: list[Bar]) -> str | None:
    """bullish (HH+HL) | bearish (LH+LL) on the last two major swings, else the last BOS direction."""
    mh = major_swings(bars, swing_highs(bars), "high")
    ml = major_swings(bars, swing_lows(bars), "low")
    if len(mh) >= 2 and len(ml) >= 2:
        hh = bars[mh[-1]].h > bars[mh[-2]].h
        hl = bars[ml[-1]].l > bars[ml[-2]].l
        if hh and hl:
            return "bullish"
        if not hh and not hl:
            return "bearish"
    brk = structure_breaks(bars)
    if brk:
        return "bullish" if brk[-1].direction == "buy" else "bearish"
    return None


def cisd_anchor(bars: list[Bar], disp_idx: int, direction: str) -> tuple[float, int] | None:
    """Open of the first candle of the last opposite-close run right before the displacement."""
    j = disp_idx - 1
    opposite = (lambda b: b.bear) if direction == "buy" else (lambda b: b.bull)
    if j < 0 or not opposite(bars[j]):
        return None
    while j - 1 >= 0 and opposite(bars[j - 1]):
        j -= 1
    return bars[j].o, j


# ---------------------------------------------------------------- FVG & displacement (§5, §6)

@dataclass
class Zone:
    kind: str  # FVG IFVG BPR VI VOID VACUUM OB BB RB PB
    direction: str  # buy | sell
    low: float
    high: float
    anchor: float
    formed_at: int  # bar open time the zone is dated by
    idx: int  # bar index the zone is dated by
    disp_idx: int | None = None  # creating displacement bar (Filter 1)
    tf: str = ""
    mitigated_at: int | None = None
    extra: dict = field(default_factory=dict)

    @property
    def ce(self) -> float:
        return (self.low + self.high) / 2


def fvgs(bars: list[Bar], tf: str = "") -> list[Zone]:
    """Strict 3-bar FVG, no body overlap. formed_at = t of the middle bar."""
    out = []
    for m in range(1, len(bars) - 1):
        a, c = bars[m - 1], bars[m + 1]
        if c.l > a.h:
            out.append(Zone("FVG", "buy", a.h, c.l, (a.h + c.l) / 2, bars[m].t, m, m, tf))
        elif c.h < a.l:
            out.append(Zone("FVG", "sell", c.h, a.l, (c.h + a.l) / 2, bars[m].t, m, m, tf))
    return out


def fvg_at(bars: list[Bar], m: int, direction: str) -> bool:
    if m < 1 or m + 1 >= len(bars):
        return False
    a, c = bars[m - 1], bars[m + 1]
    return c.l > a.h if direction == "buy" else c.h < a.l


def is_displacement(bars: list[Bar], i: int, atr_v: float) -> bool:
    b = bars[i]
    if atr_v <= 0 or b.body < DISP_BODY_ATR * atr_v or body_ratio(b) < DISP_BODY_RANGE:
        return False
    direction = "buy" if b.bull else "sell"
    return fvg_at(bars, i, direction)


def displacement_quality(bars: list[Bar], i: int, atr_v: float) -> tuple[str, int]:
    """GEM1 Step 7 displacement points: LARGE 13 / MODERATE 9 / SMALL 4."""
    b = bars[i]
    direction = "buy" if b.bull else "sell"
    if atr_v > 0 and fvg_at(bars, i, direction):
        leg_end = i
        while leg_end + 1 < len(bars) and leg_end - i < 2 and (bars[leg_end + 1].bull == b.bull) \
                and bars[leg_end + 1].body > 0:
            leg_end += 1
        leg = abs(bars[leg_end].c - b.o)
        if leg >= LARGE_LEG_ATR * atr_v:
            return "LARGE", 13
        if b.body >= DISP_BODY_ATR * atr_v:
            return "MODERATE", 9
    return "SMALL", 4


def first_touch(zone: Zone, bars: list[Bar], start: int) -> int | None:
    """Index of the first bar from `start` that trades into the zone."""
    for k in range(start, len(bars)):
        b = bars[k]
        if (zone.direction == "buy" and b.l <= zone.high) or (zone.direction == "sell" and b.h >= zone.low):
            return k
    return None


def mark_mitigation(zone: Zone, bars: list[Bar]) -> Zone:
    start = (zone.disp_idx if zone.disp_idx is not None else zone.idx) + 2
    k = first_touch(zone, bars, start)
    zone.mitigated_at = bars[k].t if k is not None else None
    return zone


def ifvgs(bars: list[Bar], fvg_list: list[Zone]) -> list[Zone]:
    """FVG re-entered and respected: tapped, and no body close beyond CE against it since the tap."""
    out = []
    for z in fvg_list:
        k = first_touch(z, bars, z.idx + 2)
        if k is None:
            continue
        broken = any((z.direction == "buy" and b.c < z.ce) or (z.direction == "sell" and b.c > z.ce)
                     for b in bars[k:])
        if not broken:
            out.append(Zone("IFVG", z.direction, z.low, z.high, z.ce, z.formed_at, z.idx, z.disp_idx, z.tf,
                            bars[k].t, {"tapped_at": bars[k].t}))
    return out


def bprs(bars: list[Bar], fvg_list: list[Zone]) -> list[Zone]:
    """FVG overlapped by a later opposite FVG; zone = overlap; dead once a body traverses it."""
    n = len(bars)
    lo_c, hi_c = [math.inf] * (n + 1), [-math.inf] * (n + 1)  # suffix min/max close
    for i in range(n - 1, -1, -1):
        lo_c[i], hi_c[i] = min(bars[i].c, lo_c[i + 1]), max(bars[i].c, hi_c[i + 1])
    out = []
    for i, a in enumerate(fvg_list):
        for b in fvg_list[i + 1:]:
            if b.direction == a.direction:
                continue
            lo, hi = max(a.low, b.low), min(a.high, b.high)
            if lo >= hi:
                continue
            k = min(b.idx + 2, n)
            if lo_c[k] < lo and hi_c[k] > hi:
                continue  # traversed body-to-body
            out.append(Zone("BPR", b.direction, lo, hi, (lo + hi) / 2, b.formed_at, b.idx, b.disp_idx, a.tf))
    return out


def inside_bpr(zone: Zone, bpr_list: list[Zone]) -> bool:
    return any(p.low <= zone.low and zone.high <= p.high for p in bpr_list)


def volume_imbalances(bars: list[Bar], tf: str = "") -> list[Zone]:
    out = []
    for i in range(1, len(bars)):
        p, b = bars[i - 1], bars[i]
        if b.body_bot > p.body_top and b.l <= p.h:
            out.append(Zone("VI", "buy", p.body_top, b.body_bot, (p.body_top + b.body_bot) / 2, b.t, i, i, tf))
        elif b.body_top < p.body_bot and b.h >= p.l:
            out.append(Zone("VI", "sell", b.body_top, p.body_bot, (b.body_top + p.body_bot) / 2, b.t, i, i, tf))
    return out


def vacuums(bars: list[Bar], tf: str = "") -> list[Zone]:
    out = []
    for i in range(1, len(bars)):
        p, b = bars[i - 1], bars[i]
        if b.l > p.h:
            out.append(Zone("VACUUM", "buy", p.h, b.l, (p.h + b.l) / 2, b.t, i, i, tf))
        elif b.h < p.l:
            out.append(Zone("VACUUM", "sell", b.h, p.l, (b.h + p.l) / 2, b.t, i, i, tf))
    return out


def voids(bars: list[Bar], fvg_list: list[Zone], atrs: list[float]) -> list[Zone]:
    """FVG of height >= 1 x ATR14 that is still unfilled (not fully traded through)."""
    out = []
    for z in fvg_list:
        if z.high - z.low < VOID_ATR * atrs[z.idx]:
            continue
        later = bars[z.idx + 2:]
        filled = any(b.l <= z.low for b in later) if z.direction == "buy" else any(b.h >= z.high for b in later)
        if not filled:
            out.append(Zone("VOID", z.direction, z.low, z.high, z.ce, z.formed_at, z.idx, z.disp_idx, z.tf))
    return out


def order_blocks(bars: list[Bar], atrs: list[float], breaks: list[Break], tf: str = "") -> list[Zone]:
    """Opposite-close run before a displacement that also produces a BOS/MSS; anchor = CISD open."""
    out = []
    brk_by_dir = {"buy": [b.idx for b in breaks if b.direction == "buy"],
                  "sell": [b.idx for b in breaks if b.direction == "sell"]}
    for i in range(1, len(bars) - 1):
        b = bars[i]
        if not is_displacement(bars, i, atrs[i]):
            continue
        direction = "buy" if b.bull else "sell"
        cisd = cisd_anchor(bars, i, direction)
        if cisd is None:
            continue
        # the displacement leg (this bar + 2) must produce the BOS/MSS
        if not any(i <= k <= i + 2 for k in brk_by_dir[direction]):
            continue
        # imbalance nearby: an FVG in the OB direction within the 3 bars after the run
        if not any(fvg_at(bars, m, direction) for m in range(i, min(i + 3, len(bars) - 1))):
            continue
        run = bars[cisd[1]:i]
        out.append(Zone("OB", direction, min(x.l for x in run), max(x.h for x in run), cisd[0], bars[cisd[1]].t,
                        cisd[1], i, tf))
    return out


def breaker_blocks(bars: list[Bar], atrs: list[float], obs: list[Zone], tf: str = "") -> list[Zone]:
    """OB invalidated by a body close through it within an opposite displacement leg -> breaker."""
    out = []
    for ob in obs:
        against = "sell" if ob.direction == "buy" else "buy"
        for k in range(ob.disp_idx + 1, len(bars)):
            c = bars[k].c
            if (ob.direction == "buy" and c < ob.low) or (ob.direction == "sell" and c > ob.high):
                disp = next((j for j in range(k, min(k + 3, len(bars) - 1))
                             if is_displacement(bars, j, atrs[j]) and ("buy" if bars[j].bull else "sell") == against),
                            None)
                if disp is not None:
                    cisd = cisd_anchor(bars, disp, against)
                    anchor = cisd[0] if cisd else bars[disp].o
                    out.append(Zone("BB", against, ob.low, ob.high, anchor, bars[disp].t, disp, disp, tf,
                                    extra={"failed_ob_at": ob.formed_at}))
                break
    return out


def rejection_blocks(bars: list[Bar], atrs: list[float], tf: str = "") -> list[Zone]:
    """Down-close candle whose high is broken and closed above by the next candle (mirrored bearish)."""
    out = []
    for i in range(len(bars) - 1):
        b, n = bars[i], bars[i + 1]
        if b.bear and n.c > b.h and is_displacement(bars, i + 1, atrs[i + 1]):
            out.append(Zone("RB", "buy", b.l, b.body_bot, b.o, b.t, i, i + 1, tf))
        elif b.bull and n.c < b.l and is_displacement(bars, i + 1, atrs[i + 1]):
            out.append(Zone("RB", "sell", b.body_top, b.h, b.o, b.t, i, i + 1, tf))
    return out


def propulsion_blocks(bars: list[Bar], atrs: list[float], tf: str = "") -> list[Zone]:
    """Single body >= 1.5 x ATR14 inside a displacement leg; mean threshold = 50 % of its body (logging)."""
    out = []
    for i in range(1, len(bars) - 1):
        b = bars[i]
        if b.body >= PB_BODY_ATR * atrs[i] and any(is_displacement(bars, j, atrs[j]) for j in (i - 1, i)):
            d = "buy" if b.bull else "sell"
            out.append(Zone("PB", d, b.body_bot, b.body_top, (b.o + b.c) / 2, b.t, i, i, tf))
    return out


def median_body(bars: list[Bar]) -> float:
    return median([b.body for b in bars]) if bars else 0.0

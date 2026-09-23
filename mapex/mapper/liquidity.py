"""GEM1 STEP 0 — Liquidity Discovery Engine: 0A/0B/0C discovery, 0D sweep status, 0E LPS."""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from datetime import timedelta

from mapex.core import levels as lv
from mapex.core import primitives as pr
from mapex.core.primitives import Bar, Zone
from mapex.core.timeutil import bar_close, ny

BASE = {"D1": 60, "H4": 40, "H1": 20}
TF_RANK = {"D1": 3, "H4": 2, "H1": 1}
# 0E type multipliers (GEM1 exactly). SWING = plain swing with no other type (DECISIONS D-07).
MULT = {"PM": 1.00, "PW": 0.95, "OLD": 0.92, "EQ3": 0.88, "MAGNET": 0.85, "PD": 0.83, "SESSION": 0.78,
        "RESTING": 0.75, "EQ2": 0.72, "ENGINEERED": 0.65, "RANGE": 0.62, "VOID": 0.70, "VACUUM": 0.70,
        "SWING": 0.62}
# discovery order inside a timeframe (0A, then 0B, then 0C) — drives LIQ_ ids
ORDER = ["OLD", "PD", "PW", "PM", "SESSION", "SWING", "MAGNET", "EQ3", "EQ2", "RANGE", "ENGINEERED", "RESTING",
         "VOID", "VACUUM"]
BONUS_CAP = 35
OLD_SWING_TRADING_DAYS = 5
RESTING_SESSIONS = 3
ENGINEERED_BARS = 10


@dataclass
class Pool:
    tf: str
    side: str  # high | low
    price: float
    kinds: list[str]
    labels: list[str]
    avail_t: int  # when the level became known (no pool is used before this)
    touches: int = 1
    id: str = ""
    status: str = "UNTOUCHED"
    untouched: bool = True
    swept_at: int | None = None
    lps: int = 0
    bonus: dict = field(default_factory=dict)
    importance: str = "LOW"
    generated_pda_id: str | None = None

    @property
    def kind(self) -> str:
        return max(self.kinds, key=lambda k: (MULT[k], -ORDER.index(k)))

    @property
    def mult(self) -> float:
        return MULT[self.kind]

    def schema_type(self) -> str:
        k, hi = self.kind, self.side == "high"
        return {"PM": "PMH" if hi else "PML", "PW": "PWH" if hi else "PWL", "PD": "PDH" if hi else "PDL",
                "SESSION": "SESSION_H" if hi else "SESSION_L", "EQ3": "EQH" if hi else "EQL",
                "EQ2": "EQH" if hi else "EQL", "OLD": "SWING_H" if hi else "SWING_L",
                "SWING": "SWING_H" if hi else "SWING_L", "RANGE": "SWING_H" if hi else "SWING_L",
                "MAGNET": "ALGO_MAGNET", "RESTING": "RESTING", "ENGINEERED": "ENGINEERED", "VOID": "VOID",
                "VACUUM": "VACUUM"}[k]

    @property
    def erl(self) -> bool:
        """External range liquidity: every pool type except internal range liquidity and voids."""
        return self.kind not in {"RANGE", "VOID", "VACUUM"}


def importance(lps: int) -> str:
    if lps >= 80:
        return "CRITICAL"
    if lps >= 65:
        return "HIGH"
    if lps >= 50:
        return "MEDIUM"
    return "LOW"


def lps_score(tf: str, mult: float, bonus_total: int) -> int:
    """Final LPS = round((base x multiplier) + bonuses), bonus capped at +35, LPS capped at 100."""
    return min(100, pr.round_half_up(BASE[tf] * mult + min(BONUS_CAP, bonus_total)))


def trading_days_between(t0: float, t1: float) -> int:
    d0, d1 = ny(t0).date(), ny(t1).date()
    n, d = 0, d0
    while d < d1:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


@dataclass
class DealingRange:
    high: float
    low: float
    high_t: int
    low_t: int

    @property
    def eq(self) -> float:
        return (self.high + self.low) / 2

    def contains(self, x: float) -> bool:
        return self.low < x < self.high


def dealing_range(d1: list[Bar]) -> DealingRange:
    """2A: latest confirmed D1 major swing high and low (fallback: 60-bar extremes, DECISIONS D-08)."""
    mh = pr.major_swings(d1, pr.swing_highs(d1), "high")
    ml = pr.major_swings(d1, pr.swing_lows(d1), "low")
    if mh and ml:
        return DealingRange(d1[mh[-1]].h, d1[ml[-1]].l, d1[mh[-1]].t, d1[ml[-1]].t)
    tail = d1[-60:]
    hb, lb = max(tail, key=lambda b: b.h), min(tail, key=lambda b: b.l)
    return DealingRange(hb.h, lb.l, hb.t, lb.t)


def _sweeps_of_side(bars: list[Bar], side: str) -> list[int]:
    """Bars whose wick took out the most recent confirmed swing of `side`."""
    idx = set(pr.swing_highs(bars) if side == "high" else pr.swing_lows(bars))
    out, last = [], None
    for i, b in enumerate(bars):
        if last is not None and ((side == "high" and b.h > last) or (side == "low" and b.l < last)):
            out.append(i)
            last = None
        if i - 1 in idx:
            last = bars[i - 1].h if side == "high" else bars[i - 1].l
    return out


def discover_tf(tf: str, bars: list[Bar], ctx: dict) -> list[Pool]:
    """0A/0B/0C for one timeframe (before de-duplication)."""
    price, now, tick = ctx["price"], ctx["now"], ctx["tick"]
    pools: list[Pool] = []
    if len(bars) < 5:
        return pools
    rng: DealingRange = ctx["dealing_range"]
    atr_tf = pr.atr(bars, 14)
    tol = lv.level_tol(price, atr_tf, tick)

    if tf == "D1":  # calendar extremes: PMH/PML, PWH/PWL, PDH/PDL of the last 3 days
        for key, src, name in (("PM", "MN1", "Previous month"), ("PW", "W1", "Previous week")):
            ser = ctx["bars"].get(src, [])
            if ser:
                b = ser[-1]
                at = bar_close(b.t, src)
                pools.append(Pool(tf, "high", b.h, [key], [f"{name} high"], at))
                pools.append(Pool(tf, "low", b.l, [key], [f"{name} low"], at))
        for k, b in enumerate(reversed(bars[-3:]), start=1):
            at = bar_close(b.t, "D1")
            pools.append(Pool(tf, "high", b.h, ["PD"], [f"PDH D-{k}"], at))
            pools.append(Pool(tf, "low", b.l, ["PD"], [f"PDL D-{k}"], at))

    for side in ("high", "low"):
        idx = pr.swing_highs(bars) if side == "high" else pr.swing_lows(bars)
        major = set(pr.major_swings(bars, idx, side))
        val = (lambda i: bars[i].h) if side == "high" else (lambda i: bars[i].l)
        avail = {i: bar_close(bars[i + 1].t, tf) for i in idx}
        for i in idx:
            v = val(i)
            if i in major and trading_days_between(bars[i].t, now) > OLD_SWING_TRADING_DAYS:
                kind, label = "OLD", f"{tf} old swing {side}"
            elif i not in major and rng.contains(v):
                kind, label = "RANGE", f"{tf} range liquidity"
            else:
                kind, label = "SWING", f"{tf} swing {side}"
            pools.append(Pool(tf, side, v, [kind], [label], avail[i]))
        groups = lv.group_equal([(val(i), i) for i in idx], price)
        sweeps = _sweeps_of_side(bars, "low" if side == "high" else "high")
        for g in groups:
            level = sum(x[0] for x in g) / len(g)
            last_i = max(x[1] for x in g)
            at = max(avail[x[1]] for x in g)
            if len(g) >= 2:
                eq = "EQ3" if len(g) >= 3 else "EQ2"
                name = "Equal Highs" if side == "high" else "Equal Lows"
                p = Pool(tf, side, level, [eq], [f"{tf} {name} — {len(g)} Touches"], at, touches=len(g))
                if any(0 <= last_i - s <= ENGINEERED_BARS for s in sweeps):
                    p.kinds.append("ENGINEERED")
                    p.labels.append("Engineered liquidity")
                pools.append(p)
            touches = lv.touch_count(level, side, bars, tol)
            if touches >= 3:
                pools.append(Pool(tf, side, level, ["MAGNET"], [f"{tf} algo magnet — {touches} touches"], at,
                                  touches=touches))

    if tf == "H1":  # named session highs/lows, today and the previous 3 days
        for s in lv.completed_sessions(ctx["bars"].get("M15", []), now, days=4):
            day = ny(s.start).strftime("%a %d")
            pools.append(Pool(tf, "high", s.high, ["SESSION"], [f"{s.name} High {day}"], s.end))
            pools.append(Pool(tf, "low", s.low, ["SESSION"], [f"{s.name} Low {day}"], s.end))

    atrs = pr.atr_series(bars)
    f = pr.fvgs(bars, tf)
    gaps = [z for z in pr.voids(bars, f, atrs)] + [
        z for z in pr.vacuums(bars, tf)
        if not any((b.l <= z.low if z.direction == "buy" else b.h >= z.high) for b in bars[z.idx + 1:])]
    for z in gaps:
        at = bar_close(bars[min(z.idx + 1, len(bars) - 1)].t, tf)
        pools.append(Pool(tf, "high" if z.ce > price else "low", z.ce, [z.kind], [f"{tf} liquidity {z.kind.lower()}"],
                          at))
    return pools


def dedupe(pools: list[Pool], price: float) -> list[Pool]:
    """Merge levels within EQ_TOL_PCT on the same timeframe and side: highest multiplier wins, labels kept."""
    tol = lv.EQ_TOL_PCT * price
    out: list[Pool] = []
    for side in ("high", "low"):
        ps = sorted((p for p in pools if p.side == side), key=lambda p: (p.price, p.avail_t))
        group: list[Pool] = []
        for p in ps + [None]:
            if p is not None and group and abs(p.price - group[0].price) <= tol:
                group.append(p)
                continue
            if group:
                best = max(group, key=lambda q: (q.mult, -ORDER.index(q.kind), q.avail_t))
                kinds = list(dict.fromkeys(k for q in group for k in q.kinds))
                labels = list(dict.fromkeys(label for q in group for label in q.labels))
                out.append(Pool(best.tf, side, best.price, kinds, labels, best.avail_t,
                                touches=max(q.touches for q in group)))
            group = [p] if p is not None else []
    return out


def later_bars(pool: Pool, own: list[Bar], h1: list[Bar]) -> list[Bar]:
    """Bars after the level became known: own timeframe, then H1 bars after the last own bar closed."""
    tail = own[bisect.bisect_left(own, pool.avail_t, key=lambda b: b.t):]
    last_close = bar_close(own[-1].t, pool.tf) if own else pool.avail_t
    return tail + h1[bisect.bisect_left(h1, max(last_close, pool.avail_t), key=lambda b: b.t):]


def classify(pools: list[Pool], bars: dict[str, list[Bar]], price: float, session_atr: float, now: float) -> None:
    """0D status + RESTING tag (untouched pool older than 3 sessions)."""
    for p in pools:
        later = later_bars(p, bars[p.tf], bars.get("H1", []))
        status, k = lv.sweep_status(p.price, p.side, later, price)
        p.status = status
        p.untouched = status == "UNTOUCHED"
        p.swept_at = later[k].t if k is not None else None
        if p.untouched:
            if lv.sessions_between(p.avail_t, now) > RESTING_SESSIONS and "RESTING" not in p.kinds:
                p.kinds.append("RESTING")
                p.labels.append("Resting liquidity")
            if abs(price - p.price) <= 0.5 * session_atr:
                p.status = "BEING_TARGETED"


def score(pools: list[Pool], price: float, htf_zones: list[Zone]) -> None:
    """0E LPS with the four bonuses (cap +35)."""
    tol = lv.EQ_TOL_PCT * price
    width = lv.CLUSTER_WIDTH_PCT * price
    prices = sorted(p.price for p in pools)
    # pools inside the window [prices[j], prices[j] + width]
    counts = [bisect.bisect_right(prices, x + width) - j for j, x in enumerate(prices)]
    for p in pools:
        b = {}
        if any(q.tf != p.tf and abs(q.price - p.price) <= tol for q in pools):
            b["multi_tf_confluence"] = 12
        if p.untouched:
            b["untouched"] = 10
        lo_j, hi_j = bisect.bisect_left(prices, p.price - width), bisect.bisect_right(prices, p.price)
        if max(counts[lo_j:hi_j], default=0) >= 3:
            b["cluster"] = 8
            if "Cluster" not in p.labels:
                p.labels.append("Cluster")
        if any(z.low <= p.price <= z.high for z in htf_zones):
            b["htf_pda"] = 5
        p.bonus = b
        p.lps = lps_score(p.tf, p.mult, sum(b.values()))
        p.importance = importance(p.lps)


def build_registry(ctx: dict, htf_zones: list[Zone]) -> list[Pool]:
    price, bars = ctx["price"], ctx["bars"]
    pools: list[Pool] = []
    for tf in ("D1", "H4", "H1"):
        pools += dedupe(discover_tf(tf, bars.get(tf, []), ctx), price)
    classify(pools, bars, price, ctx["session_atr"], ctx["now"])
    score(pools, price, htf_zones)
    pools.sort(key=lambda p: (-TF_RANK[p.tf], ORDER.index(min(p.kinds, key=ORDER.index)), p.avail_t, p.price))
    for n, p in enumerate(pools, start=1):
        p.id = f"LIQ_{n:03d}"
    return pools

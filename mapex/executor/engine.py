"""GEM2 LTF Execution Engine: Preflight -> Module 5 -> P1 -> P2 (Module 6 init) -> P3 (Module 6) -> P4 (Module 7)
-> Module 8 -> scoring. Consumes the GEM1 Strategic Map, never re-derives it. Only CONFIRMED (100/100) yields an
order plan. Pure function of (map, closed bars, quote, states, now): the caller persists states and events.
"""

from __future__ import annotations

import bisect
import hashlib
from dataclasses import dataclass, field

from mapex.core import levels as lv
from mapex.core import primitives as pr
from mapex.core.primitives import Bar
from mapex.core.timeutil import (
    TF_SECONDS,
    fmt_ny,
    in_silver_bullet,
    killzone,
    ny,
    ny_at,
    ny_midnight,
)
from mapex.executor.state import ZoneState
from mapex.executor.stoploss import NoSetup, fiduciary_sl
from mapex.executor.targets import ladder

LIMITS = {"M1": 300, "M5": 300, "M15": 400, "D1": 60}
RAID_NEAR_SESSION_ATR = 0.1  # pool within 0.1 x session_ATR of the zone edge (or inside)
DISP_WINDOW = 3  # Module 6: displacement within 3 bars of the sweep
MSS_WINDOW_BARS = 5  # sweep-TF bars after the raid for the M5/M1 body-close MSS (DECISIONS D-20)
FOOTPRINT_BARS = 3  # P4.2
FOOTPRINT_BODY_MEDIAN = 1.5
RETEST_BARS = 15  # P4.3
DEFENSE_BODY = 0.60  # P4.4
DEFENSE_WICK = 0.35
STALE_SESSIONS = 3
OPPOSING_LPS = 80
CONFIRM_MAX_LAG_S = 120  # a CONFIRMED older than this (catch-up after downtime) is never traded
TRAP_KINDS = {"EQH", "EQL", "SESSION_H", "SESSION_L", "PDH", "PDL"}
# GEM2 LAYER 4 (ranked above the Format A template text): 3001 HTF structural break, 3002 D1 close past root
LEVEL3_REASON = {3001: "D1 structural break vs. bias", 3002: "D1 body close past THESIS_ROOT"}


@dataclass
class ExecInput:
    symbol: str
    now: float
    map: dict | None
    meta: dict
    map_valid: bool
    bars: dict[str, list[Bar]]
    bid: float | None = None
    ask: float | None = None
    market_open: bool = True
    data_ok: bool = True
    tick: float = 0.01
    decimals: int = 2
    sl_safety_buffer: float = 0.3
    map_max_age_h: float = 6.0


@dataclass
class Plan:
    setup_key: str
    symbol: str
    side: str  # buy | sell
    chain: str
    zone_key: str
    decision_price: float
    sl: float
    tp1: float
    tp2: float | None
    tp3: float | None
    tp_server: float
    risk: float
    r_tp1: float
    r_tp2: float | None
    management_level: float | None
    anchor_type: str
    spread: float
    root_type: str
    root_price: float
    root_lps: int
    sweep_count: int
    sweep_type: str
    zone_label: str
    session: str
    model: str
    strategy_used: list[str]
    confirmed_at: int
    format_b: dict = field(default_factory=dict)


@dataclass
class Decision:
    zone_key: str | None
    chain: str | None
    output: str  # WAIT CONFIRMED RESET MONITOR NO-SETUP INVALIDATED (WATCH = internal progress)
    reason: str
    scores: tuple[int, int, int, int] = (0, 0, 0, 0)
    state: str = ""
    error_code: int | None = None
    plan: Plan | None = None
    data: dict | None = None
    ts: int = 0


@dataclass
class ExecResult:
    decisions: list[Decision] = field(default_factory=list)
    plan: Plan | None = None
    states: dict[str, ZoneState] = field(default_factory=dict)
    rerun_mapper: bool = False
    waits: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------- shared context per tick

@dataclass
class LtfPool:
    level: float
    side: str
    kind: str  # STH/STL, EQH/EQL, SESSION_H/L, PDH/PDL
    avail_t: int
    touches: int = 1


class Ctx:
    def __init__(self, inp: ExecInput):
        self.inp = inp
        self.now = inp.now
        self.map = inp.map
        self.bars = {tf: pr.closed_bars(inp.bars.get(tf, []), tf, inp.now)[-n:] for tf, n in LIMITS.items()}
        self.s_atr = float(inp.map["session_context"]["session_atr"])
        self.registry = {p["id"]: p for p in inp.map["liquidity_registry"]}
        self.struct_hash = inp.meta.get("struct_hash")
        self._atrs: dict[str, list[float]] = {}
        self._pools: list[LtfPool] | None = None
        self.price = inp.bid

    def atrs(self, tf: str) -> list[float]:
        if tf not in self._atrs:
            self._atrs[tf] = pr.atr_series(self.bars[tf])
        return self._atrs[tf]

    def ltf_pools(self) -> list[LtfPool]:
        """M5/M15 swings, EQH/EQL, session highs/lows (incl. NY Lunch) and PDH/PDL."""
        if self._pools is not None:
            return self._pools
        out: list[LtfPool] = []
        price = self.price or self.bars["M1"][-1].c
        for tf in ("M5", "M15"):
            b = self.bars[tf]
            for side, idx in (("high", pr.swing_highs(b)), ("low", pr.swing_lows(b))):
                val = (lambda i, b=b: b[i].h) if side == "high" else (lambda i, b=b: b[i].l)
                for i in idx:
                    out.append(LtfPool(val(i), side, "STH" if side == "high" else "STL", b[i + 1].t + TF_SECONDS[tf]))
                for g in lv.group_equal([(val(i), i) for i in idx], price):
                    if len(g) >= 2:
                        at = max(b[i + 1].t for _, i in g) + TF_SECONDS[tf]
                        out.append(LtfPool(sum(x[0] for x in g) / len(g), side, "EQH" if side == "high" else "EQL",
                                           at, len(g)))
        for s in lv.completed_sessions(self.bars["M15"], self.now, days=2):
            out.append(LtfPool(s.high, "high", "SESSION_H", s.end))
            out.append(LtfPool(s.low, "low", "SESSION_L", s.end))
        d1 = self.bars["D1"]
        if d1:
            out.append(LtfPool(d1[-1].h, "high", "PDH", d1[-1].t + 86400))
            out.append(LtfPool(d1[-1].l, "low", "PDL", d1[-1].t + 86400))
        self._pools = out
        return out

    def taken(self, level: float, side: str, since: int, until: int) -> bool:
        """Was the level reached by any M5 bar opening in [since, until)?"""
        m5 = self.bars["M5"]
        i = bisect.bisect_left(m5, since, key=lambda b: b.t)
        for b in m5[i:]:
            if b.t >= until:
                break
            if (side == "low" and b.l <= level) or (side == "high" and b.h >= level):
                return True
        return False


# ---------------------------------------------------------------- helpers

def _f(x) -> float | None:
    return None if x is None else float(x)


def zone_dist(level: float, lo: float, hi: float) -> float:
    return 0.0 if lo <= level <= hi else min(abs(level - lo), abs(level - hi))


def preflight(inp: ExecInput) -> str | None:
    m = inp.map
    if not inp.map_valid or m is None:
        return "no_valid_strategic_map"
    if not m.get("active_causal_chain", {}).get("root_liquidity_id"):
        return "active_causal_chain_missing"
    if inp.now - inp.meta.get("created_at", inp.now) > inp.map_max_age_h * 3600:
        return "strategic_map_stale"
    if inp.bid is None or inp.ask is None:
        return "live_quote_stale"
    if not inp.market_open:
        return "market_closed"
    if not inp.data_ok:
        return "candle_gap"
    return None


def setup_key(symbol: str, zone_key: str, raid_t: int, chain: str) -> str:
    h = hashlib.sha256(zone_key.encode()).hexdigest()[:6]
    return f"MPX-{symbol[:3]}-{ny(raid_t).strftime('%m%d')}-{h}-{raid_t}-{chain[-1]}"


# ---------------------------------------------------------------- the engine

def evaluate(inp: ExecInput, states: dict[str, ZoneState]) -> ExecResult:
    res = ExecResult(states={k: v.copy() for k, v in states.items()})
    reason = preflight(inp)
    if reason:
        res.decisions.append(Decision(None, None, "NO-SETUP", reason, error_code=1001, ts=int(inp.now)))
        return res
    ctx = Ctx(inp)
    zones = [kz for kz in inp.map["key_zones"] if kz["id"] in ("CHAIN_A", "CHAIN_B")]
    confirmed: list[Decision] = []
    for kz in zones:
        key = inp.meta["zone_keys"][kz["id"]]
        st = res.states.get(key)
        if st is None:
            st = ZoneState(key, inp.symbol, kz["id"], kz["direction"], first_seen=int(inp.now))
            st.ctx["seen"] = {tf: (ctx.bars[tf][-1].t if ctx.bars[tf] else 0) for tf in ("M1", "M5", "M15")}
            res.states[key] = st
        st.chain = kz["id"]
        for d in step_zone(st, kz, ctx):
            res.decisions.append(d)
            if d.output == "CONFIRMED":
                confirmed.append(d)
            if d.output == "INVALIDATED":
                res.rerun_mapper = True
            if d.output == "WAIT":
                res.waits[kz["id"]] = d.reason
    if confirmed:
        res.plan = alpha_pick([d.plan for d in confirmed])
    return res


def alpha_pick(plans: list[Plan]) -> Plan:
    """LAYER 10: higher TP1 R-multiple, then higher root LPS, then CHAIN_A. One trade per tick."""
    return sorted(plans, key=lambda p: (-round(p.r_tp1, 6), -p.root_lps, p.chain))[0]


def thesis_check(st: ZoneState, kz: dict, ctx: Ctx) -> tuple[str, int | None]:
    """Module 5 preservation P1/P2 on D1 closes after the zone was first seen (Level 3 = INVALIDATED)."""
    buy = kz["direction"] == "buy"
    root = _f(ctx.registry[kz["generating_liquidity_id"]]["price_level"])
    dr = ctx.inp.meta.get("dealing_range", {})
    struct = dr.get("low") if buy else dr.get("high")
    for b in ctx.bars["D1"]:
        if b.t + 86400 <= st.first_seen:
            continue
        if (buy and b.c < root) or (not buy and b.c > root):
            return "INVALIDATED", 3002  # D1 body close past THESIS_ROOT
        if struct is not None and ((buy and b.c < struct) or (not buy and b.c > struct)):
            return "INVALIDATED", 3001  # D1 structural break against bias
    return ("PRESERVED" if st.sweep_count else "ACTIVE"), None


def step_zone(st: ZoneState, kz: dict, ctx: Ctx) -> list[Decision]:
    out: list[Decision] = []
    now = int(ctx.now)
    if st.state in ("DEAD", "CONFIRMED"):
        return out
    status, code = thesis_check(st, kz, ctx)
    if status == "INVALIDATED":
        st.state, st.thesis_status = "DEAD", "INVALIDATED"
        st.clear_sequence()
        st.state = "DEAD"
        out.append(Decision(st.zone_key, st.chain, "INVALIDATED", f"level3_break_{code}", state="DEAD",
                            error_code=code, ts=now, data=format_a(st, kz, ctx, code)))
        return out
    if st.state == "MONITOR":
        if st.monitor_hash == ctx.struct_hash:
            return out  # no WAIT until a fresh map re-creates the zone
        st.state, st.sweep_count, st.monitor_reason = "WATCH", 0, None  # fresh GEM1 analysis
        st.ctx = {"seen": st.ctx.get("seen", {})}
    st.thesis_status = status
    # Level 2 checks that do not need new bars
    buy = kz["direction"] == "buy"
    if any(z["direction"] != kz["direction"] and z["generating_lps"] >= OPPOSING_LPS for z in ctx.map["key_zones"]):
        return [monitor(st, "opposing_htf_pda_lps80", now, ctx.struct_hash)]
    if not st.ctx.get("anchor_reached") and lv.sessions_between(st.first_seen, now) >= STALE_SESSIONS:
        st.thesis_status = "STALE"
        return [monitor(st, "stale_3_sessions_without_anchor", now, ctx.struct_hash)]
    for ev_t, tf, i in new_events(st, ctx):
        d = on_bar(st, kz, ctx, tf, i, ev_t, buy)
        if d is not None:
            out.append(d)
            if d.output in ("MONITOR", "CONFIRMED"):
                break
    if st.state == "RETURN" and (not out or out[-1].output not in ("CONFIRMED", "MONITOR")):
        fvg = st.ctx["fvg"]
        cond = f"M1 body close {'ABOVE' if buy else 'BELOW'} {fvg['ce']:.{ctx.inp.decimals}f}"
        p1, p2 = score_p1(st, kz, ctx, buy)[0], score_p2(st, kz, ctx, buy, now)[0]
        if p1 == 25 and p2 == 25:
            out.append(Decision(st.zone_key, st.chain, "WAIT", cond, (25, 25, 25, 0), "RETURN", ts=now))
    return out


def new_events(st: ZoneState, ctx: Ctx) -> list[tuple[int, str, int]]:
    """Closed M1/M5/M15 bars not processed yet, in close-time order (M15 before M5 before M1 at a tie)."""
    seen = st.ctx.setdefault("seen", {})
    evs = []
    for rank, tf in enumerate(("M15", "M5", "M1")):
        bars = ctx.bars[tf]
        last = seen.get(tf, 0)
        start = bisect.bisect_right(bars, last, key=lambda b: b.t)
        for i in range(start, len(bars)):
            evs.append((bars[i].t + TF_SECONDS[tf], rank, tf, i))
    evs.sort()
    return [(t, tf, i) for t, _, tf, i in evs]


def monitor(st: ZoneState, reason: str, now: int, struct_hash: str | None, scores=(0, 0, 0, 0)) -> Decision:
    """Module 8 Level 2: the zone produces nothing until a map with a different structure is published."""
    st.clear_sequence()
    st.state, st.monitor_reason, st.monitor_hash = "MONITOR", reason, struct_hash
    return Decision(st.zone_key, st.chain, "MONITOR", reason, scores, "MONITOR", ts=now)


def noise(st: ZoneState, kz: dict, ctx: Ctx, reason: str, t: int) -> Decision:
    """Module 8 Level 1. Mandatory RESET needs sweep_count <= 2, thesis intact (checked upstream), D1 aligned
    and THESIS_ANCHOR not violated; otherwise Level 2 MONITOR."""
    if st.sweep_count <= 2 and not st.ctx.get("anchor_violated"):
        data = format_c(st, kz, ctx, reason)
        st.clear_sequence()
        st.thesis_status = "PRESERVED"
        return Decision(st.zone_key, st.chain, "RESET", reason, (25, 25, 25, 0), "WATCH", ts=t, data=data)
    d = monitor(st, f"{reason}; sweep_count={st.sweep_count}", t, ctx.struct_hash)
    d.data = {"status": "MONITOR", "caution": "Re-run GEM1 for updated liquidity analysis",
              "sweep_count": st.sweep_count}
    return d


def on_bar(st: ZoneState, kz: dict, ctx: Ctx, tf: str, i: int, t: int, buy: bool) -> Decision | None:
    st.ctx["seen"][tf] = ctx.bars[tf][i].t
    bar = ctx.bars[tf][i]
    lo, hi = float(kz["zone_low"]), float(kz["zone_high"])
    if tf == "M15":
        if (buy and bar.l <= hi) or (not buy and bar.h >= lo):
            st.ctx["anchor_reached"] = True
        if (buy and bar.c < lo) or (not buy and bar.c > hi):
            st.ctx["anchor_violated"] = True
        if st.state in ("RAID", "SHIFT", "GAP", "RETURN") and m15_contradiction(st, ctx, i, buy):
            return monitor(st, "m15_structure_contradicts_thesis", t, ctx.struct_hash)
    if st.state == "WATCH" and tf in ("M5", "M15"):
        return watch(st, kz, ctx, tf, i, t, buy)
    if st.state == "RAID" and tf == st.ctx["raid"]["tf"]:
        return raid(st, kz, ctx, i, t, buy)
    if st.state == "SHIFT" and tf == "M1":
        return shift(st, kz, ctx, i, t, buy)
    if st.state == "GAP" and tf == "M1":
        return gap(st, kz, ctx, i, t, buy)
    if st.state == "RETURN" and tf == "M1":
        return retest(st, kz, ctx, i, t, buy)
    return None


# ---------------------------------------------------------------- P3 state 1 — RAID (+ Module 6)

def watch(st, kz, ctx: Ctx, tf, i, t, buy) -> Decision | None:
    b = ctx.bars[tf][i]
    lo, hi = float(kz["zone_low"]), float(kz["zone_high"])
    near = RAID_NEAR_SESSION_ATR * ctx.s_atr
    if (buy and b.l >= hi + near) or (not buy and b.h <= lo - near):
        return None  # price nowhere near the zone
    side = "low" if buy else "high"
    done = st.ctx.setdefault("swept", [])  # a sweep seen on M5 is not counted again on its M15 bar
    swept = []
    for p in ctx.ltf_pools():
        if p.side != side or p.avail_t > b.t or zone_dist(p.level, lo, hi) > near:
            continue
        if any(abs(p.level - x) < 1e-9 for x in done):
            continue
        if (buy and b.l < p.level) or (not buy and b.h > p.level):
            if not ctx.taken(p.level, side, p.avail_t, b.t):
                swept.append(p)
    if not swept:
        return None
    pool = min(swept, key=lambda p: p.level) if buy else max(swept, key=lambda p: p.level)
    done.extend(p.level for p in swept)
    del done[:-50]
    body = (b.c < pool.level) if buy else (b.c > pool.level)
    st.sweep_count += 1
    st.last_sweep_level = pool.level
    st.last_sweep_type = "BODY_CLOSE" if body else "WICK_ONLY"
    st.ctx["anchor_reached"] = True
    st.ctx["raid"] = {"t": b.t, "tf": tf, "idx_t": b.t, "level": pool.level, "kind": pool.kind,
                      "touches": pool.touches, "extreme": b.l if buy else b.h, "close_t": b.t + TF_SECONDS[tf]}
    if not body:
        # Type 11 (wick only): never displacement-generating -> Module 8 Level 1 (Multi-Sweep Protocol)
        return noise(st, kz, ctx, f"{ordinal(st.sweep_count)} sweep, wick only (Type 11)", t)
    st.state = "RAID"
    return Decision(st.zone_key, st.chain, "WATCH", f"raid #{st.sweep_count} BODY_CLOSE @ {pool.level}",
                    state="RAID", ts=t)


def ordinal(n: int) -> str:
    return {1: "First", 2: "Second", 3: "Third"}.get(n, f"#{n}")


def raid(st, kz, ctx: Ctx, i, t, buy) -> Decision | None:
    r = st.ctx["raid"]
    bars = ctx.bars[r["tf"]]
    ri = bisect.bisect_left(bars, r["t"], key=lambda b: b.t)
    j = i - ri
    if j == 1:
        c2 = bars[i]
        ok = (c2.c > r["level"] and c2.bull) if buy else (c2.c < r["level"] and c2.bear)
        if not ok:
            return noise(st, kz, ctx, "Type 7 candle 2 did not close back inside", t)
        st.ctx["type7"] = True
    m = i - 1
    if ri + 1 <= m <= ri + DISP_WINDOW:
        atrs = ctx.atrs(r["tf"])
        if pr.is_displacement(bars, m, atrs[m]) and (bars[m].bull if buy else bars[m].bear):
            st.displacement_detected = True
            st.ctx["disp"] = {"tf": r["tf"], "t": bars[m].t}
            st.state = "SHIFT"
            st.ctx["mss_level"] = mss_level(ctx, r, buy)
            return shift_scan(st, kz, ctx, t, buy)
    if j >= DISP_WINDOW + 1:
        return noise(st, kz, ctx, f"{ordinal(st.sweep_count)} sweep, no displacement", t)
    return None


def mss_level(ctx: Ctx, r: dict, buy: bool) -> float:
    """The last M5 swing of the leg into the raid (swing high for a buy), confirmed by the raid close."""
    m5 = ctx.bars["M5"]
    upto = bisect.bisect_right(m5, r["close_t"] - TF_SECONDS["M5"], key=lambda b: b.t)
    seg = m5[:upto]
    idx = pr.swing_highs(seg) if buy else pr.swing_lows(seg)
    idx = [k for k in idx if seg[k].t < r["t"]]
    if idx:
        return seg[idx[-1]].h if buy else seg[idx[-1]].l
    tail = seg[-12:] or m5[-12:]
    return max(b.h for b in tail) if buy else min(b.l for b in tail)


# ---------------------------------------------------------------- P3 state 2 — SHIFT (MSS)

def shift(st, kz, ctx, i, t, buy):
    return shift_scan(st, kz, ctx, t, buy)


def shift_scan(st, kz, ctx: Ctx, t: int, buy: bool) -> Decision | None:
    """First M1 body close beyond the MSS level after the raid (an M5 body close implies one). A wick-only break
    is a fake MSS (Type 11) -> Level 1."""
    r = st.ctx["raid"]
    level = st.ctx["mss_level"]
    m1 = ctx.bars["M1"]
    start = bisect.bisect_left(m1, r["close_t"], key=lambda b: b.t)
    for k in range(start, len(m1)):
        b = m1[k]
        if b.t + 60 > t:
            break
        if (buy and b.c > level) or (not buy and b.c < level):
            st.ctx["mss"] = {"t": b.t, "level": level}
            st.state = "GAP"
            return gap_eval(st, kz, ctx, t, buy)
        if (buy and b.h > level) or (not buy and b.l < level):
            st.ctx["wick_break"] = True
    if t >= r["close_t"] + MSS_WINDOW_BARS * TF_SECONDS[r["tf"]]:
        why = "Fake MSS (wick-only break)" if st.ctx.get("wick_break") else "no MSS body close"
        return noise(st, kz, ctx, why, t)
    return None


# ---------------------------------------------------------------- P3 state 3 — GAP (+ P4.2 footprint)

def gap(st, kz, ctx, i, t, buy):
    return gap_eval(st, kz, ctx, t, buy)


def gap_eval(st, kz, ctx: Ctx, t: int, buy: bool) -> Decision | None:
    m1 = ctx.bars["M1"]
    b0 = bisect.bisect_left(m1, st.ctx["mss"]["t"], key=lambda b: b.t)
    if b0 + FOOTPRINT_BARS >= len(m1) or m1[b0 + FOOTPRINT_BARS].t + 60 > t:
        return None  # the FVG of the 3rd footprint bar is confirmed by the next bar
    base = m1[max(0, b0 - 20):b0]
    med = pr.median_body(base)
    window = range(b0, b0 + FOOTPRINT_BARS)
    fvgs = [m for m in window if pr.fvg_at(m1, m, "buy" if buy else "sell")]
    body_ok = any(m1[m].body >= FOOTPRINT_BODY_MEDIAN * med and (m1[m].bull if buy else m1[m].bear)
                  for m in window)
    if not fvgs or not body_ok:
        return noise(st, kz, ctx, "no_displacement_print", t)
    zones = [pr.Zone("FVG", "buy" if buy else "sell", *(
        (m1[m - 1].h, m1[m + 1].l) if buy else (m1[m + 1].h, m1[m - 1].l)), 0, m1[m].t, m) for m in fvgs]
    for z in zones:
        z.anchor = z.ce
    z = max(zones, key=lambda z: (z.high - z.low, z.formed_at))
    recent = m1[max(0, b0 - 60):]
    bprs = pr.bprs(recent, pr.fvgs(recent))
    if pr.inside_bpr(z, [p for p in bprs if p.formed_at < z.formed_at]):
        return noise(st, kz, ctx, "FVG inside existing BPR (invalid anchor)", t)
    st.ctx["footprint"] = True
    st.ctx["fvg"] = {"low": z.low, "high": z.high, "ce": z.ce, "t": z.formed_at}
    st.state = "RETURN"
    return retest_scan(st, kz, ctx, t, buy)


# ---------------------------------------------------------------- P3 state 4 — RETURN + P4.3 / P4.4

def retest(st, kz, ctx, i, t, buy):
    return retest_scan(st, kz, ctx, t, buy)


def defended(b: Bar, ce: float, buy: bool) -> bool:
    if b.range <= 0 or b.body < DEFENSE_BODY * b.range:
        return False
    wick = (b.body_bot - b.l) if buy else (b.h - b.body_top)
    if wick > DEFENSE_WICK * b.range:
        return False
    return (b.bull and b.c > ce) if buy else (b.bear and b.c < ce)


def retest_scan(st, kz, ctx: Ctx, t: int, buy: bool) -> Decision | None:
    m1 = ctx.bars["M1"]
    f = st.ctx["fvg"]
    ce = f["ce"]
    b0 = bisect.bisect_left(m1, st.ctx["mss"]["t"], key=lambda b: b.t)
    start = bisect.bisect_right(m1, max(f["t"] + 60, st.ctx.get("retest_from", 0)), key=lambda b: b.t)
    for k in range(start, len(m1)):
        b = m1[k]
        if b.t + 60 > t:
            break
        st.ctx["retest_from"] = b.t
        if (buy and b.l < f["low"]) or (not buy and b.h > f["high"]):
            return noise(st, kz, ctx, "NOT_BREAKAWAY_RETRY (FVG fully closed)", t)
        if st.ctx.get("retest_pending"):
            if defended(b, ce, buy):
                return confirm(st, kz, ctx, b, t, buy)
            return noise(st, kz, ctx, "anchor_not_defended", t)
        touched = (b.l <= ce) if buy else (b.h >= ce)
        if touched:
            if k - b0 > RETEST_BARS:
                return noise(st, kz, ctx, "no_retest_window", t)
            if (buy and b.c < ce) or (not buy and b.c > ce):
                return noise(st, kz, ctx, "anchor_not_defended", t)
            if defended(b, ce, buy):
                return confirm(st, kz, ctx, b, t, buy)
            st.ctx["retest_pending"] = True
            continue
        if k - b0 >= RETEST_BARS:
            return noise(st, kz, ctx, "no_retest_window", t)
    return None


# ---------------------------------------------------------------- P1 / P2 scoring

def m15_contradiction(st, ctx: Ctx, i: int, buy: bool) -> bool:
    """P1: an M15 BOS against the bias with displacement after the current raid began = Level 2."""
    r = st.ctx.get("raid")
    if not r:
        return False
    m15 = ctx.bars["M15"][: i + 1]
    atrs = ctx.atrs("M15")
    for brk in pr.structure_breaks(m15):
        if m15[brk.idx].t <= r["t"] or brk.direction == ("buy" if buy else "sell"):
            continue
        if any(pr.is_displacement(m15, j, atrs[j]) for j in range(max(0, brk.idx - 2), min(brk.idx + 1, i))):
            return True
    return False


def score_p1(st, kz, ctx: Ctx, buy: bool) -> tuple[int, dict]:
    ok_bias = ctx.map.get("strategic_bias") == kz["direction"]
    ok_thesis = st.thesis_status in ("ACTIVE", "PRESERVED")
    sub = {"bias_set": ok_bias, "thesis_intact": ok_thesis, "m15_no_contradiction": True, "smt": "NO SMT VISIBLE"}
    return (25 if ok_bias and ok_thesis else 0), sub


def judas_threshold(ctx: Ctx, at: float, buy: bool) -> float | None:
    m1 = ctx.bars["M1"]
    m15 = ctx.bars["M15"]
    mid = lv.day_open_at(m15, ny_midnight(at)) or lv.day_open_at(m1, ny_midnight(at))
    t830 = ny_at(at, 8, 30)
    if at < t830 or mid is None:
        return mid
    o830 = lv.day_open_at(m1, t830) or lv.day_open_at(m15, t830)
    if o830 is None:
        return mid
    return min(mid, o830) if buy else max(mid, o830)


def score_p2(st, kz, ctx: Ctx, buy: bool, at: float) -> tuple[int, dict]:
    kzone = killzone(at)
    r = st.ctx.get("raid") or {}
    ext = r.get("extreme")
    thr = judas_threshold(ctx, at, buy)
    judas = ext is not None and thr is not None and (ext < thr if buy else ext > thr)
    if not judas and kzone == "New York" and ext is not None:
        london = [s for s in lv.completed_sessions(ctx.bars["M15"], at, days=1) if s.name == "London"]
        if london:
            judas = ext < london[-1].low if buy else ext > london[-1].high
    lo, hi = float(kz["zone_low"]), float(kz["zone_high"])
    dols = [_f(ctx.map.get("final_lrlr_objective")), _f(kz.get("tp2"))]
    dol = any(d is not None and (d > hi if buy else d < lo) for d in dols)
    sub = {"killzone": kzone, "judas": judas, "judas_threshold": thr, "dol_alignment": dol}
    n = sum([kzone is not None, judas, dol])
    return (25 if n == 3 else round(25 * n / 3)), sub


# ---------------------------------------------------------------- CONFIRMED path (P4 + SL + TP)

def confirm(st, kz, ctx: Ctx, bar: Bar, t: int, buy: bool) -> Decision:
    inp = ctx.inp
    p1, p1sub = score_p1(st, kz, ctx, buy)
    p2, p2sub = score_p2(st, kz, ctx, buy, t)
    p3, p4 = 25, 25  # raid+MSS+FVG+return confirmed; P4.1-P4.4 all passed to reach here
    scores = (p1, p2, p3, p4)
    raid_t = st.ctx["raid"]["t"]
    key = setup_key(inp.symbol, st.zone_key, raid_t, st.chain)

    def no_setup(reason: str) -> Decision:
        data = format_a(st, kz, ctx, 1001, reason, scores)
        st.clear_sequence()
        return Decision(st.zone_key, st.chain, "NO-SETUP", reason, scores, "WATCH", error_code=1001, ts=t, data=data)

    if sum(scores) != 100:
        failed = [k for k, v in {"p1": p1, "p2": p2}.items() if v != 25]
        return no_setup(f"score_{sum(scores)}_{'_'.join(failed)}: {p2sub}")
    if ctx.now - t > CONFIRM_MAX_LAG_S:
        return no_setup("confirmation_too_old (catch-up)")
    entry = inp.ask if buy else inp.bid
    spread = inp.ask - inp.bid
    r = st.ctx["raid"]
    raid_bars = ctx.bars[r["tf"]]
    raid_bar = raid_bars[bisect.bisect_left(raid_bars, r["t"], key=lambda b: b.t)]
    m1 = ctx.bars["M1"]
    seq = [b for b in m1 if b.t >= r["t"] and b.t <= bar.t]
    extreme = min([r["extreme"]] + [b.l for b in seq]) if buy else max([r["extreme"]] + [b.h for b in seq])
    disp_extreme = max(b.h for b in seq) if buy else min(b.l for b in seq)
    atr_m5 = pr.atr(ctx.bars["M5"], 14)
    trap_tol = lv.level_tol(entry, atr_m5, inp.tick)
    live_reg = [p for p in ctx.map["liquidity_registry"] if p["status"] in ("UNTOUCHED", "BEING_TARGETED")]
    traps = [float(p["price_level"]) for p in live_reg if p["type"] in TRAP_KINDS]
    ltf_live = [p for p in ctx.ltf_pools() if not ctx.taken(p.level, p.side, p.avail_t, bar.t + 60)]
    traps += [p.level for p in ltf_live if p.kind in {"EQH", "EQL", "SESSION_H", "SESSION_L", "PDH", "PDL"}]
    try:
        sl = fiduciary_sl(buy, raid_bar, extreme, entry, spread, inp.tick, inp.sl_safety_buffer, atr_m5, traps,
                          trap_tol, ctx.s_atr)
        objectives = [(float(p["price_level"]), f"{p['type']} {p['id']}") for p in live_reg]
        objectives += [(p.level, f"LTF {p.kind}") for p in ltf_live if p.side == ("high" if buy else "low")]
        objectives += opposing_edges(ctx, buy)
        lad = ladder(buy, entry, sl.risk, spread, ctx.s_atr, objectives, extreme, disp_extreme,
                     _f(ctx.map.get("final_lrlr_objective")))
    except NoSetup as exc:
        return no_setup(exc.reason)
    root = ctx.registry[kz["generating_liquidity_id"]]
    sb = in_silver_bullet(t)
    labels = ["Displacement + Return to Origin (DRO)"]
    if r["kind"] in ("EQH", "EQL", "SESSION_H", "SESSION_L", "PDH", "PDL"):
        labels.insert(0, "CRT (Turtle Soup) Liquidity Sweep")
    if r["kind"] in ("EQH", "EQL"):
        labels.append("Magnetized Liquidity Cluster (MLC)")
    if kz["zone_type"] == "OB":
        labels.append("Order Block Mitigation (OBM)")
    if sb:
        labels.append("SILVER_BULLET")
    plan = Plan(
        setup_key=key, symbol=inp.symbol, side=kz["direction"], chain=st.chain, zone_key=st.zone_key,
        decision_price=entry, sl=round(sl.value, inp.decimals), tp1=round(lad.tp1, inp.decimals),
        tp2=None if lad.tp2 is None else round(lad.tp2, inp.decimals),
        tp3=None if lad.tp3 is None else round(lad.tp3, inp.decimals),
        tp_server=round(lad.server_tp, inp.decimals), risk=sl.risk, r_tp1=lad.tp1_r, r_tp2=lad.tp2_r,
        management_level=lad.management_level, anchor_type=sl.anchor_type, spread=spread, root_type=root["type"],
        root_price=float(root["price_level"]), root_lps=int(root["lps"]), sweep_count=st.sweep_count,
        sweep_type=st.last_sweep_type or "BODY_CLOSE", zone_label=f"{kz['id']} {kz['timeframe']} {kz['zone_type']} @ "
                                                                   f"{kz['anchor_price']}",
        session=f"{p2sub['killzone']} {fmt_ny(t)}", model="SILVER_BULLET" if sb else kz["suggested_pearl"],
        strategy_used=labels, confirmed_at=t)
    plan.format_b = format_b(plan, st, kz, ctx, sl, lad, scores, p1sub, p2sub)
    st.state = "CONFIRMED"
    st.sweep_count = 0  # Module 6: cleared for this POI upon entry confirmation
    return Decision(st.zone_key, st.chain, "CONFIRMED", "A+ 100/100", scores, "CONFIRMED", plan=plan, ts=t,
                    data=plan.format_b)


def opposing_edges(ctx: Ctx, buy: bool) -> list[tuple[float, str]]:
    """Near edges of unmitigated opposing M5/M15 FVGs and OBs (Layer 5 objectives)."""
    out = []
    for tf in ("M5", "M15"):
        bars = ctx.bars[tf]
        atrs = ctx.atrs(tf)
        opp = "sell" if buy else "buy"
        zones = [z for z in pr.fvgs(bars, tf) if z.direction == opp]
        zones += [z for z in pr.order_blocks(bars, atrs, pr.structure_breaks(bars), tf) if z.direction == opp]
        for z in zones:
            pr.mark_mitigation(z, bars)
            if z.mitigated_at is None:
                out.append((z.low if buy else z.high, f"{tf} opposing {z.kind}"))
    return out


# ---------------------------------------------------------------- LAYER 11 output formats

def format_a(st, kz, ctx: Ctx, code: int, reason: str | None = None, scores=(0, 0, 0, 0)) -> dict:
    root = ctx.registry.get(kz["generating_liquidity_id"], {})
    return {
        "status": "no-setup", "diagnosed_trap": "NONE",
        "reason": reason or LEVEL3_REASON.get(code, ""),
        "error_code": code, "user_confirmation": None, "sniper_entry_precision": 0.0,
        "contextual_confidence_score": {"p1_bias": scores[0], "p2_keylevel": scores[1], "p3_ltf_entry": scores[2],
                                        "p4_confirmation": scores[3], "total": sum(scores)},
        "thesis_anchor": {"thesis_status": "INVALIDATED" if code in (3001, 3002) else "ACTIVE",
                          "thesis_root": root.get("price_level"), "thesis_root_lps": root.get("lps", 0),
                          "thesis_anchor_pda": kz["zone_label"], "thesis_invalidation_level": root.get("price_level"),
                          "sweep_count_at_evaluation": st.sweep_count, "root_liquidity_id": root.get("id")},
        "execution_notes": {"ocr_status": "N/A", "live_ask": ctx.inp.ask, "live_bid": ctx.inp.bid,
                            "live_spread_points": None if ctx.inp.ask is None else ctx.inp.ask - ctx.inp.bid,
                            "audit_log_id": "N/A"},
    }


def format_c(st, kz, ctx: Ctx, reason: str) -> dict:
    root = ctx.registry.get(kz["generating_liquidity_id"], {})
    buy = kz["direction"] == "buy"
    return {
        "status": "RESET", "reason": reason, "thesis_status": "PRESERVED", "thesis_root": root.get("price_level"),
        "thesis_root_type": root.get("type"), "thesis_root_lps": root.get("lps", 0),
        "thesis_anchor_pda": kz["zone_label"], "sweep_count": st.sweep_count,
        "last_sweep_type": st.last_sweep_type, "displacement_detected": st.displacement_detected,
        "watching_for": f"Price reloads to {kz['zone_low'] if buy else kz['zone_high']} for the next sweep",
        "thesis_invalidation_level": root.get("price_level"), "next_wait_condition": None,
        "level_routing": "Module 8 Level 1 — LTF NOISE", "user_confirmation": None, "sniper_entry_precision": 0.0,
        "contextual_confidence_score": {"p1_bias": 25, "p2_keylevel": 25, "p3_ltf_entry": 25, "p4_confirmation": 0,
                                        "total": 75,
                                        "note": "P4 unconfirmed; do not add to total until body close is observed"},
        "execution_notes": {"ocr_status": "OK", "live_ask": ctx.inp.ask, "live_bid": ctx.inp.bid,
                            "live_spread_points": ctx.inp.ask - ctx.inp.bid, "audit_log_id": str(int(ctx.now))},
    }


def format_b(plan: Plan, st, kz, ctx: Ctx, sl, lad, scores, p1sub, p2sub) -> dict:
    d = ctx.inp.decimals

    def fx(x):
        return None if x is None else f"{x:.{d}f}"

    buy = plan.side == "buy"
    f = st.ctx["fvg"]
    r = st.ctx["raid"]
    return {
        "status": "A+", "setup_score": 100,
        "entry": {"price": fx(plan.decision_price), "type": "market_close",
                  "trigger": f"M1 candle body close {'above' if buy else 'below'} {fx(f['ce'])}"},
        "user_confirmation": {"mode": "SWEEP_PLUS_CISD",
                              "levels": {"sweep_level": fx(r["level"]), "trigger_level": fx(f["ce"]),
                                         "continuation_level": None},
                              "timeframe": "M1",
                              "rules_plain": [f"Rule 1: price traded {'below' if buy else 'above'} {fx(r['level'])}",
                                              f"Rule 2: M1 body close {'above' if buy else 'below'} {fx(f['ce'])}"]},
        "entry_zone": {"entry_zone_low": fx(f["low"]), "entry_zone_high": fx(f["high"]),
                       "refinement_method": "Standard CISD", "sniper_entry_precision": 1.0},
        "entry_timeframe": "M1",
        "stop_loss": {"value": fx(plan.sl), "anchor_type": sl.anchor_type, "live_spread_used": fx(plan.spread),
                      "spread_buffer": fx(sl.spread_buffer),
                      "formula_description": "BUY → SL = anchor − buffer" if buy else "SELL → SL = anchor + buffer"},
        "tp_policy": "R_MULTIPLE_LIQUIDITY_3R_MIN",
        "risk_distance_points": fx(plan.risk),
        "management_level": {"price": fx(lad.management_level),
                             "note": "FTA < 3R — management only, not a take-profit"},
        "take_profit": {
            "tp1": {"price": fx(lad.tp1), "r_multiple_at_tp": round(lad.tp1_r, 2),
                    "tp_distance_points": fx(abs(lad.tp1 - plan.decision_price)), "evidence": lad.tp1_evidence},
            "tp2": {"price": fx(lad.tp2), "r_multiple_at_tp": None if lad.tp2_r is None else round(lad.tp2_r, 2),
                    "tp_distance_points": None if lad.tp2 is None else fx(abs(lad.tp2 - plan.decision_price)),
                    "evidence": lad.tp2_evidence},
            "tp3": {"price": fx(lad.tp3), "r_multiple_at_tp": None if lad.tp3_r is None else round(lad.tp3_r, 2),
                    "tp_distance_points": None if lad.tp3 is None else fx(abs(lad.tp3 - plan.decision_price)),
                    "evidence": "final_lrlr_objective"}},
        "trap_integration": {"trap_assist": True, "trap_type_used": "SINGLE_SIDED_LIQUIDITY_TRAP",
                             "trap_entry_rule": "DUAL_SWEEP_FLIP" if plan.sweep_count >= 2 else "SRT_ENGULF",
                             "trap_anchor_price": fx(r["level"]), "anchor_type": "SWEEP"},
        "trade_quality": {"setup_type": "A+ CRT" if "CRT" in plan.strategy_used[0] else "OBM",
                          "trap_identified": "SINGLE_SIDED_LIQUIDITY_TRAP", "confidence_score": 100,
                          "micro_validation": "BodyClose(Type7) with displacement"},
        "bias": plan.side,
        "trade_type_context": "Intraday" if kz["time_horizon"] == "INTRADAY" else "HTF Swing",
        "strategy_used": plan.strategy_used,
        "thesis_anchor": {"root_liquidity_type": plan.root_type, "root_liquidity_level": fx(plan.root_price),
                          "root_lps": plan.root_lps, "root_liquidity_id": kz["generating_liquidity_id"],
                          "sweep_count_at_entry": plan.sweep_count,
                          "sweep_type_at_entry": "SWEPT_BODY" if plan.sweep_type == "BODY_CLOSE" else "SWEPT_WICK",
                          "thesis_invalidation_level": fx(plan.root_price),
                          "active_causal_chain": {"chain_assigned": plan.chain[-1],
                                                  "pda_confidence_inherited": kz["validation_score"],
                                                  "generating_event_tf": kz["timeframe"]}},
        "confidence_reasoning": (
            f"HTF {plan.side} bias → {kz['timeframe']} {kz['zone_type']} POI → M15 Bellwether in {p2sub['killzone']} "
            f"(Judas beyond {fx(p2sub['judas_threshold'])}) → M5/M1 sniper: {r['tf']} Type 7 sweep of {r['kind']} "
            f"@ {fx(r['level'])}, displacement, M1 MSS, FVG retest at CE {fx(f['ce'])}. Thesis root: "
            f"{plan.root_type} @ {fx(plan.root_price)} (LPS {plan.root_lps}); sweep #{plan.sweep_count} at zone. "
            f"Pay-the-Trader at TP1."),
        "confidence_percentage": 100,
        "reasoning": f"Sweep, MSS, FVG, retest; TP ladder {fx(lad.tp1)} → {fx(lad.tp2)} → {fx(lad.tp3)}.",
        "contextual_confidence_score": {"p1_bias": scores[0], "p2_keylevel": scores[1], "p3_ltf_entry": scores[2],
                                        "p4_confirmation": scores[3], "total": sum(scores)},
        "execution_notes": {"ocr_status": "OK", "live_ask": fx(ctx.inp.ask), "live_bid": fx(ctx.inp.bid),
                            "live_spread_points": fx(plan.spread), "audit_log_id": plan.setup_key},
    }

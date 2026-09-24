"""GEM1 HTF Structure Mapper — Step 0 -> Step 8, emitting the LAYER 4 Part 1 JSON exactly.

Pure function of (closed candles, price, now). Never reads a bar that is not closed at `now` (A1).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from mapex.core import levels as lv
from mapex.core import primitives as pr
from mapex.core.primitives import Bar
from mapex.core.timeutil import (
    current_session,
    fmt_cet,
    fmt_ny,
    ny,
    ny_midnight,
    week_start,
)
from mapex.mapper import chains as ch
from mapex.mapper.liquidity import TF_RANK, Pool, build_registry, dealing_range

MIN_BARS = {"D1": 30, "H4": 30, "H1": 30}
# gem1-mapper-spec input sizes (closed bars): D1 250, H4 250, H1 300, M15 (session ranges), W1 60, MN1 24
LIMITS = {"MN1": 24, "W1": 60, "D1": 250, "H4": 250, "H1": 300, "M15": 1500}
# D-70 intraday mode (the owner's default): zones reachable within one session ATR are chained first
INTRADAY_REACH_ATR = 1.0
INTRADAY_LINK_BARS = 3
MAX_ALERTS = 8


@dataclass
class MapperInput:
    symbol: str
    now: float
    price: float  # live bid
    bars: dict[str, list[Bar]]
    tick: float = 0.01
    decimals: int = 2
    gates: tuple[int, int, int] = (65, 50, 40)
    mode: str = "strict"  # "strict" = GEM1 to the letter; "intraday" = D-70


@dataclass
class MapResult:
    valid: bool
    reason: str | None
    json: dict | None
    meta: dict = field(default_factory=dict)
    failed_checks: list[str] = field(default_factory=list)

    def dumps(self) -> str:
        return json.dumps({"map": self.json, "meta": self.meta}, separators=(",", ":"))


def zone_key(symbol: str, tf: str, zone_type: str, formed_at: int, direction: str) -> str:
    return f"{symbol}|{tf}|{zone_type}|{formed_at}|{direction}"


# ---------------------------------------------------------------- Step 2 helpers

def d1_narrative(d1: list[Bar]) -> str:
    if len(d1) < 2:
        return "neutral"
    last, prev = d1[-1], d1[-2]
    mid = (prev.h + prev.l) / 2
    if last.c > last.o and last.c > mid:
        return "bullish"
    if last.c < last.o and last.c < mid:
        return "bearish"
    return "neutral"


def bias_hierarchy(d1: list[Bar], h4: list[Bar], h1: list[Bar]) -> dict:
    """2C: LTH from D1 structure (60 bars), ITH = last H4 BOS/MSS with displacement, STH = last H1 BOS."""
    tail = d1[-60:]
    lth = pr.structure_direction(tail)
    if lth is None:  # DECISIONS D-11
        lth = "bullish" if tail[-1].c >= tail[0].c else "bearish"
    ith = "corrective"
    atrs4 = pr.atr_series(h4)
    for brk in reversed(pr.structure_breaks(h4)):
        if any(pr.is_displacement(h4, j, atrs4[j]) and (h4[j].bull == (brk.direction == "buy"))
               for j in range(max(0, brk.idx - 2), brk.idx + 1)):
            ith = "bullish" if brk.direction == "buy" else "bearish"
            break
    brks1 = pr.structure_breaks(h1)
    sth = ("bullish" if brks1[-1].direction == "buy" else "bearish") if brks1 else "corrective"
    opposite = {"bullish": "bearish", "bearish": "bullish"}
    if sth != "corrective" and (opposite[sth] == lth or opposite[sth] == ith):
        sth = "corrective"
    return {"LTH": lth, "ITH": ith, "STH": sth}


def live_pools(registry: list[Pool]) -> list[Pool]:
    return [p for p in registry if p.status in ("UNTOUCHED", "BEING_TARGETED")]


def _best(pools: list[Pool]) -> Pool | None:
    return max(pools, key=lambda p: (p.lps, TF_RANK[p.tf], -int(p.id[4:])), default=None)


def _hier_bias(hier: dict) -> str | None:
    for k in ("ITH", "LTH"):
        if hier.get(k) in ("bullish", "bearish"):
            return "buy" if hier[k] == "bullish" else "sell"
    return None


def derive_bias(registry: list[Pool], price: float, d1: list[Bar], hier: dict, resolve: bool = False) -> dict:
    """2B: liquidity-first bias. Returns {'bias': buy|sell|None, 'reason', 'conflict', 'tie_break', ...}.
    `resolve` (intraday mode, D-70): a tie, an empty side or an LTH+ITH conflict is not a dead end — 2B says the
    bias hierarchy (LTH/ITH/STH) resolves it via 2C, so the hierarchy direction is used."""
    live = live_pools(registry)
    up = _best([p for p in live if p.price > price])
    down = _best([p for p in live if p.price < price])
    narrative = d1_narrative(d1)
    out = {"bias": None, "reason": None, "conflict": False, "tie_break": None, "narrative": narrative,
           "top_up": up.id if up else None, "top_down": down.id if down else None}
    if not up and not down:
        out["reason"] = "no_untouched_pools"
        if resolve and _hier_bias(hier):
            out.update(bias=_hier_bias(hier), reason=None, resolved_by="hierarchy")
        return out
    if up and down and abs(up.lps - down.lps) <= 10:
        if TF_RANK[up.tf] != TF_RANK[down.tf]:
            cand, out["tie_break"] = ("buy" if TF_RANK[up.tf] > TF_RANK[down.tf] else "sell"), "dominant_tf"
        elif narrative != "neutral":
            cand, out["tie_break"] = ("buy" if narrative == "bullish" else "sell"), "d1_narrative"
        else:
            out["reason"] = "bias_tie"
            if resolve and _hier_bias(hier):
                out.update(bias=_hier_bias(hier), reason=None, resolved_by="hierarchy")
            return out
    else:
        cand = "buy" if up and (not down or up.lps > down.lps) else "sell"
    out["conflict"] = narrative != "neutral" and (narrative == "bullish") != (cand == "buy")
    opposing = "bearish" if cand == "buy" else "bullish"
    if hier["LTH"] == opposing and hier["ITH"] == opposing:
        if resolve:
            out.update(bias="sell" if cand == "buy" else "buy", conflict=True, resolved_by="hierarchy")
            return out
        out["reason"] = "bias_conflict_htf"
        return out
    out["bias"] = cand
    return out


def po3_phase(m15: list[Bar], now: float, bias: str) -> str:
    """2D: Accumulation in Asia; Manipulation until the against-bias sweep of the NY-midnight open;
    Distribution once it happened and price is back through the anchor in the bias direction."""
    if current_session(now) == "Asia":
        return "Accumulation"
    mid = ny_midnight(now)
    today = [b for b in m15 if b.t >= mid]
    if not today:
        return "Manipulation"
    anchor = today[0].o
    last = today[-1].c
    if bias == "buy":
        swept = min(b.l for b in today) < anchor
        return "Distribution" if swept and last > anchor else "Manipulation"
    swept = max(b.h for b in today) > anchor
    return "Distribution" if swept and last < anchor else "Manipulation"


def weekly_profile(h1: list[Bar], now: float, bias: str) -> str:
    """2E (informational): Expansion / Reversal / Range from this week's H1 bars (DECISIONS D-12)."""
    ws = week_start(now)
    wk = [b for b in h1 if b.t >= ws]
    if len(wk) < 2:
        return "Range"
    wopen = wk[0].o
    hb, lb = max(wk, key=lambda b: b.h), min(wk, key=lambda b: b.l)
    rng = hb.h - lb.l
    if rng <= 0:
        return "Range"
    for ext, kind in ((hb, "high"), (lb, "low")):
        if ny(ext.t).weekday() in (1, 2):
            after = [b for b in wk if b.t > ext.t]
            if any((b.c < wopen) if kind == "high" else (b.c > wopen) for b in after):
                return "Weekly Reversal"
    bias_ext, opp = (hb, lb.l) if bias == "buy" else (lb, hb.h)
    if bias_ext.t >= now - 86400 and abs(wopen - opp) <= 0.25 * rng:
        return "Weekly Expansion"
    return "Range"


def special_days(d1: list[Bar], price: float, tick: float) -> dict:
    """2F: inside day, big event range, daily discount wick (CE of the wick)."""
    out = {"inside_day": False, "big_event_range": False, "daily_discount_wick_ce": None}
    if len(d1) < 16:
        return out
    last, prev = d1[-1], d1[-2]
    atr_d1 = pr.atr(d1[:-1], 14)
    out["inside_day"] = last.h < prev.h and last.l > prev.l
    out["big_event_range"] = last.range >= 1.5 * atr_d1
    tol = lv.level_tol(price, atr_d1, tick)
    lower_wick = last.body_bot - last.l
    if abs(last.o - prev.c) <= tol and last.range > 0 and lower_wick >= 0.6 * last.range:
        out["daily_discount_wick_ce"] = (last.l + last.body_bot) / 2
    return out


def opening_gaps(symbol: str, m15: list[Bar], h1: list[Bar], now: float) -> list[dict]:
    """NDOG (16:59 close vs 18:00 re-open) and NWOG (Friday close vs Sunday open). None for 24/7 BTC."""
    if symbol.upper().startswith("BTC"):
        return []
    series = m15 or h1
    found: dict[str, tuple[float, float]] = {}
    for prev, b in zip(series, series[1:], strict=False):
        d = ny(b.t)
        if d.hour == 18 and d.minute == 0 and b.t - prev.t > 3600 and b.o != prev.c:
            found["NWOG" if d.weekday() == 6 else "NDOG"] = (min(b.o, prev.c), max(b.o, prev.c))  # latest wins
    return [{"type": t, "gap_low": lo, "gap_high": hi, "anchor_type": "STRUCTURAL (FVG)"}
            for t, (lo, hi) in sorted(found.items())]


# ---------------------------------------------------------------- main pipeline

def build_map(inp: MapperInput) -> MapResult:
    now, price, sym, dec = inp.now, inp.price, inp.symbol, inp.decimals
    bars = {tf: pr.closed_bars(inp.bars.get(tf, []), tf, now)[-n:] for tf, n in LIMITS.items()}
    meta: dict = {"symbol": sym, "created_at": int(now), "price": price, "decimals": dec}
    short = [tf for tf, n in MIN_BARS.items() if len(bars[tf]) < n]
    if short or not bars["M15"]:
        return MapResult(False, "insufficient_data", None, meta, [f"bars:{','.join(short) or 'M15'}"])

    def fmt(x: float | None) -> str | None:
        return None if x is None else f"{x:.{dec}f}"

    d1, h4, h1, m15 = bars["D1"], bars["H4"], bars["H1"], bars["M15"]
    s_atr, s_fb = lv.session_atr(m15, h1, now)
    rng = dealing_range(d1)
    atr_d1 = pr.atr(d1, 14)
    pdas = {tf: ch.detect_pdas(bars[tf], tf) for tf in ("D1", "H4", "H1")}
    ctx = {"price": price, "now": now, "tick": inp.tick, "bars": bars, "dealing_range": rng, "session_atr": s_atr}
    # STEP 0
    registry = build_registry(ctx, ch.active_htf_zones(pdas))
    # STEP 1
    session = current_session(now)
    reach = 0.4 * s_atr
    # STEP 2
    hier = bias_hierarchy(d1, h4, h1)
    intraday_mode = inp.mode == "intraday"
    b = derive_bias(registry, price, d1, hier, resolve=intraday_mode)
    meta.update({"session_atr": s_atr, "session_atr_fallback": s_fb, "bias_derivation": b,
                 "dealing_range": {"high": rng.high, "low": rng.low, "eq": rng.eq}})
    if b["bias"] is None:
        return MapResult(False, b["reason"], None, meta, [b["reason"]])
    bias = b["bias"]
    po3 = po3_phase(m15, now, bias)
    wprof = weekly_profile(h1, now, bias)
    sdays = special_days(d1, price, inp.tick)
    tol_d1 = lv.level_tol(price, atr_d1, inp.tick)

    def pd_status(x: float) -> str:
        if abs(x - rng.eq) <= tol_d1:
            return "Equilibrium"
        return "Premium" if x > rng.eq else "Discount"

    # STEP 5 — DOL
    live = live_pools(registry)
    in_dir = [p for p in live if (p.price > price if bias == "buy" else p.price < price)]
    ranked = sorted(in_dir, key=lambda p: (-p.lps, -TF_RANK[p.tf], int(p.id[4:])))
    primary = ranked[0] if ranked else None
    secondary = ranked[1] if len(ranked) > 1 else None
    d1_erl = [p for p in ranked if p.tf == "D1" and p.erl]
    final = d1_erl[0] if d1_erl else primary
    gaps = opening_gaps(sym, m15, h1, now)
    mid_open = lv.day_open_at(m15, ny_midnight(now))
    week_open = lv.day_open_at(h1, week_start(now))
    def beyond(c: ch.Candidate) -> float | None:
        z = c.zone
        cand = [p for p in live if (p.price > z.high if bias == "buy" else p.price < z.low)]
        near = min(cand, key=lambda p: abs(p.price - z.anchor), default=None)
        return near.price if near else (primary.price if primary else None)

    def ladder_ok(c: ch.Candidate) -> bool:  # Step 8 tp1/tp2 check, applied before chaining in intraday mode
        tp1, tp2 = beyond(c), primary.price if primary else None
        if tp1 is None or tp2 is None:
            return False
        if bias == "buy":
            return c.zone.high < tp1 <= tp2
        return c.zone.low > tp1 >= tp2

    # STEP 7 — chains
    linked, unlinked = ch.candidates(pdas, bars, registry, bias, price, hier["LTH"], hier["ITH"],
                                     window=INTRADAY_LINK_BARS if intraday_mode else 2, keep_touched=intraday_mode)
    sel_reach = INTRADAY_REACH_ATR * s_atr
    if intraday_mode:
        chains = ch.assign_intraday([c for c in linked if ladder_ok(c)], inp.gates, price, sel_reach, bias)
    else:
        chains = ch.assign_chains(linked, inp.gates)
    for name, c in chains.items():
        c.gen.generated_pda_id = name
    if "CHAIN_A" in chains:
        chains["CHAIN_A"].gen.status = "PDA_GENERATING"
    # STEP 6 — alerts (after statuses are final)
    alert_pools = [p for p in registry if p.status in ("UNTOUCHED", "BEING_TARGETED") and p.lps >= 50]
    alert_pools.sort(key=lambda p: (-p.lps, p.status != "BEING_TARGETED", int(p.id[4:])))
    crit = [p for p in alert_pools if p.importance == "CRITICAL"]
    chosen = alert_pools[:MAX_ALERTS]
    chosen += [p for p in crit if p not in chosen]
    chosen.sort(key=lambda p: (-p.lps, p.status != "BEING_TARGETED", int(p.id[4:])))

    def alert(p: Pool) -> dict:
        above = p.price > price
        aligned = (above and bias == "buy") or (not above and bias == "sell")
        return {
            "timeframe": p.tf, "price_level": fmt(p.price), "type": p.schema_type(),
            "label": f"{p.labels[0]} at {fmt(p.price)}", "lps": p.lps, "status": p.status,
            "alert_price": fmt(p.price), "alert_trigger": "CROSS_UP" if above else "CROSS_DOWN",
            "direction_of_sweep": "UPWARD_SWEEP" if above else "DOWNWARD_SWEEP", "importance": p.importance,
            "reason": (f"{p.tf} {'BSL' if above else 'SSL'} {p.schema_type()} ({p.labels[0]}), "
                       f"{lv.sessions_between(p.avail_t, now)} sessions old, status {p.status}. "
                       f"{'Upward' if above else 'Downward'} sweep collects {'buy' if above else 'sell'}-stops; "
                       f"expect {'bearish' if above else 'bullish'} displacement. "
                       f"{'Aligned with' if aligned else 'Against'} {bias} bias."),
            "expected_pda_type_after_sweep": "FVG" if p.tf == "H1" else "OB",
            "linked_pda_id_if_already_swept": None,
        }

    key_zones, zkeys = [], {}
    for name in ("CHAIN_A", "CHAIN_B", "CHAIN_C"):
        c = chains.get(name)
        if c is None:
            continue
        z = c.zone
        ztype = ch.ZONE_TYPE[z.kind]
        if intraday_mode:
            dist = ch.distance(c, price, bias)
            horizon = "INTRADAY" if dist is not None and dist <= sel_reach else "SWING_HTF"
        else:
            horizon = "SWING_HTF" if (abs(z.anchor - price) > reach or c.score >= 80) else "INTRADAY"
        zkeys[name] = zone_key(sym, c.tf, ztype, z.formed_at, bias)
        key_zones.append({
            "id": name, "timeframe": c.tf, "direction": bias, "zone_type": ztype,
            "zone_label": f"{c.tf} {'bullish' if bias == 'buy' else 'bearish'} {z.kind} @ {fmt(z.anchor)}",
            "zone_low": fmt(z.low), "zone_high": fmt(z.high), "anchor_price": fmt(z.anchor),
            "anchor_type": ch.ANCHOR_TYPE[z.kind], "pd_status": pd_status(z.anchor),
            "liquidity_class": "IRL" if rng.contains(z.anchor) else "ERL", "time_horizon": horizon,
            "generating_liquidity_id": c.gen.id, "generating_lps": c.gen.lps, "causal_sweep_type": c.sweep_type,
            "validation_score": c.score, "validation_breakdown": dict(c.breakdown),
            "expected_reach_window": "Current session" if horizon == "INTRADAY" else "1-3 sessions",
            "suggested_pearl": "TURTLE_SOUP" if c.sweep_type == "SWEPT_WICK" else "CRT",
            "target_liquidity_pool": (f"{primary.schema_type()} {primary.id} @ {fmt(primary.price)}"
                                      if primary else None),
            "tp1": fmt(beyond(c)), "tp2": fmt(primary.price if primary else None),
        })
    a = chains.get("CHAIN_A")
    acc = {
        "root_liquidity_id": a.gen.id if a else None, "root_lps": a.gen.lps if a else None,
        "root_price_level": fmt(a.gen.price) if a else None, "sweep_occurred": bool(a),
        "sweep_type": a.sweep_type if a else None,
        "displacement_generated": (f"{a.tf} {'bullish' if bias == 'buy' else 'bearish'} displacement, "
                                   f"{a.disp_quality.lower()} body, FVG created") if a else None,
        "active_pda_id": "CHAIN_A" if a else None, "pda_confidence_inherited": a.score if a else None,
        "chain_assigned": "A" if a else None,
    }
    target = "external" if primary is None or primary.erl else "internal"
    if primary and secondary and primary.erl != secondary.erl:
        target = "mixed"
    tags = [k for k in ("inside_day", "big_event_range") if sdays[k]]
    if sdays["daily_discount_wick_ce"] is not None:
        tags.append(f"daily_discount_wick CE {fmt(sdays['daily_discount_wick_ce'])}")
    profile = (f"Price {pd_status(price)} of D1 dealing range {fmt(rng.low)}-{fmt(rng.high)}; "
               f"DOL {primary.id + ' ' + primary.schema_type() + ' @ ' + fmt(primary.price) if primary else 'none'}"
               f"{'; secondary ' + secondary.id if secondary else ''}; "
               f"final objective {final.id if final else 'none'}"
               f"{'; ' + ', '.join(tags) if tags else ''}"
               f"{'; D1 narrative conflicts with LPS delivery' if b['conflict'] else ''}.")
    alarms = [{"trigger_price": kz["anchor_price"], "trigger_type": "CROSS_DOWN" if bias == "buy" else "CROSS_UP",
               "label": f"{kz['id']} {kz['zone_type']} entry", "lps": kz["generating_lps"],
               "reason": f"{kz['zone_label']} generated by {kz['generating_liquidity_id']} "
                         f"({kz['causal_sweep_type']})"} for kz in key_zones]
    intraday = [{"type": kz["zone_type"], "zone_low": kz["zone_low"], "zone_high": kz["zone_high"],
                 "mean_threshold": fmt((float(kz["zone_low"]) + float(kz["zone_high"])) / 2),
                 "validation_score": kz["validation_score"], "expected_reach_time": kz["expected_reach_window"],
                 "rationale": f"{kz['id']} within reach"} for kz in key_zones if kz["time_horizon"] == "INTRADAY"]
    for kind in ("BPR", "VI"):
        for z in pdas["H1"][kind][-3:]:
            if z.direction == bias and z.mitigated_at is None and len(intraday) < 6:
                intraday.append({"type": kind, "zone_low": fmt(z.low), "zone_high": fmt(z.high),
                                 "mean_threshold": fmt(z.ce), "validation_score": 0,
                                 "expected_reach_time": "Current session", "rationale": f"H1 {kind} in bias direction"})
    swing_levels = [{"type": ch.ZONE_TYPE[c.zone.kind], "zone_low": fmt(c.zone.low), "zone_high": fmt(c.zone.high),
                     "cisd_anchor_price": fmt(c.zone.anchor),
                     "invalidation_point_mt": fmt(c.zone.low if bias == "buy" else c.zone.high),
                     "validation_score": c.score, "time_horizon": "SWING_HTF",
                     "rationale": f"{c.tf} UNLINKED — no registry pool swept in the 2 bars before displacement"}
                    for c in sorted(unlinked, key=ch.Candidate.order_key)[:8]]
    out = {
        "strategic_bias": bias,
        "bias_hierarchy": hier,
        "po3_phase": po3,
        "weekly_profile": wprof,
        "market_phase": f"{session} {po3}",
        "liquidity_profile": profile,
        "target_liquidity": target,
        "active_causal_chain": acc,
        "session_context": {"current_session": session, "current_local_time": f"{fmt_ny(now)} NY / {fmt_cet(now)}",
                            "session_atr": fmt(s_atr), "intraday_reachability_filter": fmt(reach)},
        "smt_analysis": {"status": "NO SMT VISIBLE", "type": None, "significance": None},
        "static_anchors": {"ny_midnight": fmt(mid_open), "weekly_open": fmt(week_open),
                           "opening_gaps": [{**g, "gap_low": fmt(g["gap_low"]), "gap_high": fmt(g["gap_high"])}
                                            for g in gaps]},
        "liquidity_registry": [{
            "id": p.id, "timeframe": p.tf, "type": p.schema_type(), "label": " · ".join(p.labels),
            "price_level": fmt(p.price), "lps": p.lps, "status": p.status, "generated_pda_id": p.generated_pda_id,
            "importance": p.importance, "direction_of_sweep_expected": "UPWARD" if p.price > price else "DOWNWARD",
        } for p in registry],
        "htf_liquidity_alerts": [alert(p) for p in chosen],
        "tp_policy": "R_MULTIPLE_LIQUIDITY_3R_MIN",
        "final_lrlr_objective": fmt(final.price) if final else None,
        "key_zones": key_zones,
        "alarms": alarms,
        "intraday_keylevels": intraday,
        "htf_swing_keylevels": swing_levels,
    }
    # STEP 8 — verification (hard gate)
    checks = verify(out, b, hier, registry, price, session, final, primary)
    failed = [k for k, ok in checks.items() if not ok]
    struct = json.dumps({"bias": bias, "zones": [(k, zkeys[k], kz["generating_liquidity_id"])
                                                for k, kz in zip(zkeys, key_zones, strict=True)],
                         "root": acc["root_liquidity_id"]}, sort_keys=True)
    meta.update({"zone_keys": zkeys, "checks": checks, "special_days": sdays, "po3": po3,
                 "struct_hash": hashlib.sha256(struct.encode()).hexdigest()[:16],
                 "primary_dol": primary.price if primary else None, "final_lrlr": final.price if final else None})
    meta["hash"] = hashlib.sha256(json.dumps(out, separators=(",", ":")).encode()).hexdigest()[:16]
    if failed and not intraday_mode:
        return MapResult(False, f"verification_failed:{failed[0]}", out, meta, failed)
    if failed:  # intraday mode: Step 8 "resolve before output" — zones were already filtered, the rest is noted
        meta["warnings"] = failed
    if b.get("resolved_by"):
        meta.setdefault("warnings", []).append(f"bias_resolved_by_{b['resolved_by']}")
    return MapResult(True, None, out, meta)


def verify(m: dict, b: dict, hier: dict, registry: list[Pool], price: float, session: str,
           final: Pool | None, primary: Pool | None) -> dict[str, bool]:
    """GEM1 Step 8 — every check as an assertion; a failure publishes nothing."""
    bias = m["strategic_bias"]
    opposing = "bearish" if bias == "buy" else "bullish"
    ids = {p["id"] for p in m["liquidity_registry"]}
    prices = {p["price_level"] for p in m["liquidity_registry"]}
    up = bias == "buy"

    def beyond_zone(v: str | None, kz: dict) -> bool:
        return v is not None and (float(v) > float(kz["zone_high"]) if up else float(v) < float(kz["zone_low"]))

    def ladder_ok(kz: dict) -> bool:
        if not (beyond_zone(kz["tp1"], kz) and beyond_zone(kz["tp2"], kz)):
            return False
        return float(kz["tp1"]) <= float(kz["tp2"]) if up else float(kz["tp1"]) >= float(kz["tp2"])

    re_derived = derive_bias(registry, price, [], hier) if b.get("tie_break") != "d1_narrative" else b
    anchor_ok = {"OB": "STRUCTURAL (CISD)", "BB": "STRUCTURAL (CISD)", "REJECTION_BLOCK": "STRUCTURAL (CISD)",
                 "FVG": "STRUCTURAL (FVG)", "IFVG": "STRUCTURAL (FVG)", "LIQUIDITY_POOL": "SWEEP"}
    return {
        "bias_matches_hierarchy": not (hier["LTH"] == opposing and hier["ITH"] == opposing),
        "bias_derived_from_registry": re_derived.get("bias") == bias,
        "po3_consistent_with_session": (m["po3_phase"] == "Accumulation") == (session == "Asia"),
        "key_zones_align_with_bias": all(kz["direction"] == bias for kz in m["key_zones"])
        and primary is not None and ((primary.price > price) == up),
        "anchor_types_correct": all(kz["anchor_type"] == anchor_ok[kz["zone_type"]] for kz in m["key_zones"]),
        "tp1_tp2_within_reach": all(ladder_ok(kz) for kz in m["key_zones"]),
        "final_lrlr_is_dominant_dol": final is not None and ((final.price > price) == up)
        and (final.tf == "D1" or final is primary),
        "no_unlinked_chain": all(kz["causal_sweep_type"] != "UNLINKED" for kz in m["key_zones"]),
        "alerts_have_critical_or_high": any(a["importance"] in ("CRITICAL", "HIGH")
                                            for a in m["htf_liquidity_alerts"]),
        "zone_generators_in_registry": all(kz["generating_liquidity_id"] in ids for kz in m["key_zones"]),
        "alert_levels_in_registry": all(a["price_level"] in prices for a in m["htf_liquidity_alerts"]),
    }


def brief(m: dict) -> str:
    """LAYER 4 Part 2 — Liquidity Intelligence Brief (fixed template, used by /map and replay)."""
    lines = ["🚨 LIQUIDITY INTELLIGENCE BRIEF", "", "📊 HTF LIQUIDITY SWEEP ALARMS (Set these in MT5 first):"]
    for a in m["htf_liquidity_alerts"]:
        if a["importance"] in ("CRITICAL", "HIGH"):
            lines += [f"  🔔 {a['timeframe']} {a['type']} @ {a['alert_price']} | LPS: {a['lps']} | {a['importance']}",
                      f"     Trigger: {a['alert_trigger']}",
                      f"     If swept: {a['expected_pda_type_after_sweep']} expected",
                      f"     Why: {' '.join(a['reason'].split()[:25])}"]
    lines += ["", "─" * 49, "",
              f"🏗️ Context: Bias {m['strategic_bias']} derived from highest-LPS delivery direction",
              f"   PO3: {m['po3_phase']} | DOL: {m['final_lrlr_objective']} | Cycle: {m['target_liquidity']}"]
    reg = {p["id"]: p for p in m["liquidity_registry"]}
    for kz in m["key_zones"]:
        root = reg.get(kz["generating_liquidity_id"], {})
        inval = m["active_causal_chain"]["root_price_level"] if kz["id"] == "CHAIN_A" else root.get("price_level")
        lines += ["", f"📍 {kz['id'].replace('_', ' ')} — {kz['zone_label']}",
                  f"   Root Liquidity: {root.get('type')} @ {root.get('price_level')} (LPS {kz['generating_lps']})",
                  f"   PDA Confidence: {kz['validation_score']}/100 | {kz['zone_type']} @ {kz['anchor_price']}",
                  f"   🔔 PDA Entry Alert: {kz['anchor_price']} ({kz['anchor_type']})",
                  f"   💡 Pearl: {kz['suggested_pearl']}",
                  f"   🎯 DOL Target: {kz['tp2']} ({kz['target_liquidity_pool']})",
                  f"   ❌ Thesis Invalidation: D1 body close beyond {inval}"]
    return "\n".join(lines)

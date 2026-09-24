"""The owner's architecture (D-71): Gemini maps (GEM1), MAPEX executes (GEM2).

A GEM1 JSON sent by Gemini becomes the map the executor reads — validated, never trusted: every price must be a number
inside the symbol's band, each zone must point in the bias direction, sit on the side price retraces to, be within
reach, and carry targets beyond it. Values Gemini cannot measure exactly (session ATR, the D1 dealing range, the time
horizon) are computed by MAPEX from its own candles. A zone that fails is dropped with its reason; a map without a
usable CHAIN_A/B is refused.
"""

from __future__ import annotations

import hashlib
import json
import re

from mapex.config import Settings
from mapex.core import levels as lv
from mapex.core import primitives as pr
from mapex.core.timeutil import current_session, fmt_cet, fmt_ny
from mapex.mapper.engine import MapResult
from mapex.mapper.liquidity import dealing_range

ZONE_TYPES = {"OB", "BB", "FVG", "IFVG", "REJECTION_BLOCK", "LIQUIDITY_POOL"}
ANCHOR = {"OB": "STRUCTURAL (CISD)", "BB": "STRUCTURAL (CISD)", "REJECTION_BLOCK": "STRUCTURAL (CISD)",
          "FVG": "STRUCTURAL (FVG)", "IFVG": "STRUCTURAL (FVG)", "LIQUIDITY_POOL": "SWEEP"}
STATUSES = {"UNTOUCHED", "SWEPT_BODY", "SWEPT_WICK", "BEING_TARGETED", "PDA_GENERATING"}
MAX_DIST_D1_ATR = 3.0  # a zone further than this from price is not a plan for the coming sessions
NUM = re.compile(r"-?\d+(?:\.\d+)?")


class MapRejected(ValueError):
    def __init__(self, reasons: list[str]):
        super().__init__("; ".join(reasons))
        self.reasons = reasons


def num(v) -> float | None:
    """A price field: a number, or the first number in a string ("4273.68", "approx. 4273.68")."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int | float):
        return float(v)
    m = NUM.search(str(v or "").replace(",", ""))
    return float(m.group()) if m else None


def parse(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    text = str(raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)  # a fenced block pasted as is
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise MapRejected(["no JSON object found"])
    try:
        data = json.loads(text[start:end + 1])
    except ValueError as exc:
        raise MapRejected([f"invalid JSON: {exc}"]) from None
    if not isinstance(data, dict):
        raise MapRejected(["the JSON must be an object"])
    return data


def accept(raw, symbol: str, s: Settings, bars: dict, price: float, now: float) -> MapResult:
    """A valid MapResult for the executor (meta["warnings"] lists what was fixed or dropped), or MapRejected."""
    g = parse(raw)
    dec = s.display_decimals.get(symbol, 2)
    band = s.price_bands.get(symbol, [0, 1e12])
    warnings: list[str] = []

    def fmt(x: float | None) -> str | None:
        return None if x is None else f"{x:.{dec}f}"

    def in_band(x: float | None) -> bool:
        return x is not None and band[0] <= x <= band[1]

    bias = str(g.get("strategic_bias", "")).strip().lower()
    if bias not in ("buy", "sell"):
        raise MapRejected([f"strategic_bias must be buy or sell (got {g.get('strategic_bias')!r})"])
    if not price or not in_band(price):
        raise MapRejected(["no live price for this symbol yet"])
    d1, h1, m15 = bars.get("D1", []), bars.get("H1", []), bars.get("M15", [])
    if len(d1) < 15 or not h1:
        raise MapRejected(["MAPEX has too few candles yet; try again in a minute"])
    atr_d1 = pr.atr(d1, 14)
    s_atr, _ = lv.session_atr(m15, h1, now)
    buy = bias == "buy"

    registry, reg_ids = [], {}
    for p in g.get("liquidity_registry") or []:
        if not isinstance(p, dict) or not p.get("id"):
            continue
        lvl = num(p.get("price_level"))
        if not in_band(lvl):
            warnings.append(f"registry {p.get('id')}: price {p.get('price_level')!r} dropped")
            continue
        status = str(p.get("status", "UNTOUCHED")).upper().replace(" ", "_").replace("(", "").replace(")", "")
        entry = {**p, "id": str(p["id"]), "price_level": fmt(lvl), "lps": max(0, min(100, int(num(p.get("lps")) or 0))),
                 "status": status if status in STATUSES else "UNTOUCHED", "type": str(p.get("type") or "SWING"),
                 "timeframe": str(p.get("timeframe") or "H1")}
        registry.append(entry)
        reg_ids[entry["id"]] = entry

    final = num(g.get("final_lrlr_objective"))
    zones, errors = [], []
    for kz in g.get("key_zones") or []:
        zid = str(kz.get("id", "")).upper() if isinstance(kz, dict) else ""
        if zid not in ("CHAIN_A", "CHAIN_B", "CHAIN_C"):
            continue
        why = []
        if str(kz.get("direction", "")).lower() != bias:
            why.append(f"direction {kz.get('direction')!r} is not the {bias} bias")
        lo, hi = num(kz.get("zone_low")), num(kz.get("zone_high"))
        if not (in_band(lo) and in_band(hi)):
            why.append(f"zone {kz.get('zone_low')!r}-{kz.get('zone_high')!r} is not a price in {band}")
        else:
            if lo > hi:
                lo, hi = hi, lo
                warnings.append(f"{zid}: zone_low/zone_high swapped")
            if lo == hi:
                why.append("zone has no width")
            if (buy and lo > price) or (not buy and hi < price):
                why.append("zone is on the wrong side of price (a retrace can no longer reach it)")
            dist = max(0.0, (price - hi) if buy else (lo - price))
            if dist > MAX_DIST_D1_ATR * atr_d1:
                why.append(f"zone is {dist:.2f} away (> {MAX_DIST_D1_ATR:g} x D1 ATR {atr_d1:.2f})")

        def beyond(x: float | None, lo=lo, hi=hi) -> bool:
            return x is not None and in_band(x) and (x > hi if buy else x < lo)

        tp1, tp2 = num(kz.get("tp1")), num(kz.get("tp2"))
        if not why:
            if not beyond(tp2):
                tp2 = final if beyond(final) else None
            if not beyond(tp1):
                tp1 = tp2
            if tp1 is None:
                why.append("no tp1/tp2 beyond the zone in the bias direction")
            elif (buy and tp1 > tp2) or (not buy and tp1 < tp2):
                tp1, tp2 = tp2, tp1
        if why:
            errors.append(f"{zid}: " + "; ".join(why))
            continue
        anchor = num(kz.get("anchor_price"))
        if anchor is None or not lo - (hi - lo) <= anchor <= hi + (hi - lo):
            anchor = (lo + hi) / 2
        ztype = str(kz.get("zone_type", "FVG")).upper()
        if ztype not in ZONE_TYPES:
            warnings.append(f"{zid}: zone_type {ztype!r} read as FVG")
            ztype = "FVG"
        tf = str(kz.get("timeframe", "H1")).upper() if str(kz.get("timeframe", "H1")).upper() in (
            "D1", "H4", "H1", "M15") else "H1"
        gid = str(kz.get("generating_liquidity_id") or "")
        root_lvl = num(reg_ids[gid]["price_level"]) if gid in reg_ids else None
        if root_lvl is None or (root_lvl > hi if buy else root_lvl < lo):
            # the swept pool is the thesis root (a D1 close past it kills the zone): the zone's far edge stands in
            warnings.append(f"{zid}: root {gid or '-'} missing or on the wrong side — zone edge used as the root")
            gid = f"LIQ_{zid}"
            entry = {"id": gid, "timeframe": tf, "type": "SWING_H" if not buy else "SWING_L",
                     "label": f"{zid} root (not in registry)", "price_level": fmt(hi if not buy else lo),
                     "lps": max(0, min(100, int(num(kz.get("generating_lps")) or 0))), "status": "SWEPT_WICK",
                     "generated_pda_id": zid, "importance": "MEDIUM",
                     "direction_of_sweep_expected": "UPWARD" if not buy else "DOWNWARD"}
            registry.append(entry)
            reg_ids[gid] = entry
        dist = max(0.0, (price - hi) if buy else (lo - price))
        zones.append({
            **kz, "id": zid, "direction": bias, "timeframe": tf, "zone_type": ztype,
            "zone_low": fmt(lo), "zone_high": fmt(hi), "anchor_price": fmt(anchor),
            "anchor_type": kz.get("anchor_type") or ANCHOR[ztype],
            "zone_label": kz.get("zone_label") or f"{tf} {'bullish' if buy else 'bearish'} {ztype} @ {fmt(anchor)}",
            "time_horizon": "INTRADAY" if dist <= s_atr else "SWING_HTF",
            "generating_liquidity_id": gid, "generating_lps": reg_ids[gid]["lps"],
            "causal_sweep_type": str(kz.get("causal_sweep_type") or "SWEPT_WICK").upper(),
            "validation_score": int(num(kz.get("validation_score")) or 0),
            "suggested_pearl": kz.get("suggested_pearl") or "CRT",
            "target_liquidity_pool": kz.get("target_liquidity_pool") or fmt(tp2),
            "tp1": fmt(tp1), "tp2": fmt(tp2),
        })
    if not any(z["id"] in ("CHAIN_A", "CHAIN_B") for z in zones):
        raise MapRejected(errors or ["no CHAIN_A / CHAIN_B in key_zones"])
    warnings += errors

    root_zone = next((z for z in zones if z["id"] == "CHAIN_A"), zones[0])
    root = reg_ids[root_zone["generating_liquidity_id"]]
    given = g.get("active_causal_chain")
    acc = {"sweep_occurred": True, "sweep_type": root_zone["causal_sweep_type"],
           **(given if isinstance(given, dict) else {}),
           "root_liquidity_id": root["id"], "root_lps": root["lps"], "root_price_level": root["price_level"],
           "active_pda_id": root_zone["id"], "pda_confidence_inherited": root_zone["validation_score"],
           "chain_assigned": root_zone["id"][-1]}
    far = [num(z["tp2"]) for z in zones]
    final = final if final is not None and any(x is not None and (final >= x if buy else final <= x) for x in far) \
        else (max(far) if buy else min(far))
    alerts = []
    for a in g.get("htf_liquidity_alerts") or []:
        if isinstance(a, dict) and in_band(num(a.get("alert_price") or a.get("price_level"))):
            px = fmt(num(a.get("alert_price") or a.get("price_level")))
            alerts.append({"timeframe": a.get("timeframe", "H1"), "type": a.get("type", "SWING"),
                           "lps": a.get("lps", 0), "importance": str(a.get("importance", "MEDIUM")).upper(),
                           "alert_price": px, "price_level": px, "alert_trigger": a.get("alert_trigger", ""),
                           "expected_pda_type_after_sweep": a.get("expected_pda_type_after_sweep"),
                           "reason": str(a.get("reason", "")), "label": a.get("label", "")})
    out = {**g, "strategic_bias": bias, "key_zones": zones, "liquidity_registry": registry,
           "active_causal_chain": acc, "final_lrlr_objective": fmt(final), "htf_liquidity_alerts": alerts,
           "po3_phase": g.get("po3_phase") or "Distribution",
           "target_liquidity": g.get("target_liquidity") or "external",
           "session_context": {"current_session": current_session(now),
                               "current_local_time": f"{fmt_ny(now)} NY / {fmt_cet(now)}",
                               "session_atr": fmt(s_atr), "intraday_reachability_filter": fmt(0.4 * s_atr)},
           "source": "gemini"}
    rng = dealing_range(d1)
    zone_keys = {z["id"]: f"{symbol}|{z['timeframe']}|{z['zone_type']}|{z['zone_low']}-{z['zone_high']}|{bias}|gemini"
                 for z in zones}
    struct = json.dumps({"bias": bias, "zones": sorted(zone_keys.items()), "root": acc["root_liquidity_id"]},
                        sort_keys=True)
    meta = {"symbol": symbol, "created_at": int(now), "price": price, "decimals": dec, "source": "gemini",
            "zone_keys": zone_keys, "struct_hash": hashlib.sha256(struct.encode()).hexdigest()[:16],
            "dealing_range": {"high": rng.high, "low": rng.low, "eq": rng.eq}, "session_atr": s_atr,
            "warnings": warnings}
    meta["hash"] = hashlib.sha256(json.dumps(out, separators=(",", ":"), sort_keys=True).encode()).hexdigest()[:16]
    return MapResult(True, "gemini", out, meta)


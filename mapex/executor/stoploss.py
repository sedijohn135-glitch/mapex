"""GEM2 LAYER 6 — Fiduciary stop-loss mandate."""

from __future__ import annotations

import math
from dataclasses import dataclass

from mapex.core.primitives import Bar

LONG_WICK_ATR = 2.0  # GEM2 3B wick-CE alternative when the sweep wick exceeds 2 x ATR14(M5)
MIN_RISK_SPREADS = 4.0
MAX_RISK_SESSION_ATR = 0.5


class NoSetup(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class StopLoss:
    value: float
    anchor: float
    anchor_type: str  # SWEEP | EXTREME_WICK
    spread_buffer: float
    safety_buffer: float
    risk: float


def safety_buffer(spread: float, tick: float, symbol_min: float) -> float:
    """safety = max(ceil(spread / tick x 0.05) x tick, SL_SAFETY_BUFFER[symbol])."""
    return max(math.ceil(round(spread / tick * 0.05, 9)) * tick, symbol_min)


def fiduciary_sl(buy: bool, raid_bar: Bar, extreme: float, entry: float, spread: float, tick: float,
                 safety_min: float, atr_m5: float, trap_levels: list[float], trap_tol: float,
                 session_atr: float) -> StopLoss:
    # 3A sweep anchor = the sweep extreme; 3B long-wick alternative = CE of the raid wick
    anchor, anchor_type = extreme, "SWEEP"
    is_extreme_bar = (raid_bar.l == extreme) if buy else (raid_bar.h == extreme)
    wick = (raid_bar.body_bot - raid_bar.l) if buy else (raid_bar.h - raid_bar.body_top)
    if is_extreme_bar and atr_m5 > 0 and wick > LONG_WICK_ATR * atr_m5:
        anchor = (raid_bar.l + raid_bar.body_bot) / 2 if buy else (raid_bar.h + raid_bar.body_top) / 2
        anchor_type = "EXTREME_WICK"
    safety = safety_buffer(spread, tick, safety_min)
    buf = spread + safety
    sl = anchor - buf if buy else anchor + buf
    # 3D dangerous trap: a live pool (EQH/EQL, session H/L, PDH/PDL) sitting on the stop -> no-setup
    if any(abs(lv - sl) <= trap_tol for lv in trap_levels):
        raise NoSetup("dangerous_trap")
    risk = entry - sl if buy else sl - entry
    if risk <= 0 or risk < MIN_RISK_SPREADS * spread or risk > MAX_RISK_SESSION_ATR * session_atr:
        raise NoSetup("risk_out_of_bounds")
    return StopLoss(sl, anchor, anchor_type, buf, safety, risk)

"""GEM2 LAYER 5 — take-profit ladder: 3R minimum, reachability, SD projection, guardrails."""

from __future__ import annotations

from dataclasses import dataclass

from mapex.executor.stoploss import NoSetup

TP1_MIN_R, TP2_MIN_R, TP3_MIN_R = 3.0, 3.5, 4.5
REACH_SESSION_ATR = 0.4
MIN_TP_SPREADS = 10.0
SD_TP1, SD_TP2, SD_TP3 = 2.0, 2.5, 4.0


@dataclass(frozen=True)
class Ladder:
    tp1: float
    tp1_r: float
    tp1_evidence: str
    tp2: float | None
    tp2_r: float | None
    tp2_evidence: str | None
    tp3: float | None
    tp3_r: float | None
    management_level: float | None

    @property
    def server_tp(self) -> float:
        return self.tp2 if self.tp2 is not None else self.tp1


def ladder(buy: bool, entry: float, risk: float, spread: float, session_atr: float,
           objectives: list[tuple[float, str]], raid_extreme: float, disp_extreme: float,
           final_lrlr: float | None) -> Ladder:
    sgn = 1 if buy else -1

    def dist(level: float) -> float:
        return sgn * (level - entry)

    def r_of(level: float) -> float:
        return dist(level) / risk

    reach = REACH_SESSION_ATR * session_atr
    min_dist = MIN_TP_SPREADS * spread
    ahead = sorted(((lv, why) for lv, why in objectives if dist(lv) > 0), key=lambda x: dist(x[0]))
    tp1 = mgmt = None
    for lv, why in ahead:
        if r_of(lv) >= TP1_MIN_R:
            if dist(lv) <= reach and dist(lv) >= min_dist:
                tp1 = (lv, why)
            break  # the first >= 3R objective decides; farther ones are beyond it
        if mgmt is None:
            mgmt = lv  # FTA < 3R: management only, never TP1
    leg = abs(disp_extreme - raid_extreme)
    sd = [(disp_extreme + sgn * k * leg, f"SD -{k}") for k in (SD_TP1, SD_TP2, SD_TP3)] if leg > 0 else []
    if tp1 is None and sd:
        lv, why = sd[0]
        if r_of(lv) >= TP1_MIN_R and min_dist <= dist(lv) <= reach:
            tp1 = (lv, f"{why} projection of the manipulation leg")
    if tp1 is None:
        raise NoSetup("tp_constraints_not_met")
    # TP2: next liquidity objective beyond TP1 with >= 3.5R; the SD -2.5 projection only when none exists
    beyond = [(lv, why) for lv, why in ahead if dist(lv) > dist(tp1[0])]
    tp2 = next(((lv, why) for lv, why in beyond if r_of(lv) >= TP2_MIN_R and dist(lv) >= min_dist), None)
    if tp2 is None:
        tp2 = next(((lv, why) for lv, why in sd[1:2] if dist(lv) > dist(tp1[0]) and r_of(lv) >= TP2_MIN_R), None)
    tp3 = None
    if final_lrlr is not None and r_of(final_lrlr) >= TP3_MIN_R and (tp2 is None or dist(final_lrlr) > dist(tp2[0])):
        tp3 = final_lrlr
    return Ladder(tp1[0], r_of(tp1[0]), tp1[1], tp2[0] if tp2 else None, r_of(tp2[0]) if tp2 else None,
                  tp2[1] if tp2 else None, tp3, r_of(tp3) if tp3 is not None else None, mgmt)

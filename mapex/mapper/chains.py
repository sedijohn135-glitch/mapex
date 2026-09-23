"""GEM1 STEP 7 — Causal chain builder + PDA scoring (Filters 1-2, 1A linkage, 1B confidence, 1C chains)."""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field

from mapex.core import levels as lv
from mapex.core import primitives as pr
from mapex.core.primitives import Bar, Zone
from mapex.mapper.liquidity import TF_RANK, Pool

CHAIN_KINDS = ("OB", "BB", "FVG", "IFVG", "RB")
TYPE_POINTS = {"OB": 12, "FVG": 12, "BB": 10, "IFVG": 10, "RB": 10, "VI": 7, "BPR": 7}
# 1C priority 4: OB > FVG > BB > IFVG > VI (RB placed after IFVG, DECISIONS D-10)
TYPE_RANK = {"OB": 6, "FVG": 5, "BB": 4, "IFVG": 3, "RB": 2, "BPR": 1, "VI": 1}
ZONE_TYPE = {"OB": "OB", "BB": "BB", "FVG": "FVG", "IFVG": "IFVG", "RB": "REJECTION_BLOCK"}
ANCHOR_TYPE = {"OB": "STRUCTURAL (CISD)", "BB": "STRUCTURAL (CISD)", "RB": "STRUCTURAL (CISD)",
               "FVG": "STRUCTURAL (FVG)", "IFVG": "STRUCTURAL (FVG)", "LIQUIDITY_POOL": "SWEEP"}


@dataclass
class Candidate:
    zone: Zone
    tf: str
    unmitigated: bool
    disp_quality: str
    disp_points: int
    gen: Pool | None = None
    sweep_type: str = "UNLINKED"
    breakdown: dict = field(default_factory=dict)
    chain: str | None = None

    @property
    def score(self) -> int:
        return sum(self.breakdown.values())

    @property
    def gen_lps(self) -> int:
        return self.gen.lps if self.gen else 0

    def order_key(self):
        """1C priority: generating LPS, displacement, generating TF, PDA type, unmitigated; then most recent."""
        return (-self.gen_lps, -self.disp_points, -TF_RANK[self.tf], -TYPE_RANK[self.zone.kind],
                -int(self.unmitigated), -self.zone.formed_at, self.zone.anchor)


def lps_points(lps: int) -> int:
    if lps >= 80:
        return 42
    if lps >= 65:
        return 35
    if lps >= 50:
        return 28
    return 15


def detect_pdas(bars: list[Bar], tf: str) -> dict[str, list[Zone]]:
    """All PD arrays of a timeframe (Steps 3/4), mitigation marked."""
    atrs = pr.atr_series(bars)
    breaks = pr.structure_breaks(bars)
    f = pr.fvgs(bars, tf)
    obs = pr.order_blocks(bars, atrs, breaks, tf)
    out = {"FVG": f, "OB": obs, "BB": pr.breaker_blocks(bars, atrs, obs, tf), "RB": pr.rejection_blocks(bars, atrs, tf),
           "IFVG": pr.ifvgs(bars, f), "BPR": pr.bprs(bars, f), "VI": pr.volume_imbalances(bars, tf)}
    for kind in ("FVG", "OB", "BB", "RB", "VI"):
        for z in out[kind]:
            z.tf = tf
            pr.mark_mitigation(z, bars)
    for z in out["IFVG"] + out["BPR"]:
        z.tf = tf
    return out


def active_htf_zones(pdas: dict[str, dict[str, list[Zone]]]) -> list[Zone]:
    """Unmitigated D1/H4 PD arrays — the '+5 inside an active HTF PDA' bonus reference."""
    out = []
    for tf in ("D1", "H4"):
        for kind in ("OB", "FVG", "BB", "RB"):
            out += [z for z in pdas.get(tf, {}).get(kind, []) if z.mitigated_at is None]
        out += pdas.get(tf, {}).get("IFVG", [])
    return out


def _reached_before(p: Pool, bars: list[Bar], k: int, tol: float) -> bool:
    """True when a bar between the pool's formation and bar k already took it (then bar k is no sweep)."""
    start = bisect.bisect_left(bars, p.avail_t, key=lambda b: b.t)
    for b in bars[start:k]:
        if (p.side == "low" and b.l <= p.price + tol) or (p.side == "high" and b.h >= p.price - tol):
            return True
    return False


def link(zone: Zone, bars: list[Bar], registry: list[Pool], price: float) -> tuple[Pool | None, str]:
    """1A: highest-LPS registry pool swept in the 2 bars immediately before the displacement."""
    if zone.disp_idx is None:
        return None, "UNLINKED"
    side = "low" if zone.direction == "buy" else "high"
    tol = lv.TOUCH_TOL_PCT * price
    best: tuple | None = None
    for k in (zone.disp_idx - 1, zone.disp_idx - 2):
        if k < 0:
            continue
        b = bars[k]
        for p in registry:
            if p.side != side or p.avail_t > b.t:
                continue
            if side == "low":
                swept, body = b.l <= p.price + tol, b.c < p.price
            else:
                swept, body = b.h >= p.price - tol, b.c > p.price
            if swept and not _reached_before(p, bars, k, tol):
                key = (p.lps, int(body), -int(p.id[4:]))
                if best is None or key > best[0]:
                    best = (key, p, "SWEPT_BODY" if body else "SWEPT_WICK")
    if best is None:
        return None, "UNLINKED"
    return best[1], best[2]


def candidates(pdas: dict[str, dict[str, list[Zone]]], bars: dict[str, list[Bar]], registry: list[Pool],
               bias: str, price: float, lth: str, ith: str) -> tuple[list[Candidate], list[Candidate]]:
    """Linked and UNLINKED candidates in the bias direction that pass Filter 1 (displacement)."""
    linked, unlinked = [], []
    aligned = {"buy": "bullish", "sell": "bearish"}[bias] in (lth, ith)
    for tf in ("D1", "H4", "H1"):
        tfb = bars.get(tf, [])
        if len(tfb) < 5:
            continue
        atrs = pr.atr_series(tfb)
        for kind in CHAIN_KINDS:
            for z in pdas[tf][kind]:
                if z.direction != bias:
                    continue
                unmit = z.mitigated_at is None
                if kind != "IFVG" and not unmit:
                    continue
                d = z.disp_idx
                if d is None or not pr.is_displacement(tfb, d, atrs[d]):
                    continue  # Filter 1: wick-only / weak breaks rejected
                q, qpts = pr.displacement_quality(tfb, d, atrs[d])
                c = Candidate(z, tf, unmit and kind != "IFVG", q, qpts)
                c.gen, c.sweep_type = link(z, tfb, registry, price)
                c.breakdown = {
                    "lps_contribution": lps_points(c.gen_lps) if c.gen else 0,
                    "sweep_type": {"SWEPT_BODY": 15, "SWEPT_WICK": 8}.get(c.sweep_type, 0),
                    "displacement_quality": qpts,
                    "unmitigated": 15 if c.unmitigated else 0,
                    "pda_type": TYPE_POINTS[kind],
                    "htf_alignment": 3 if aligned else 0,
                }
                (linked if c.gen else unlinked).append(c)
    return linked, unlinked


def assign_chains(cands: list[Candidate], gates: tuple[int, int, int]) -> dict[str, Candidate]:
    """1C: CHAIN_A (gate 65, never an LPS<50 root), CHAIN_B (50), CHAIN_C (40, only if structurally distinct)."""
    ordered = sorted(cands, key=Candidate.order_key)
    chains: dict[str, Candidate] = {}
    used: set[int] = set()
    a = next((c for c in ordered if c.gen_lps >= max(gates[0], 50)), None)
    if a:
        chains["CHAIN_A"], a.chain = a, "A"
        used.add(id(a))
    b = next((c for c in ordered if id(c) not in used and c.gen_lps >= gates[1]), None)
    if b:
        chains["CHAIN_B"], b.chain = b, "B"
        used.add(id(b))
    if len(chains) < 2:
        return chains  # C is a *third* zone
    prior = list(chains.values())
    for c in ordered:
        if id(c) in used or c.gen_lps < gates[2]:
            continue
        if all(c.tf != p.tf or c.gen.id != p.gen.id for p in prior):
            chains["CHAIN_C"], c.chain = c, "C"
            break
    return chains

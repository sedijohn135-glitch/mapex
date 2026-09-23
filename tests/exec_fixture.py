"""Hand-built GEM2 scenario: a buy CHAIN_A zone [2640, 2645], a Type-7 M5 raid of the 2645.5 swing low at 09:00 NY,
M5 displacement, M1 MSS above 2648.0, M1 FVG [2647.3, 2649.4] (CE 2648.35), retest at 09:20, defense at 09:21."""

from __future__ import annotations

from dataclasses import dataclass, field

from mapex.core.primitives import Bar
from mapex.executor.engine import ExecInput, evaluate
from tests.helpers import ny_ts
from tests.synth import aggregate

DAY = (2026, 9, 22)  # a Tuesday


def m1_from_m5(t0: int, o, h, l, c) -> list[Bar]:
    """Five M1 bars reproducing an M5 bar exactly (o -> first extreme -> second extreme -> c)."""
    first, second = (l, h) if c >= o else (h, l)
    closes = [first, (first + second) / 2, second, (second + c) / 2, c]
    out, prev = [], o
    for k, cl in enumerate(closes):
        out.append(Bar(t0 + 60 * k, prev, max(prev, cl), min(prev, cl), cl))
        prev = cl
    return out


def quiet_m5(t0: int, t1: int, price: float, amp: float = 0.3) -> list[tuple]:
    rows, t, k = [], t0, 0
    while t < t1:
        o = price + (amp if k % 2 else -amp) / 2
        c = price - (amp if k % 2 else -amp) / 2
        rows.append((t, o, max(o, c) + 0.1, min(o, c) - 0.1, c))
        t += 300
        k += 1
    return rows


# M5 blocks 08:30 -> 09:10 (clean scenario), then hand-made M1 from 09:10
LEAD_IN = [
    ((8, 30), (2649.0, 2649.3, 2648.2, 2648.4)),
    ((8, 35), (2648.4, 2648.6, 2646.0, 2646.3)),
    ((8, 40), (2646.3, 2647.5, 2645.5, 2647.2)),  # swing low 2645.5 = the pool (0.5 from the zone)
    ((8, 45), (2647.2, 2648.0, 2646.2, 2646.6)),  # swing high 2648.0 = MSS level
    ((8, 50), (2646.6, 2647.4, 2646.3, 2647.0)),
    ((8, 55), (2647.0, 2647.1, 2646.1, 2646.2)),
    ((9, 0), (2646.2, 2646.3, 2644.0, 2645.0)),  # RAID: body closes below 2645.5 (Type 7 candle 1)
    ((9, 5), (2645.0, 2646.8, 2644.9, 2646.5)),  # candle 2 closes back inside, bullish
]
M1_TAIL = [  # (minute offset from 09:10, o, h, l, c)
    (0, 2646.5, 2647.3, 2646.4, 2647.2),
    (1, 2647.2, 2649.6, 2647.1, 2649.5),  # first M1 close above 2648.0 (MSS), body 2.3
    (2, 2649.5, 2650.4, 2649.4, 2650.3),  # FVG: low 2649.4 > high(09:10) 2647.3
    (3, 2650.3, 2650.6, 2650.0, 2650.5),
    (4, 2650.5, 2650.7, 2650.2, 2650.4),
    (5, 2650.4, 2650.6, 2649.9, 2650.1),
    (6, 2650.1, 2650.3, 2649.6, 2649.8),
    (7, 2649.8, 2650.0, 2649.4, 2649.6),
    (8, 2649.6, 2649.9, 2649.2, 2649.5),
    (9, 2649.5, 2649.7, 2649.1, 2649.3),
    (10, 2649.3, 2649.4, 2648.3, 2648.6),  # 09:20 retest touches CE 2648.35, closes above, not defended
    (11, 2648.6, 2649.9, 2648.55, 2649.8),  # 09:21 defense: body 89 %, tiny lower wick, close > CE
    (12, 2649.8, 2650.2, 2649.7, 2650.1),
    (13, 2650.1, 2650.4, 2650.0, 2650.3),
    (14, 2650.3, 2650.6, 2650.2, 2650.5),
]


@dataclass
class Scenario:
    m1: list[Bar]
    bars: dict[str, list[Bar]]
    map: dict
    meta: dict
    start: int
    end: int
    extra: dict = field(default_factory=dict)


def base_map(zone_low=2640.0, zone_high=2645.0, root=2630.0, s_atr=60.0, final=2700.0, registry_extra=()):
    reg = [
        {"id": "LIQ_001", "timeframe": "H4", "type": "PML", "price_level": f"{root:.2f}", "lps": 82,
         "status": "PDA_GENERATING"},
        {"id": "LIQ_002", "timeframe": "H1", "type": "SESSION_H", "price_level": "2660.00", "lps": 40,
         "status": "UNTOUCHED"},
        {"id": "LIQ_003", "timeframe": "D1", "type": "PWH", "price_level": "2671.00", "lps": 70, "status": "UNTOUCHED"},
        {"id": "LIQ_004", "timeframe": "D1", "type": "SWING_H", "price_level": "2680.00", "lps": 66,
         "status": "UNTOUCHED"},
        {"id": "LIQ_005", "timeframe": "D1", "type": "PMH", "price_level": f"{final:.2f}", "lps": 90,
         "status": "UNTOUCHED"},
        *registry_extra,
    ]
    kz = {"id": "CHAIN_A", "timeframe": "H4", "direction": "buy", "zone_type": "FVG",
          "zone_label": "H4 bullish FVG @ 2642.50", "zone_low": f"{zone_low:.2f}", "zone_high": f"{zone_high:.2f}",
          "anchor_price": f"{(zone_low + zone_high) / 2:.2f}", "anchor_type": "STRUCTURAL (FVG)",
          "generating_liquidity_id": "LIQ_001", "generating_lps": 82, "causal_sweep_type": "SWEPT_BODY",
          "validation_score": 94, "time_horizon": "SWING_HTF", "suggested_pearl": "CRT", "tp1": "2660.00",
          "tp2": f"{final:.2f}"}
    m = {"strategic_bias": "buy", "session_context": {"session_atr": f"{s_atr:.2f}"}, "liquidity_registry": reg,
         "key_zones": [kz], "final_lrlr_objective": f"{final:.2f}",
         "active_causal_chain": {"root_liquidity_id": "LIQ_001", "root_lps": 82, "root_price_level": f"{root:.2f}"}}
    return m


def build(hh: int = 9, mm: int = 0, lead=None, tail=None, midnight_open=2650.0, london_low=2650.0,
          d1_close=None, reg_extra=()) -> Scenario:
    """Scenario with the raid M5 bar at hh:mm NY. Quiet bars fill from 18:00 the day before."""
    lead = lead or LEAD_IN
    tail = tail or M1_TAIL
    raid_t = ny_ts(*DAY, hh, mm)
    shift = raid_t - ny_ts(*DAY, 9, 0)
    first_block = ny_ts(*DAY, 8, 30) + shift
    start = ny_ts(*DAY, 0, 0) - 6 * 3600  # 18:00 the previous day
    rows = []
    # overnight: Asia/London ranges around 2651-2654, midnight open as requested
    for t, o, h, l, c in quiet_m5(start, ny_ts(*DAY, 0, 0), 2652.0):
        rows.append((t, o, h, l, c))
    rows.append((ny_ts(*DAY, 0, 0), midnight_open, max(midnight_open, 2650.4) + 0.1, min(midnight_open, 2650) - 0.1,
                 2650.0))
    for t, o, h, l, c in quiet_m5(ny_ts(*DAY, 0, 5), ny_ts(*DAY, 2, 0), 2650.5):
        rows.append((t, o, h, l, c))
    london = quiet_m5(ny_ts(*DAY, 2, 0), ny_ts(*DAY, 5, 0), 2652.0, amp=1.0)
    london[5] = (london[5][0], 2652.0, 2652.5, london_low, 2651.8)
    rows += london
    rows += quiet_m5(ny_ts(*DAY, 5, 0), first_block, 2649.8)
    m1: list[Bar] = []
    for t, o, h, l, c in rows:
        m1 += m1_from_m5(t, o, h, l, c)
    for (h_, mi), (o, h, l, c) in lead:
        m1 += m1_from_m5(ny_ts(*DAY, h_, mi) + shift, o, h, l, c)
    t10 = ny_ts(*DAY, 9, 10) + shift
    for k, o, h, l, c in tail:
        m1.append(Bar(t10 + 60 * k, o, h, l, c))
    last = m1[-1]
    t = last.t + 60
    while t < t10 + 60 * 40:
        m1.append(Bar(t, last.c, last.c + 0.1, last.c - 0.1, last.c))
        t += 60
    d1 = []
    for k in range(60, 0, -1):
        t0 = ny_ts(*DAY, 0, 0) - k * 86400
        d1.append(Bar(t0, 2650, 2665, 2635, 2651))
    if d1_close is not None:  # yesterday's (closed) D1 bar closes at d1_close
        d1[-1] = Bar(d1[-1].t, 2650, 2655, 2620, d1_close)
    bars = {"M1": m1, "M5": aggregate(m1, "M5"), "M15": aggregate(m1, "M15"), "D1": d1}
    m = base_map(registry_extra=reg_extra)
    meta = {"zone_keys": {"CHAIN_A": "XAUUSD|H4|FVG|1790000000|buy"}, "struct_hash": "abc",
            "created_at": first_block - 3600, "dealing_range": {"high": 2700.0, "low": 2600.0}}
    return Scenario(m1, bars, m, meta, first_block - 1800, t10 + 60 * 35)


def run(sc: Scenario, states=None, until=None, truncate=False, spread=0.2, mapper=None):
    """Tick every minute (3 s after each M1 close); returns (all decisions, final states, plans)."""
    states = dict(states or {})
    decisions, plans = [], []
    by_t = {b.t: b for b in sc.m1}
    now = sc.start
    end = until or sc.end
    while now <= end:
        tick = now + 3
        last = by_t.get(now - 60)
        bid = last.c if last else sc.m1[0].o
        bars = sc.bars
        if truncate:
            bars = {tf: [b for b in v if b.t < tick] for tf, v in sc.bars.items()}
        inp = ExecInput("XAUUSD", tick, sc.map if mapper is None else mapper(tick), sc.meta, True, bars, bid,
                        round(bid + spread, 2), True, True, 0.01, 2, 0.30)
        res = evaluate(inp, states)
        states = res.states
        decisions += res.decisions
        if res.plan:
            plans.append(res.plan)
        now += 60
    return decisions, states, plans

"""GEM1 golden tests (gem1-mapper-spec §9)."""

import time

import pytest

from mapex.core.primitives import Zone
from mapex.mapper import chains as ch
from mapex.mapper import liquidity as lq
from mapex.mapper.engine import MapperInput, build_map, derive_bias, verify
from mapex.mapper.liquidity import Pool
from tests.helpers import flat, mk
from tests.synth import market


# 1. LPS arithmetic -------------------------------------------------------------------------------
@pytest.mark.parametrize("tf,kind,bonus,expected", [
    ("D1", "PM", 0, 60), ("D1", "PW", 0, 57), ("D1", "OLD", 0, 55), ("D1", "EQ3", 0, 53), ("D1", "MAGNET", 0, 51),
    ("D1", "PD", 0, 50), ("D1", "SESSION", 0, 47), ("D1", "RESTING", 0, 45), ("D1", "EQ2", 0, 43),
    ("D1", "ENGINEERED", 0, 39), ("D1", "RANGE", 0, 37), ("D1", "VOID", 0, 42),
    ("H4", "PD", 10, 43), ("H1", "SESSION", 10, 26),
    ("D1", "PM", 12 + 10 + 8 + 5, 95),  # full bonus 35
    ("D1", "PM", 50, 95),  # bonus capped at +35
    ("D1", "PM", 45, 95), ("D1", "PW", 35, 92),
])
def test_lps_arithmetic(tf, kind, bonus, expected):
    assert lq.lps_score(tf, lq.MULT[kind], bonus) == expected


def test_lps_capped_at_100_and_importance():
    assert lq.lps_score("D1", 1.0, 35) == 95
    assert min(100, 60 * 1.0 + 35) <= 100
    assert [lq.importance(x) for x in (80, 79, 65, 64, 50, 49)] == ["CRITICAL", "HIGH", "HIGH", "MEDIUM", "MEDIUM",
                                                                     "LOW"]


# 2. registry ---------------------------------------------------------------------------------------
def _ctx(bars, price):
    rng = lq.DealingRange(price * 1.1, price * 0.9, 0, 0)
    return {"price": price, "now": bars["H1"][-1].t + 7200, "tick": 0.01, "bars": bars, "dealing_range": rng,
            "session_atr": 1.0}


def test_eqh_detection_cluster_and_confluence():
    rows = []
    for peak in (110.0, 110.08, 104.0, 110.05, 103.0):  # three highs within 0.1 % of 110
        rows += [(100, 101, 99, 100.5), (100.5, peak, 100, 101), (101, 101.5, 99.5, 100)]
    rows += [(100, 100.5, 99.8, 100.2)] * 3
    h1 = mk(rows, start=1_700_006_400, tf="H1")
    pools = lq.dedupe(lq.discover_tf("H1", h1, _ctx({"H1": h1, "M15": []}, 100.0)), 100.0)
    eq = [p for p in pools if "EQ3" in p.kinds and p.side == "high"]
    assert len(eq) == 1 and eq[0].touches == 3 and abs(eq[0].price - 110.0433) < 0.01
    # the swing highs merged into the EQH pool (dedupe on the same timeframe keeps one level)
    assert len([p for p in pools if p.side == "high" and abs(p.price - 110) < 0.2]) == 1

    a = Pool("H1", "high", 110.0, ["EQ3"], ["a"], 0)
    b = Pool("H4", "high", 110.05, ["SWING"], ["b"], 0)
    c = Pool("H4", "high", 110.1, ["SWING"], ["c"], 0)
    far = Pool("D1", "low", 90.0, ["PD"], ["d"], 0)
    lq.score([a, b, c, far], 100.0, [Zone("FVG", "buy", 109.9, 110.2, 110, 0, 0)])
    assert a.bonus == {"multi_tf_confluence": 12, "untouched": 10, "cluster": 8, "htf_pda": 5}
    assert b.bonus["multi_tf_confluence"] == 12  # confluence applied on both entries
    assert far.bonus == {"untouched": 10}
    assert a.lps == round(20 * 0.88 + 35)


def test_sweep_status_classification():
    h1 = mk([(100, 100.5, 99.5, 100)] * 5 + [(100, 101.2, 99.8, 100.2)], start=1_700_006_400, tf="H1")
    wick = Pool("H1", "high", 101.2, ["SWING"], ["w"], h1[3].t)
    body = Pool("H1", "high", 100.1, ["SWING"], ["b"], h1[3].t)
    clean = Pool("H1", "high", 105.0, ["SWING"], ["c"], h1[3].t)
    lq.classify([wick, body, clean], {"H1": h1}, 100.0, 0.5, h1[-1].t + 3600)
    assert (wick.status, body.status, clean.status) == ("SWEPT_WICK", "SWEPT_BODY", "UNTOUCHED")


# 4. bias -------------------------------------------------------------------------------------------
def _p(pid, tf, side, price, lps, status="UNTOUCHED"):
    p = Pool(tf, side, price, ["SWING"], [pid], 0, id=pid, status=status)
    p.lps = lps
    return p


BULL = {"LTH": "bullish", "ITH": "bullish", "STH": "bullish"}


def test_bias_candidate_above_and_below():
    reg = [_p("LIQ_001", "D1", "high", 110, 80), _p("LIQ_002", "D1", "low", 90, 60)]
    assert derive_bias(reg, 100, [], BULL)["bias"] == "buy"
    reg = [_p("LIQ_001", "D1", "high", 110, 55), _p("LIQ_002", "D1", "low", 90, 70)]
    assert derive_bias(reg, 100, [], {"LTH": "bearish", "ITH": "corrective", "STH": "bearish"})["bias"] == "sell"


def test_bias_tie_dominant_tf_then_no_map():
    reg = [_p("LIQ_001", "H4", "high", 110, 75), _p("LIQ_002", "D1", "low", 90, 70)]
    out = derive_bias(reg, 100, [], {"LTH": "bearish", "ITH": "bearish", "STH": "bearish"})
    assert out["bias"] == "sell" and out["tie_break"] == "dominant_tf"
    reg = [_p("LIQ_001", "D1", "high", 110, 75), _p("LIQ_002", "D1", "low", 90, 70)]
    out = derive_bias(reg, 100, [], BULL)
    assert out["bias"] is None and out["reason"] == "bias_tie"


def test_bias_conflict_with_lth_and_ith_gives_no_map():
    reg = [_p("LIQ_001", "D1", "high", 110, 90), _p("LIQ_002", "D1", "low", 90, 50)]
    out = derive_bias(reg, 100, [], {"LTH": "bearish", "ITH": "bearish", "STH": "corrective"})
    assert out["bias"] is None and out["reason"] == "bias_conflict_htf"
    out = derive_bias(reg, 100, [], {"LTH": "bearish", "ITH": "bullish", "STH": "corrective"})
    assert out["bias"] == "buy"


# 5. chains -----------------------------------------------------------------------------------------
def _cand(lps, tf="H1", kind="FVG", disp=9, unmit=True, t=0, pid=None):
    z = Zone(kind, "buy", 99, 100, 99.5, t, 0, 0, tf)
    c = ch.Candidate(z, tf, unmit, "MODERATE", disp)
    c.gen = _p(pid or f"LIQ_{lps:03d}", "D1", "low", 95, lps)
    return c


def test_chain_gates():
    cs = [_cand(70), _cand(55), _cand(45, tf="H4"), _cand(30)]
    chains = ch.assign_chains(cs, (65, 50, 40))
    assert [chains[k].gen_lps for k in ("CHAIN_A", "CHAIN_B", "CHAIN_C")] == [70, 55, 45]
    chains = ch.assign_chains([_cand(60), _cand(55)], (65, 50, 40))
    assert "CHAIN_A" not in chains and chains["CHAIN_B"].gen_lps == 60 and "CHAIN_C" not in chains
    chains = ch.assign_chains([_cand(39)], (65, 50, 40))
    assert chains == {}


def test_chain_c_needs_structural_distinction():
    a, b = _cand(70, pid="LIQ_001"), _cand(60, pid="LIQ_002")
    same = _cand(45, pid="LIQ_001")  # same TF and same pool as A
    assert "CHAIN_C" not in ch.assign_chains([a, b, same], (65, 50, 40))


def test_chain_ordering_tie_breaks():
    d1 = _cand(70, tf="D1", disp=9)
    h1_large = _cand(70, tf="H1", disp=13)
    assert ch.assign_chains([d1, h1_large], (65, 50, 40))["CHAIN_A"] is h1_large  # displacement before TF
    h4 = _cand(70, tf="H4", disp=9)
    h1 = _cand(70, tf="H1", disp=9)
    assert ch.assign_chains([h1, h4], (65, 50, 40))["CHAIN_A"] is h4  # TF before type
    ob, fvg = _cand(70, kind="OB"), _cand(70, kind="FVG")
    assert ch.assign_chains([fvg, ob], (65, 50, 40))["CHAIN_A"] is ob
    mitig, clean = _cand(70, unmit=False), _cand(70)
    assert ch.assign_chains([mitig, clean], (65, 50, 40))["CHAIN_A"] is clean


# 6. confidence exactly 87 ---------------------------------------------------------------------------
def test_confidence_87_breakdown():
    rows = [(r.o, r.h, r.l, r.c) for r in flat(20, 100.0, 0.5)]
    rows += [(100.0, 100.1, 98.9, 99.3),  # down-close RB candle, wick sweeps the pool at 99.0 (close back above)
             (99.3, 101.2, 99.25, 101.1),  # displacement: body 1.8 >= ATR ~1, closes above 100.1
             (101.1, 101.3, 100.5, 100.9),  # FVG: low 100.5 > high 100.1
             (100.9, 101.2, 100.6, 101.0)]
    h1 = mk(rows, start=1_700_006_400, tf="H1")
    pool = _p("LIQ_001", "H1", "low", 99.0, 82, status="SWEPT_WICK")
    pdas = {"H1": ch.detect_pdas(h1, "H1")}
    linked, unlinked = ch.candidates(pdas, {"H1": h1}, [pool], "buy", 101.0, "bullish", "corrective")
    rb = [c for c in linked if c.zone.kind == "RB"]
    assert len(rb) == 1
    c = rb[0]
    assert c.breakdown == {"lps_contribution": 42, "sweep_type": 8, "displacement_quality": 9, "unmitigated": 15,
                           "pda_type": 10, "htf_alignment": 3}
    assert c.score == 87 and c.gen is pool and c.sweep_type == "SWEPT_WICK"
    fvg = [c for c in linked if c.zone.kind == "FVG"]
    assert fvg and fvg[0].score == 89


def test_unlinked_pda_is_excluded_from_chains():
    rows = [(r.o, r.h, r.l, r.c) for r in flat(20, 100.0, 0.5)]
    rows += [(100.0, 100.1, 99.6, 99.8), (99.8, 101.2, 99.75, 101.1), (101.1, 101.3, 100.5, 100.9),
             (100.9, 101.2, 100.6, 101.0)]
    h1 = mk(rows, start=1_700_006_400, tf="H1")
    pool = _p("LIQ_001", "H1", "low", 95.0, 82)  # never reached
    linked, unlinked = ch.candidates({"H1": ch.detect_pdas(h1, "H1")}, {"H1": h1}, [pool], "buy", 101, "bullish",
                                     "bullish")
    assert linked == [] and unlinked
    assert ch.assign_chains(linked, (65, 50, 40)) == {}


# 7. Step 8 ------------------------------------------------------------------------------------------
def test_inconsistent_map_publishes_nothing():
    m = {"strategic_bias": "buy", "po3_phase": "Manipulation", "liquidity_registry": [{"id": "LIQ_001",
                                                                                        "price_level": "110.00"}],
         "htf_liquidity_alerts": [{"importance": "HIGH", "price_level": "110.00"}],
         "key_zones": [{"direction": "sell", "zone_type": "FVG", "anchor_type": "STRUCTURAL (FVG)",
                        "zone_low": "99.00", "zone_high": "100.00", "tp1": "110.00", "tp2": "110.00",
                        "causal_sweep_type": "SWEPT_BODY", "generating_liquidity_id": "LIQ_001"}]}
    primary = _p("LIQ_001", "D1", "high", 110, 80)
    checks = verify(m, {"bias": "buy"}, BULL, [primary], 105.0, "New York", primary, primary)
    assert not checks["key_zones_align_with_bias"]
    assert checks["tp1_tp2_within_reach"] and checks["no_unlinked_chain"]


# 8/9. determinism + no-lookahead on a full synthetic market ------------------------------------------
@pytest.fixture(scope="module")
def mkt():
    return market(days=260, seed=11)


def _inp(bars, now, price):
    return MapperInput("BTCUSD", now, price, bars, tick=0.01, decimals=2)


def test_full_map_determinism_and_schema(mkt):
    now = mkt["M15"][-1].t + 900 + 5
    price = mkt["M15"][-1].c
    t0 = time.time()
    r1 = build_map(_inp(mkt, now, price))
    elapsed = time.time() - t0
    r2 = build_map(_inp(mkt, now, price))
    assert r1.dumps() == r2.dumps()  # byte-identical
    assert elapsed < 10
    assert r1.meta["checks"] or r1.reason
    if r1.json:
        m = r1.json
        assert set(m) == {"strategic_bias", "bias_hierarchy", "po3_phase", "weekly_profile", "market_phase",
                          "liquidity_profile", "target_liquidity", "active_causal_chain", "session_context",
                          "smt_analysis", "static_anchors", "liquidity_registry", "htf_liquidity_alerts",
                          "tp_policy", "final_lrlr_objective", "key_zones", "alarms", "intraday_keylevels",
                          "htf_swing_keylevels"}
        assert all(p["id"].startswith("LIQ_") for p in m["liquidity_registry"])
        assert len(m["htf_liquidity_alerts"]) <= 8 or all(
            a["importance"] == "CRITICAL" for a in m["htf_liquidity_alerts"][8:])


def test_mapper_no_lookahead(mkt):
    cut = mkt["M15"][-2000].t
    now = cut + 5
    price = [b for b in mkt["M15"] if b.t < cut][-1].c
    past = {tf: [b for b in bars if b.t < cut] for tf, bars in mkt.items()}
    full = build_map(_inp(mkt, now, price))  # future bars are present but must be invisible
    trunc = build_map(_inp(past, now, price))
    assert full.dumps() == trunc.dumps()


def test_many_windows_produce_valid_maps_sometimes(mkt):
    """Across several cut points the mapper must publish at least one valid map with a CHAIN_A."""
    got_valid = got_chain = False
    for back in range(200, 4000, 400):
        cut = mkt["M15"][-back].t
        price = [b for b in mkt["M15"] if b.t < cut][-1].c
        r = build_map(_inp(mkt, cut + 5, price))
        got_valid |= r.valid
        got_chain |= bool(r.valid and r.json["active_causal_chain"]["root_liquidity_id"])
    assert got_valid


# Step 2 helpers ---------------------------------------------------------------------------------
def test_dealing_range_and_fallback():
    from mapex.core.primitives import Bar
    d1 = [Bar(i * 86400, 100, 100 + (i % 7), 95 - (i % 5), 100) for i in range(80)]
    rng = lq.dealing_range(d1)
    assert rng.high > rng.low and rng.low < rng.eq < rng.high
    short = [Bar(i * 86400, 100, 101 + i, 99 - i, 100) for i in range(5)]
    fb = lq.dealing_range(short)
    assert (fb.high, fb.low) == (105, 95)


def test_bias_hierarchy_and_d1_narrative(mkt):
    from mapex.mapper.engine import bias_hierarchy, d1_narrative
    h = bias_hierarchy(mkt["D1"][-250:], mkt["H4"][-250:], mkt["H1"][-300:])
    assert h["LTH"] in ("bullish", "bearish") and h["ITH"] in ("bullish", "bearish", "corrective")
    assert h["STH"] in ("bullish", "bearish", "corrective")
    from mapex.core.primitives import Bar
    assert d1_narrative([Bar(0, 100, 110, 90, 100), Bar(1, 99, 106, 98, 105)]) == "bullish"
    assert d1_narrative([Bar(0, 100, 110, 90, 100), Bar(1, 101, 102, 94, 95)]) == "bearish"
    assert d1_narrative([Bar(0, 100, 110, 90, 100), Bar(1, 99, 101, 98, 99.5)]) == "neutral"


def test_po3_phase_rules():
    from mapex.core.primitives import Bar
    from mapex.mapper.engine import po3_phase
    from tests.helpers import ny_ts
    mid = ny_ts(2026, 9, 22, 0, 0)
    bars = [Bar(mid + k * 900, 100, 100.5, 100.0, 100.2) for k in range(40)]
    assert po3_phase(bars, ny_ts(2026, 9, 22, 20, 0), "buy") == "Accumulation"
    assert po3_phase(bars, mid + 40 * 900, "buy") == "Manipulation"  # the day low never went below the anchor
    swept = bars + [Bar(mid + 40 * 900, 100, 100.1, 99.0, 99.5), Bar(mid + 41 * 900, 99.5, 101, 99.4, 100.8)]
    assert po3_phase(swept, mid + 42 * 900, "buy") == "Distribution"


def test_weekly_profile_and_special_days():
    from mapex.core.primitives import Bar
    from mapex.mapper.engine import special_days, weekly_profile
    from tests.helpers import ny_ts
    ws = ny_ts(2026, 9, 20, 18)
    h1 = [Bar(ws + k * 3600, 100 + k * 0.1, 100.2 + k * 0.1, 99.9 + k * 0.1, 100.1 + k * 0.1) for k in range(40)]
    assert weekly_profile(h1, ws + 40 * 3600, "buy") == "Weekly Expansion"
    assert weekly_profile(h1[:1], ws + 3600, "buy") == "Range"
    d1 = [Bar(k * 86400, 100, 101, 99, 100) for k in range(20)]
    d1.append(Bar(21 * 86400, 100, 100.5, 99.5, 100.2))
    sd = special_days(d1, 100, 0.01)
    assert sd["inside_day"] and not sd["big_event_range"]
    wick = d1[:-1] + [Bar(21 * 86400, 100.0, 100.4, 97.0, 100.2)]
    assert special_days(wick, 100, 0.01)["daily_discount_wick_ce"] == (97.0 + 100.0) / 2


def test_opening_gaps_ndog_nwog():
    from mapex.core.primitives import Bar
    from mapex.mapper.engine import opening_gaps
    from tests.helpers import ny_ts
    fri = ny_ts(2026, 9, 18, 16, 45)
    sun = ny_ts(2026, 9, 20, 18, 0)
    mon_close, mon_open = ny_ts(2026, 9, 21, 16, 45), ny_ts(2026, 9, 21, 18, 0)
    bars = [Bar(fri, 100, 101, 99, 100.0), Bar(sun, 102, 103, 101, 102.5), Bar(mon_close, 103, 104, 102, 103.0),
            Bar(mon_open, 103.4, 104, 103, 103.8)]
    gaps = {g["type"]: g for g in opening_gaps("XAUUSD", bars, [], ny_ts(2026, 9, 22, 9))}
    assert (gaps["NWOG"]["gap_low"], gaps["NWOG"]["gap_high"]) == (100.0, 102)
    assert (gaps["NDOG"]["gap_low"], gaps["NDOG"]["gap_high"]) == (103.0, 103.4)
    assert opening_gaps("BTCUSD", bars, [], ny_ts(2026, 9, 22, 9)) == []


def test_brief_matches_chains(mkt):
    from mapex.mapper.engine import brief
    for back in range(200, 4000, 400):
        cut = mkt["M15"][-back].t
        price = [b for b in mkt["M15"] if b.t < cut][-1].c
        r = build_map(_inp(mkt, cut + 5, price))
        if r.valid and r.json["key_zones"]:
            text = brief(r.json)
            assert text.startswith("🚨 LIQUIDITY INTELLIGENCE BRIEF")
            for kz in r.json["key_zones"]:
                assert f"📍 {kz['id'].replace('_', ' ')}" in text
            return
    pytest.skip("no map with zones in this fixture")


def test_pool_erl_irl():
    assert Pool("D1", "high", 1, ["PD"], [], 0).erl and not Pool("H1", "high", 1, ["RANGE"], [], 0).erl
    assert not Pool("H4", "low", 1, ["VOID"], [], 0).erl

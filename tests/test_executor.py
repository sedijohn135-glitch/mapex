"""GEM2 golden scenarios (gem2-executor-spec §10)."""

import pytest

from mapex.core.primitives import Bar
from mapex.executor.engine import Plan, alpha_pick
from mapex.executor.state import ZoneState
from mapex.executor.stoploss import NoSetup, fiduciary_sl
from mapex.executor.targets import ladder
from tests.exec_fixture import LEAD_IN, M1_TAIL, build, run

KEY = "XAUUSD|H4|FVG|1790000000|buy"


def outputs(decisions):
    return [(d.output, d.reason) for d in decisions if d.output != "WATCH"]


def assert_all_confirmed_are_100(decisions):
    for d in decisions:
        if d.output == "CONFIRMED":
            assert sum(d.scores) == 100 and d.plan is not None
        else:
            assert d.plan is None


# 1. clean buy -------------------------------------------------------------------------------------
def test_clean_buy_exactly_one_order():
    decisions, states, plans = run(build())
    assert_all_confirmed_are_100(decisions)
    assert len(plans) == 1, outputs(decisions)
    p = plans[0]
    assert p.side == "buy" and p.chain == "CHAIN_A" and p.sweep_count == 1
    assert p.decision_price == 2650.0  # ask at the defense close
    assert p.sl == 2643.50  # sweep extreme 2644.0 - (spread 0.2 + safety 0.30)
    assert p.anchor_type == "SWEEP"
    assert (p.tp1, p.tp2, p.tp3, p.tp_server) == (2671.0, 2680.0, 2700.0, 2680.0)
    assert round(p.r_tp1, 2) == round(21 / 6.5, 2)
    assert p.format_b["contextual_confidence_score"]["total"] == 100
    assert states[KEY].state == "CONFIRMED" and states[KEY].sweep_count == 0
    waits = [d for d in decisions if d.output == "WAIT"]
    assert waits and waits[0].reason == "M1 body close ABOVE 2648.35"
    assert "CRT (Turtle Soup) Liquidity Sweep" not in p.strategy_used  # an M5 swing low is not an 'obvious' pool
    assert "Displacement + Return to Origin (DRO)" in p.strategy_used


# 2. wick-only raid -> RESET, then Type 7 -> entry ----------------------------------------------------
def test_wick_only_then_type7_entry():
    lead = [((8, 15), (2649.0, 2649.2, 2647.5, 2647.8)),
            ((8, 20), (2647.8, 2648.2, 2646.0, 2647.9)),  # swing low 2646.0 (near the zone)
            ((8, 25), (2647.9, 2649.3, 2647.6, 2649.0))] + LEAD_IN
    lead[3] = ((8, 30), (2649.0, 2649.3, 2648.2, 2648.4))
    lead[4] = ((8, 35), (2648.4, 2648.6, 2645.9, 2646.3))  # wick below 2646.0, close back above: WICK_ONLY
    decisions, states, plans = run(build(lead=lead))
    resets = [d for d in decisions if d.output == "RESET"]
    assert resets and "wick only" in resets[0].reason
    assert resets[0].data["sweep_count"] == 1 and resets[0].data["last_sweep_type"] == "WICK_ONLY"
    assert len(plans) == 1 and plans[0].sweep_count == 2


# 3. fake MSS ----------------------------------------------------------------------------------------
def test_fake_mss_resets():
    lead = LEAD_IN[:7] + [((9, 5), (2645.0, 2646.8, 2644.9, 2646.5))]
    tail = [(0, 2646.5, 2647.2, 2646.4, 2647.0), (1, 2647.0, 2647.9, 2646.9, 2647.8),
            (2, 2647.8, 2648.3, 2647.7, 2647.9),  # wick above 2648.0, close below: fake MSS
            (3, 2647.9, 2648.1, 2647.3, 2647.5), (4, 2647.5, 2647.9, 2647.4, 2647.8)]
    tail += [(k, 2647.6, 2647.8, 2647.4, 2647.6) for k in range(5, 30)]
    decisions, states, plans = run(build(lead=lead, tail=tail))
    assert plans == []
    assert any(d.output == "RESET" and ("Fake MSS" in d.reason or "no displacement" in d.reason) for d in decisions)


# 4. third sweep without displacement -> MONITOR -----------------------------------------------------
def test_third_sweep_monitor_blocks_everything():
    st = ZoneState(KEY, "XAUUSD", "CHAIN_A", "buy", sweep_count=2, first_seen=0)
    lead = LEAD_IN[:6] + [((9, 0), (2646.2, 2646.3, 2644.0, 2645.8))] + LEAD_IN[7:]  # wick-only raid
    sc = build(lead=lead)
    st.first_seen = sc.start
    st.ctx["seen"] = {tf: sc.start - 3600 for tf in ("M1", "M5", "M15")}
    decisions, states, plans = run(sc, states={KEY: st})
    assert plans == []
    mons = [d for d in decisions if d.output == "MONITOR"]
    assert mons and states[KEY].state == "MONITOR"
    after = decisions[decisions.index(mons[0]) + 1:]
    assert not any(d.output in ("WAIT", "CONFIRMED") for d in after)


# 5. anchor not defended -----------------------------------------------------------------------------
def test_anchor_not_defended_resets():
    tail = list(M1_TAIL)
    tail[10] = (10, 2649.3, 2649.4, 2648.0, 2648.1)  # body closes below CE 2648.35
    decisions, states, plans = run(build(tail=tail))
    assert plans == []
    assert any(d.output == "RESET" and d.reason == "anchor_not_defended" for d in decisions)
    assert states[KEY].sweep_count == 1 and states[KEY].state == "WATCH"


# 6. no retest within 15 M1 bars ---------------------------------------------------------------------
def _no_retest_tail():
    tail = list(M1_TAIL[:10])
    tail += [(k, 2650.0, 2650.3, 2649.8, 2650.1) for k in range(10, 30)]
    return tail


def test_no_retest_window_reset_then_monitor():
    decisions, states, plans = run(build(tail=_no_retest_tail()))
    assert plans == [] and any(d.output == "RESET" and d.reason == "no_retest_window" for d in decisions)
    sc = build(tail=_no_retest_tail())
    st = ZoneState(KEY, "XAUUSD", "CHAIN_A", "buy", sweep_count=2, first_seen=sc.start)
    st.ctx["seen"] = {tf: sc.start - 3600 for tf in ("M1", "M5", "M15")}
    decisions, states, plans = run(sc, states={KEY: st})
    assert plans == [] and any(d.output == "MONITOR" and "no_retest_window" in d.reason for d in decisions)


# 7. TP ladder ---------------------------------------------------------------------------------------
def test_tp_ladder_rules():
    objs = [(2412.0, "pool 2.4R"), (2416.0, "pool 3.2R"), (2425.0, "pool 5R")]
    lad = ladder(True, 2400.0, 5.0, 0.2, 60.0, objs, 2394.0, 2402.0, 2440.0)
    assert lad.management_level == 2412.0 and lad.tp1 == 2416.0 and lad.tp2 == 2425.0 and lad.tp3 == 2440.0
    with pytest.raises(NoSetup) as e:
        ladder(True, 2400.0, 5.0, 0.2, 20.0, objs, 2399.0, 2400.5, None)  # reach 8 < 3R
    assert e.value.reason == "tp_constraints_not_met"
    sd = ladder(True, 2400.0, 2.0, 0.1, 60.0, [], 2396.0, 2400.0, None)  # SD -2.0 = 2408 (4R)
    assert sd.tp1 == 2408.0 and "SD" in sd.tp1_evidence


# 8. dangerous trap ----------------------------------------------------------------------------------
def test_dangerous_trap_sl_on_pdl():
    raid = Bar(0, 2646.2, 2646.3, 2644.0, 2645.0)
    with pytest.raises(NoSetup) as e:
        fiduciary_sl(True, raid, 2644.0, 2650.0, 0.2, 0.01, 0.3, 1.5, [2643.6], 0.8, 60.0)
    assert e.value.reason == "dangerous_trap"
    sl = fiduciary_sl(True, raid, 2644.0, 2650.0, 0.2, 0.01, 0.3, 1.5, [2630.0], 0.8, 60.0)
    assert sl.value == pytest.approx(2643.5)
    extra = [{"id": "LIQ_009", "timeframe": "D1", "type": "PDL", "price_level": "2643.40", "lps": 50,
              "status": "UNTOUCHED"}]
    decisions, _, plans = run(build(reg_extra=extra))
    assert plans == [] and any(d.output == "NO-SETUP" and d.reason == "dangerous_trap" for d in decisions)


# 9. killzone gates ----------------------------------------------------------------------------------
def test_killzone_gates():
    _, _, plans = run(build(11, 30))
    assert plans == []
    decisions, _, plans = run(build(14, 0))  # retest/defense at ~14:21 NY, PM Silver Bullet
    assert len(plans) == 1 and "SILVER_BULLET" in plans[0].strategy_used


# 10. Judas ------------------------------------------------------------------------------------------
def test_judas_threshold_not_exceeded():
    decisions, _, plans = run(build(midnight_open=2641.0, london_low=2641.0))
    assert plans == []
    assert any(d.output == "NO-SETUP" and "judas" in d.reason for d in decisions)


# 11. thesis invalidation ----------------------------------------------------------------------------
def test_thesis_invalidated_by_d1_close_past_root():
    sc = build(d1_close=2625.0)  # root 2630
    st = ZoneState(KEY, "XAUUSD", "CHAIN_A", "buy", first_seen=sc.bars["D1"][-1].t)
    st.ctx["seen"] = {tf: sc.start for tf in ("M1", "M5", "M15")}
    decisions, states, plans = run(sc, states={KEY: st}, until=sc.start + 120)
    inv = [d for d in decisions if d.output == "INVALIDATED"]
    assert inv and inv[0].error_code == 3002 and states[KEY].state == "DEAD" and plans == []


def test_rerun_mapper_requested_on_invalidation():
    from mapex.executor.engine import ExecInput, evaluate
    sc = build(d1_close=2625.0)
    st = ZoneState(KEY, "XAUUSD", "CHAIN_A", "buy", first_seen=sc.bars["D1"][-1].t)
    res = evaluate(ExecInput("XAUUSD", sc.start + 3, sc.map, sc.meta, True, sc.bars, 2650.0, 2650.2), {KEY: st})
    assert res.rerun_mapper


# 12. alpha pick -------------------------------------------------------------------------------------
def _plan(chain, r, lps):
    return Plan("k" + chain, "XAUUSD", "buy", chain, "z" + chain, 1, 0, 3, None, None, 3, 1, r, None, None, "SWEEP",
                0.2, "PML", 1, lps, 1, "BODY_CLOSE", "", "", "", [], 0)


def test_alpha_pick():
    assert alpha_pick([_plan("CHAIN_A", 3.1, 90), _plan("CHAIN_B", 3.4, 60)]).chain == "CHAIN_B"
    assert alpha_pick([_plan("CHAIN_A", 3.1, 60), _plan("CHAIN_B", 3.1, 90)]).chain == "CHAIN_B"
    assert alpha_pick([_plan("CHAIN_B", 3.1, 60), _plan("CHAIN_A", 3.1, 60)]).chain == "CHAIN_A"


# 13. determinism + no-lookahead ---------------------------------------------------------------------
def _sig(decisions):
    return [(d.ts, d.zone_key, d.output, d.reason, d.scores) for d in decisions]


def test_determinism_and_no_lookahead():
    a, _, pa = run(build())
    b, _, pb = run(build())
    c, _, pc = run(build(), truncate=True)  # the engine only ever sees bars up to the tick
    assert _sig(a) == _sig(b) == _sig(c)
    assert [p.format_b for p in pa] == [p.format_b for p in pc]


def test_preflight_no_setup_without_touching_state():
    from mapex.executor.engine import ExecInput, evaluate
    sc = build()
    res = evaluate(ExecInput("XAUUSD", sc.start + 3, sc.map, sc.meta, True, sc.bars, None, None), {})
    assert res.decisions[0].output == "NO-SETUP" and res.decisions[0].error_code == 1001 and res.states == {}
    res = evaluate(ExecInput("XAUUSD", sc.start + 3, sc.map, sc.meta, False, sc.bars, 1, 1.2), {})
    assert res.decisions[0].reason == "no_valid_strategic_map"
    old = dict(sc.meta, created_at=sc.start - 7 * 3600)
    res = evaluate(ExecInput("XAUUSD", sc.start + 3, sc.map, old, True, sc.bars, 1, 1.2), {})
    assert res.decisions[0].reason == "strategic_map_stale"  # A8


def test_monitor_clears_only_with_structurally_new_map():
    from mapex.executor.engine import ExecInput, evaluate
    sc = build()
    st = ZoneState(KEY, "XAUUSD", "CHAIN_A", "buy", state="MONITOR", monitor_hash="abc", sweep_count=3,
                   first_seen=sc.start)
    st.ctx["seen"] = {tf: sc.start for tf in ("M1", "M5", "M15")}
    inp = ExecInput("XAUUSD", sc.start + 63, sc.map, sc.meta, True, sc.bars, 2650.0, 2650.2)
    res = evaluate(inp, {KEY: st})
    assert res.states[KEY].state == "MONITOR" and res.decisions == []
    inp.meta = dict(sc.meta, struct_hash="new")
    res = evaluate(inp, {KEY: st})
    assert res.states[KEY].state == "WATCH" and res.states[KEY].sweep_count == 0


def test_state_roundtrip_in_sqlite():
    from mapex.executor.state import archive_missing, load_states, save_state
    from mapex.store import Store
    db = Store(":memory:")
    st = ZoneState(KEY, "XAUUSD", "CHAIN_A", "buy", state="RETURN", sweep_count=2, first_seen=5,
                   ctx={"fvg": {"ce": 1.5}, "seen": {"M1": 60}})
    save_state(db, st, {"id": "CHAIN_A"})
    back = load_states(db, "XAUUSD")[KEY]
    assert back.state == "RETURN" and back.sweep_count == 2 and back.ctx["fvg"]["ce"] == 1.5
    archive_missing(db, "XAUUSD", set())
    assert load_states(db, "XAUUSD") == {}

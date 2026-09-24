"""D-70 intraday chain mode (the owner's default) vs GEM1 to the letter (CHAIN_MODE=strict)."""

from collections import Counter

import pytest

from mapex import config
from mapex.core.primitives import Bar, Zone
from mapex.mapper import chains as ch
from mapex.mapper.engine import MapperInput, build_map, derive_bias
from mapex.replay import Report
from tests.synth import market
from tests.test_mapper import BULL, _p


def zc(lps, low, high, direction="sell", tf="H1", disp=9, pid=None):
    z = Zone("FVG", direction, low, high, (low + high) / 2, 0, 0, 0, tf)
    c = ch.Candidate(z, tf, True, "MODERATE", disp)
    c.gen = _p(pid or f"LIQ_{lps:03d}", tf, "high" if direction == "sell" else "low", high + 1, lps)
    return c


def test_reachable_zones_become_chains_first():
    far, near, near2 = zc(80, 130, 131), zc(30, 102, 103), zc(25, 104, 105)
    wrong_side, too_weak = zc(70, 90, 91), zc(10, 101, 102)
    cands = [far, near, near2, wrong_side, too_weak]
    chains = ch.assign_intraday(cands, (65, 50, 40), 100.0, 10.0, "sell")
    assert chains["CHAIN_A"] is near and chains["CHAIN_B"] is near2  # 1C order among reachable zones
    assert ch.assign_chains(cands, (65, 50, 40))["CHAIN_A"] is far  # GEM1 strict picks the far one


def test_chain_a_always_exists_when_any_zone_is_linked():
    only_far = zc(55, 130, 131)
    chains = ch.assign_intraday([only_far], (65, 50, 40), 100.0, 10.0, "sell")
    assert chains["CHAIN_A"] is only_far  # below the GEM1 gate of 65 but still the GEM2 thesis root
    assert "CHAIN_A" not in ch.assign_chains([zc(55, 130, 131)], (65, 50, 40))
    assert ch.distance(zc(30, 99, 101), 100.0, "sell") == 0.0  # price inside the zone
    assert ch.distance(zc(30, 95, 96, direction="buy"), 100.0, "buy") == 4.0


def test_bias_conflict_is_resolved_by_the_hierarchy():
    reg = [_p("LIQ_001", "D1", "high", 110, 90), _p("LIQ_002", "D1", "low", 90, 50)]
    bear = {"LTH": "bearish", "ITH": "bearish", "STH": "corrective"}
    assert derive_bias(reg, 100, [], bear)["bias"] is None  # strict: no map
    out = derive_bias(reg, 100, [], bear, resolve=True)
    assert out["bias"] == "sell" and out["resolved_by"] == "hierarchy" and out["conflict"]
    tie = [_p("LIQ_001", "D1", "high", 110, 75), _p("LIQ_002", "D1", "low", 90, 70)]
    assert derive_bias(tie, 100, [], BULL)["reason"] == "bias_tie"
    assert derive_bias(tie, 100, [], BULL, resolve=True)["bias"] == "buy"


def test_touched_zone_stays_until_a_close_beyond_its_far_edge():
    z = Zone("FVG", "sell", 100.0, 101.0, 100.5, 1000, 0, 0, "H1")
    inside = [Bar(1000, 99, 100.8, 98, 99), Bar(4600, 99, 100.9, 98.5, 100.4)]
    assert not ch.broken(z, inside)
    assert ch.broken(z, [*inside, Bar(8200, 100.4, 102, 100.2, 101.5)])


@pytest.fixture(scope="module")
def mkt():
    return market(days=260, seed=11, price=2650.0)


def test_intraday_publishes_a_map_with_a_reachable_chain_far_more_often(mkt):
    got = Counter()
    for back in range(80, 4000, 360):
        cut = mkt["M15"][-back].t
        price = [b for b in mkt["M15"] if b.t < cut][-1].c
        for mode in ("strict", "intraday"):
            r = build_map(MapperInput("XAUUSD", cut + 5, price, mkt, tick=0.01, decimals=2, mode=mode))
            got[mode, "valid"] += r.valid
            got[mode, "near"] += bool(r.valid and any(z["time_horizon"] == "INTRADAY" for z in r.json["key_zones"]
                                                      if z["id"] in ("CHAIN_A", "CHAIN_B")))
            if mode == "intraday" and r.valid:
                assert r.json["active_causal_chain"]["root_liquidity_id"]  # GEM2 preflight never starves
    n = len(range(80, 4000, 360))
    assert got["intraday", "valid"] == n and got["intraday", "near"] >= n // 2
    assert got["intraday", "near"] > got["strict", "near"]


def test_chain_mode_setting_and_replay_diagnostics():
    assert config.load({}).chain_mode == "intraday"
    assert config.load({"CHAIN_MODE": "strict"}).chain_mode == "strict"
    bad = config.load({"CHAIN_MODE": "yolo"})
    assert bad.chain_mode == "intraday" and any("CHAIN_MODE" in e for e in bad.errors)
    rep = Report("XAUUSD", 30, 0, 86400 * 30)
    rep.killzones = {"09-01 London": True, "09-01 New York": False}
    rep.invalid["bias_tie"] = 3
    rep.steps.update({"RAID": 5, "SHIFT": 2})
    rep.blocks["Second sweep, wick only (Type #)"] = 4
    text = rep.text()
    assert "Kill zone me zinxhir afër çmimit: 1/2" in text and "bias_tie 3" in text
    assert "RAID 5 · SHIFT 2 · GAP 0 · RETURN 0 · CONFIRMED 0" in text and "Ku ndalet GEM2" in text

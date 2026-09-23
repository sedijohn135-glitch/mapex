"""Replay backtester: >= 30 days, deterministic, no lookahead, and an end-to-end paper trade."""

import asyncio
import json

import pytest

from mapex import config
from mapex.replay import run_replay
from tests.exec_fixture import build
from tests.synth import market_m1

S = config.load({"LOT_BTCUSD": "0.01", "LOT_XAUUSD": "0.10", "DATA_DIR": "/tmp/mapex-test"})


@pytest.fixture(scope="module")
def mkt():
    return market_m1(days_m1=33, days_total=380, seed=5)


def test_replay_30_days_deterministic_and_no_lookahead(mkt):
    end = mkt["M1"][-1].t - 86400
    start = end - 30 * 86400
    a = asyncio.run(run_replay("BTCUSD", mkt, start, end, S))
    past = {tf: [b for b in bars if b.t < end] for tf, bars in mkt.items()}
    b = asyncio.run(run_replay("BTCUSD", past, start, end, S))
    assert a.days == 30 and a.maps >= 30 * 24
    assert (a.maps, a.valid_maps, a.decisions, len(a.trades)) == (b.maps, b.valid_maps, b.decisions, len(b.trades))
    assert "REPLAY BTCUSD · 30 ditë" in a.text() and "Win rate" in a.text()


def test_replay_end_to_end_paper_trade(monkeypatch):
    sc = build()
    # after the 09:21 defense price runs to TP1 (2671) then to the server TP (2680)
    from mapex.core.primitives import Bar
    last = sc.m1[-1]
    path = [2655, 2660, 2665, 2671.5, 2675, 2681]
    extra = [Bar(last.t + 60 * (k + 1), p - 1, p + 0.5, p - 1.2, p) for k, p in enumerate(path)]
    m1 = [b for b in sc.m1] + extra
    from tests.synth import aggregate
    bars = {"M1": m1, "M5": aggregate(m1, "M5"), "M15": aggregate(m1, "M15"), "D1": sc.bars["D1"],
            "H1": aggregate(m1, "H1"), "H4": [], "W1": [], "MN1": []}

    def fake_mapper(store, s, symbol, b, price, now):
        meta = dict(sc.meta, created_at=int(now))
        store.execute("INSERT INTO maps(symbol, created_at, json, bias, valid, struct_hash) VALUES(?,?,?,?,1,?)",
                      (symbol, int(now), json.dumps({"map": sc.map, "meta": meta}), "buy", meta["struct_hash"]))

        class R:
            valid, reason, json = True, None, sc.map
        return R()

    import mapex.replay as rp
    monkeypatch.setattr(rp, "run_mapper", fake_mapper)
    rep = asyncio.run(run_replay("XAUUSD", bars, sc.start, extra[-1].t + 60, S))
    assert len(rep.trades) == 1
    t = rep.trades[0]
    assert t["side"] == "buy" and t["sl"] == 2643.5 and t["partial_done"] == 1 and t["state"] == "CLOSED"
    # 50 % closed near TP1, the rest at the server TP 2680 -> about +4.0R
    assert t["entry_fill"] == 2650.0  # replay ask = M1 close 2649.8 + 0.20 spread; risk 6.5
    assert t["result_r"] == pytest.approx(0.5 * (2671.5 - 2650.0) / 6.5 + 0.5 * (2680 - 2650.0) / 6.5)
    assert rep.wins == 1 and rep.win_rate == 1.0

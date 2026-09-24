"""Order-path tests with a fake broker that records every call (broker-execution §8)."""

import asyncio
import re
import sqlite3
from pathlib import Path

import pytest

from mapex import config, guards
from mapex.ctrader.broker import LiveVenue, TradeManager
from mapex.ctrader.client import CTraderClient, Quote
from mapex.ctrader.paper import PaperVenue
from mapex.executor.engine import Plan
from mapex.store import Store
from tests.fake_ctrader import FakeCTrader, build_server, connector_for
from tests.helpers import ny_ts

NOW = ny_ts(2026, 9, 22, 9, 22)
ENV = {"LOT_XAUUSD": "0.10", "LOT_BTCUSD": "0.01", "TRADING_MODE": "live", "DATA_DIR": "/tmp/x"}


def plan(key="MPX-XAU-0922-abc-1-A", side="buy", **kw):
    base = dict(setup_key=key, symbol="XAUUSD", side=side, chain="CHAIN_A", zone_key="z", decision_price=2650.20,
                sl=2643.50, tp1=2671.0, tp2=2680.0, tp3=2700.0, tp_server=2680.0, risk=6.7, r_tp1=3.1, r_tp2=4.4,
                management_level=None, anchor_type="SWEEP", spread=0.2, root_type="PML", root_price=2630.0,
                root_lps=82, sweep_count=1, sweep_type="BODY_CLOSE", zone_label="CHAIN_A H4 FVG @ 2642.50",
                session="New York 09:21", model="CRT", strategy_used=["Displacement + Return to Origin (DRO)"],
                confirmed_at=NOW)
    base.update(kw)
    return Plan(**base)


QUOTE = Quote(2650.0, 2650.2, NOW, NOW)


class Rig:
    def __init__(self, fake=None, env=None, clock=None):
        self.fake = fake or FakeCTrader()
        self.s = config.load({**ENV, **(env or {})})
        self.db = Store(":memory:")
        self.clock = clock or [NOW]
        self.client = CTraderClient("https://fake/trading/mcp", "tok.en.x",
                                    connector=connector_for(build_server(self.fake)), call_timeout=0.5,
                                    clock=lambda: self.clock[0])
        self.venue = LiveVenue(self.client, self.s)
        self.tm = TradeManager(self.db, self.s, self.venue, clock=lambda: self.clock[0], sleep=self._sleep)

    async def _sleep(self, _s):
        await asyncio.sleep(0)

    async def start(self):
        await self.client.calibrate("XAUUSD", [3], [1500, 14000])
        return self

    def calls(self, tool):
        return [a for t, a in self.fake.calls if t == tool]

    def trade(self, key="MPX-XAU-0922-abc-1-A"):
        return self.db.one("SELECT * FROM trades WHERE setup_key=?", (key,))


def go(coro):
    return asyncio.run(coro)


# 1. happy path ----------------------------------------------------------------------------------
def test_happy_path_one_order_relative_points_and_exact_amend():
    async def t():
        r = await Rig().start()
        out = await r.tm.execute(plan(), QUOTE)
        return r, out

    r, out = go(t())
    assert out == "open"
    co = r.calls("create_order")
    assert len(co) == 1
    a = co[0]
    assert a["orderType"] == "MARKET_RANGE" and a["tradeSide"] == "BUY" and a["volume"] == 1000  # 0.10 lot = 10 oz
    assert a["relativeStopLoss"] == 6700 and a["relativeTakeProfit"] == 29800  # points, 3 digits
    assert a["label"] == "MAPEX" and a["comment"] == "MPX-XAU-0922-abc-1-A"
    assert a["baseSlippagePrice"] == 2650.2 and a["slippageInPoints"] == 300
    assert "stopLoss" not in a and "takeProfit" not in a  # no absolute SL/TP on market orders (Q-R4)
    am = r.calls("amend_position")
    assert am == [{"positionId": am[0]["positionId"], "stopLoss": 2643.5, "takeProfit": 2680.0}]
    t = r.trade()
    assert t["state"] == "OPEN" and t["entry_fill"] == 2650.2
    ob = r.db.all("SELECT text FROM outbox")
    assert len(ob) == 1 and "MAPEX HYRI — BUY XAUUSD" in ob[0]["text"] and "Lot: <b>0.10</b>" in ob[0]["text"]
    logs = r.db.all("SELECT tool, ok FROM order_log")
    assert [x["tool"] for x in logs][:2] == ["create_order", "amend_position"]


# 2. timeout after send -------------------------------------------------------------------------
def test_timeout_never_resends_and_adopts_by_comment():
    async def t():
        fake = FakeCTrader()
        fake.mode.add("timeout")
        r = await Rig(fake).start()
        out = await r.tm.execute(plan(), QUOTE)
        return r, out

    r, out = go(t())
    assert len(r.calls("create_order")) == 1
    assert out == "open" and r.trade()["state"] == "OPEN" and r.trade()["position_id"]


def test_timeout_without_fill_fails_and_trips_kill_switch():
    async def t():
        fake = FakeCTrader()
        fake.mode.add("timeout_nofill")
        r = await Rig(fake).start()
        out = await r.tm.execute(plan(), QUOTE)
        return r, out

    r, out = go(t())
    assert len(r.calls("create_order")) == 1
    assert out.startswith("failed") and r.trade()["state"] == "FAILED"
    assert guards.kill_switch(r.db) == "verifikimi i urdhrit dështoi"


# 3. rejection ------------------------------------------------------------------------------------
def test_rejection_fails_without_retry():
    async def t():
        fake = FakeCTrader()
        fake.mode.add("reject")
        r = await Rig(fake).start()
        return r, await r.tm.execute(plan(), QUOTE)

    r, out = go(t())
    assert out.startswith("failed: rejected") and len(r.calls("create_order")) == 1
    assert r.trade()["state"] == "FAILED"
    assert any("refuzua" in x["text"] for x in r.db.all("SELECT text FROM outbox"))


# 4. missing SL after verification ---------------------------------------------------------------
def test_missing_sl_closes_and_trips():
    async def t():
        fake = FakeCTrader()
        fake.mode |= {"no_sl", "amend_ignored"}
        r = await Rig(fake).start()
        return r, await r.tm.execute(plan(), QUOTE)

    r, out = go(t())
    assert out == "closed: verification failed"
    assert len(r.calls("amend_position")) == 2  # one retry only
    assert len(r.calls("close_position")) == 1 and r.fake.positions == {}
    assert guards.kill_switch(r.db) == "verifikimi i urdhrit dështoi"


# 5. volume mismatch (contract size x100) -----------------------------------------------------------
def test_volume_mismatch_closes_immediately():
    async def t():
        fake = FakeCTrader()
        fake.mode.add("volume_x100")
        r = await Rig(fake).start()
        return r, await r.tm.execute(plan(), QUOTE)

    r, out = go(t())
    assert out == "closed: volume mismatch" and r.fake.positions == {}
    assert r.calls("close_position")[0]["volume"] == 100000
    assert "volumi nuk përputhet" in guards.kill_switch(r.db)


# 6. TP1 partial + breakeven ---------------------------------------------------------------------
def test_tp1_partial_close_and_breakeven_sends_both_legs():
    async def t():
        r = await Rig().start()
        await r.tm.execute(plan(), QUOTE)
        await r.tm.manage({"XAUUSD": Quote(2665.0, 2665.2, NOW, NOW)})  # below TP1: nothing
        n_before = len(r.calls("close_position"))
        r.clock[0] += 30
        await r.tm.manage({"XAUUSD": Quote(2671.0, 2671.2, NOW, NOW)})
        return r, n_before

    r, n_before = go(t())
    assert n_before == 0
    assert r.calls("close_position") == [{"positionId": r.calls("close_position")[0]["positionId"], "volume": 500}]
    be = r.calls("amend_position")[-1]
    assert be["stopLoss"] == 2650.2 and be["takeProfit"] == 2680.0
    t = r.trade()
    assert t["partial_done"] == 1 and t["be_done"] == 1 and t["state"] == "BE"


# 7. amend_position always carries both legs --------------------------------------------------------
def test_amend_position_both_fields_everywhere():
    src = "\n".join(p.read_text() for p in Path("mapex").rglob("*.py"))
    calls = re.findall(r'call\(\s*"amend_position",\s*\{[^}]*\}', src)
    assert calls and all("stopLoss" in c and "takeProfit" in c for c in calls)
    for m in re.finditer(r"amend_position,\s*([^\n]*)", src):
        assert m.group(1).count(",") >= 2, m.group(0)  # pid, stop_loss, take_profit
    with pytest.raises(ValueError):
        go(LiveVenue(None, config.load(ENV)).amend_position("1", None, 2680.0))


# 8. guards ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("setup,expect", [
    (lambda db: db.put("kill_switch", "manual /stop"), "kill_switch"),
    (lambda db: db.put("consecutive_losses", "3"), "max_consecutive_losses"),
    (lambda db: [db.execute("INSERT INTO trades(setup_key, symbol, side, state, opened_at, result_r) "
                            "VALUES(?, 'BTCUSD', 'buy', 'CLOSED', ?, -1.5)", (f"k{i}", NOW - 60)) for i in range(2)],
     "daily_loss_limit_r"),
    (lambda db: [db.execute("INSERT INTO trades(setup_key, symbol, side, state, opened_at) "
                            "VALUES(?, 'BTCUSD', 'buy', 'CLOSED', ?)", (f"k{i}", NOW - 60)) for i in range(3)],
     "max_trades_per_day"),
    (lambda db: db.execute("INSERT INTO trades(setup_key, symbol, side, state, opened_at) "
                           "VALUES('o1', 'XAUUSD', 'sell', 'OPEN', ?)", (NOW - 60,)), "opposite_position_open"),
    (lambda db: [db.execute("INSERT INTO trades(setup_key, symbol, side, state, opened_at) "
                            "VALUES(?, 'BTCUSD', 'buy', 'OPEN', ?)", (f"o{i}", NOW - 60)) for i in range(2)],
     "max_open_total"),
])
def test_each_breaker_blocks_independently(setup, expect):
    s = config.load(ENV)
    db = Store(":memory:")
    setup(db)
    why = guards.check_entry(s, db, plan(), QUOTE, NOW)
    assert any(expect in w for w in why), why


def test_market_guards_spread_killzone_quote_lot():
    s = config.load(ENV)
    db = Store(":memory:")
    assert guards.check_entry(s, db, plan(), QUOTE, NOW) == []
    assert "spread" in guards.check_entry(s, db, plan(), Quote(2650.0, 2651.5, NOW, NOW), NOW)[0]
    assert "outside_killzone" in guards.check_entry(s, db, plan(), QUOTE, ny_ts(2026, 9, 22, 12, 30))
    assert "quote_stale" in guards.check_entry(s, db, plan(), None, NOW)
    s2 = config.load({**ENV, "LOT_XAUUSD": ""})
    assert "no LOT_XAUUSD" in guards.check_entry(s2, db, plan(), QUOTE, NOW)
    assert "market_closed_or_near_close" in guards.check_entry(s, db, plan(), QUOTE, ny_ts(2026, 9, 19, 9, 30))


def test_duplicate_setup_key_blocked_by_db():
    async def t():
        r = await Rig().start()
        first = await r.tm.execute(plan(), QUOTE)
        r.db.put("consecutive_losses", "0")
        second = await r.tm.execute(plan(), QUOTE)
        return r, first, second

    r, first, second = go(t())
    assert first == "open" and "duplicate_setup_key" in second and len(r.calls("create_order")) == 1
    with pytest.raises(sqlite3.IntegrityError):
        r.db.execute("INSERT INTO trades(setup_key, state) VALUES(?, 'SENDING')", ("MPX-XAU-0922-abc-1-A",))


def test_breakers_trip_kill_switch_on_results():
    s = config.load(ENV)
    db = Store(":memory:")
    for _ in range(3):
        guards.record_result(s, db, -1.0, NOW)
    assert guards.kill_switch(db) == "3 humbje radhazi"
    guards.resume(db)
    guards.daily_roll(db, NOW)
    assert db.get("consecutive_losses") == "0"


# 9. restart mid-trade --------------------------------------------------------------------------
def test_restart_rebuilds_state_without_duplicate():
    async def t():
        r = await Rig().start()
        await r.tm.execute(plan(), QUOTE)
        # a new process: fresh client + manager, same DB and broker
        tm2 = TradeManager(r.db, r.s, LiveVenue(r.client, r.s), clock=lambda: r.clock[0], sleep=r._sleep)
        rec = await tm2.reconcile()
        r.clock[0] += 30
        await tm2.manage({"XAUUSD": Quote(2672.0, 2672.2, NOW, NOW)})
        return r, rec

    r, rec = go(t())
    assert len(r.calls("create_order")) == 1 and rec["orphans"] == 0
    assert r.trade()["partial_done"] == 1  # management resumed


def test_restart_with_sending_row_adopts_position_by_comment():
    async def t():
        r = await Rig().start()
        pid = r.fake.open_manual("XAUUSD", "BUY", 1000, label="MAPEX", comment="MPX-XAU-0922-abc-1-A")
        r.fake.positions[pid]["stopLoss"] = 2643.0
        r.fake.positions[pid]["takeProfit"] = 2680.0
        r.db.execute("INSERT INTO trades(setup_key, symbol, side, lots, volume_cents, sl, tp1, tp_server, state, mode, "
                     "opened_at, info) VALUES('MPX-XAU-0922-abc-1-A','XAUUSD','buy',0.1,1000,2643.5,2671,2680,"
                     "'SENDING','live',?, '{}')", (NOW,))
        rec = await r.tm.reconcile()
        return r, rec

    r, rec = go(t())
    assert rec["adopted"] == 1 and r.calls("create_order") == []
    assert r.trade()["state"] == "OPEN" and r.calls("amend_position")[-1]["stopLoss"] == 2643.5


# 10. manual interference -----------------------------------------------------------------------------
def test_manual_modify_and_close_are_respected():
    async def t():
        r = await Rig().start()
        await r.tm.execute(plan(), QUOTE)
        pid = int(r.trade()["position_id"])
        r.fake.positions[pid]["stopLoss"] = 2640.0  # owner widens the stop by hand
        r.clock[0] += 120
        n_amends = len(r.calls("amend_position"))
        await r.tm.manage({"XAUUSD": Quote(2672.0, 2672.2, NOW, NOW)})
        adopted = r.trade()["manual"]
        r.fake.close(pid, r.fake.positions[pid]["volume"], 2660.0)  # owner closes it
        await r.tm.manage({})
        return r, n_amends, adopted

    r, n_amends, adopted = go(t())
    assert adopted == 1
    assert len(r.calls("amend_position")) == n_amends  # never re-amended against the owner
    assert r.calls("close_position") == []  # MAPEX did not close it
    t = r.trade()
    assert t["state"] == "CLOSED" and t["result_r"] == pytest.approx((2660.0 - 2650.2) / 6.7)
    assert len(r.calls("create_order")) == 1  # never reopened


# 11. only MAPEX positions ------------------------------------------------------------------------
def test_foreign_positions_are_untouchable():
    async def t():
        r = await Rig().start()
        foreign = r.fake.open_manual("XAUUSD", "SELL", 500, label="", comment="owner")
        await r.tm.execute(plan(), QUOTE)
        await r.tm.reconcile()
        n = await r.tm.flat()
        return r, foreign, n

    r, foreign, n = go(t())
    assert n == 1 and foreign in r.fake.positions
    assert all(c["positionId"] != foreign for c in r.calls("close_position") + r.calls("amend_position"))
    assert r.db.one("SELECT COUNT(*) AS n FROM trades")["n"] == 1  # the foreign position was never adopted


# 12. paper vs live parity --------------------------------------------------------------------------
def test_paper_live_parity_and_paper_simulation():
    async def t():
        r = await Rig().start()
        await r.tm.execute(plan(), QUOTE)
        live_args = r.calls("create_order")[0]
        s = config.load({**ENV, "TRADING_MODE": "paper"})
        db = Store(":memory:")
        clock = [NOW]
        pv = PaperVenue(db, s, lambda sym: QUOTE, lambda: clock[0])
        seen = []
        orig = pv.create_order

        async def spy(symbol, args):
            seen.append(dict(args))
            return await orig(symbol, args)

        pv.create_order = spy
        tm = TradeManager(db, s, pv, clock=lambda: clock[0], sleep=r._sleep)
        out = await tm.execute(plan(), QUOTE)
        pos = await pv.positions()
        # same bar hits SL and TP -> SL first
        from mapex.core.primitives import Bar
        pv.on_bar("XAUUSD", Bar(NOW, 2650, 2690, 2640, 2650))
        clock[0] += 120
        await tm.manage({})
        return live_args, seen[0], out, pos, db

    live_args, paper_args, out, pos, db = go(t())
    live_args = {k: v for k, v in live_args.items() if k != "symbolId"}
    assert live_args == paper_args and out == "open"
    assert pos[0]["sl"] == 2643.5 and pos[0]["tp"] == 2680.0
    t = db.one("SELECT * FROM trades")
    assert t["state"] == "CLOSED" and t["result_r"] == pytest.approx(-1.0)
    assert "📝 PAPER" in db.one("SELECT text FROM outbox WHERE dedupe LIKE 'entry:%'")["text"]


def test_partial_fill_is_a_volume_mismatch():
    async def t():
        fake = FakeCTrader()
        fake.mode.add("partial_fill")
        r = await Rig(fake).start()
        return r, await r.tm.execute(plan(), QUOTE)

    r, out = go(t())
    assert out == "closed: volume mismatch" and r.fake.positions == {}
    assert r.calls("close_position")[0]["volume"] == 500 and guards.kill_switch(r.db)


def test_market_closed_blocks_before_any_call():
    async def t():
        r = await Rig(clock=[ny_ts(2026, 9, 19, 10, 0)]).start()  # Saturday
        return r, await r.tm.execute(plan(), QUOTE)

    r, out = go(t())
    assert out.startswith("blocked") and "market_closed" in out and r.calls("create_order") == []


def test_relative_points_scale_is_learned_from_the_first_fill():
    """If the broker reads relative points at another power of ten, the fill is exactified by the amend and the
    next order uses the proven scale."""
    async def t():
        fake = FakeCTrader()
        fake.points_digits["XAUUSD"] = 2  # server: 1 point = 0.01, MAPEX assumed 0.001
        r = await Rig(fake).start()
        first = await r.tm.execute(plan(), QUOTE)
        r.db.execute("UPDATE trades SET state='CLOSED' WHERE setup_key=?", ("MPX-XAU-0922-abc-1-A",))
        r.fake.positions.clear()
        second = await r.tm.execute(plan(key="MPX-XAU-0922-abc-2-A"), QUOTE)
        return r, first, second

    r, first, second = go(t())
    assert first == "open" and second == "open"
    co = r.calls("create_order")
    assert co[0]["relativeStopLoss"] == 6700 and co[1]["relativeStopLoss"] == 670
    assert r.db.get("points_digits:XAUUSD") == "2"
    assert all(a["stopLoss"] == 2643.5 for a in r.calls("amend_position"))  # both trades exactified
    assert any("njësia e SL/TP" in x["text"] for x in r.db.all("SELECT text FROM outbox"))

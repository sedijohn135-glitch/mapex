"""cTrader Open API client (D-69) against an in-process fake of the JSON protocol."""

import asyncio
import json

import pytest

from mapex import config
from mapex.ctrader.broker import LiveVenue, TradeManager
from mapex.ctrader.client import AuthError, ToolError, TransportError
from mapex.ctrader.openapi import OpenApiClient
from mapex.main import App
from mapex.store import Store
from tests.fake_openapi import FakeOpenApi
from tests.test_broker import ENV, NOW, QUOTE, plan

OA_ENV = {"CTRADER_CLIENT_ID": "cid", "CTRADER_CLIENT_SECRET": "csecret", "CTRADER_ACCES_TOKEN": "acc-1",
          "CTRADER_REFRESH_TOKEN": "ref-1", "CTRADER_ACCOUNT_ID": "777"}


def client_for(fake, store=None, access="acc-1", refresh="ref-1", account="777", timeout=1.0):
    return OpenApiClient("cid", "csecret", access, refresh, account, store=store, connect=fake.connect,
                         call_timeout=timeout, clock=lambda: NOW)


def run(coro):
    return asyncio.run(coro)


def test_connects_authorises_and_reads_market_data():
    fake = FakeOpenApi()
    start = 1_789_000_000 // 3600 * 3600
    fake.bars[(41, 9)] = [(start + i * 3600, 2600 + i, 2601 + i, 2599 + i, 2600.5 + i) for i in range(30)]

    async def go():
        c = client_for(fake)
        d = await c.calibrate("XAUUSD", [3], [1500, 14000])
        q = await c.spot(["XAUUSD"])
        bars = await c.trendbars("XAUUSD", "H1", start, start + 30 * 3600)
        version = await c.call("get_version")
        await c.close()
        return c, d, q, bars, version

    c, d, q, bars, version = run(go())
    assert d == 5 and c.env == "demo" and c.account_id == 777 and c.trading_profile
    assert q["XAUUSD"].bid == 2650.0 and round(q["XAUUSD"].spread, 2) == 0.2
    assert len(bars) == 30 and bars[0].t == start and (bars[0].o, bars[0].h, bars[0].l, bars[0].c) == (
        2600, 2601, 2599, 2600.5)
    assert version == {"version": "fake-oa-1"}
    assert fake.connects == ["wss://demo.ctraderapi.com:5036"]
    req = fake.requests(2137)[0]
    assert req["period"] == 9 and req["symbolId"] == 41 and req["ctidTraderAccountId"] == 777


def test_account_may_be_the_login_and_a_live_account_uses_the_live_host():
    fake = FakeOpenApi()
    fake.accounts = [{"ctidTraderAccountId": 888, "isLive": True, "traderLogin": 9001}]

    async def go():
        c = client_for(fake, account="9001")
        await c.load_symbols()
        return c

    c = run(go())
    assert c.account_id == 888 and c.env == "live"
    assert fake.connects == ["wss://demo.ctraderapi.com:5036", "wss://live.ctraderapi.com:5036"]


def test_unknown_account_is_an_auth_error_naming_the_logins():
    fake = FakeOpenApi()

    async def go():
        c = client_for(fake, account="42")
        with pytest.raises(AuthError, match="5123"):
            await c.load_symbols()
        return c

    assert run(go()).auth_error_since is not None


def test_expired_access_token_is_refreshed_and_kept_in_the_volume():
    fake = FakeOpenApi()
    fake.valid_access = set()  # acc-1 expired; ref-1 still valid
    db = Store(":memory:")

    async def go():
        c = client_for(fake, store=db)
        await c.load_symbols()
        again = client_for(fake, store=db)  # a restart with the same Railway variables
        await again.load_symbols()
        return c, again

    c, again = run(go())
    assert c.token == "acc-2" and db.get("oa_access_token") == "acc-2" and db.get("oa_refresh_token") == "ref-2"
    assert again.token == "acc-2" and len(fake.requests(2173)) == 1  # no second refresh
    fresh = client_for(fake, store=db, access="acc-9", refresh="ref-9")  # new tokens in Railway win
    assert fresh.token == "acc-9" and db.get("oa_access_token") is None


def test_refused_refresh_is_an_auth_error_without_leaking_tokens():
    fake = FakeOpenApi()
    fake.valid_access, fake.valid_refresh = set(), set()

    async def go():
        c = client_for(fake)
        with pytest.raises(AuthError) as err:
            await c.load_symbols()
        return c, str(err.value)

    c, text = run(go())
    assert c.auth_error_since is not None and "acc-1" not in text and "ref-1" not in text


def test_order_flow_through_the_trade_manager():
    fake = FakeOpenApi()
    db = Store(":memory:")
    s = config.load(ENV)

    async def fast(_s):
        await asyncio.sleep(0)

    async def go():
        c = client_for(fake, store=db)
        tm = TradeManager(db, s, LiveVenue(c, s), clock=lambda: NOW, sleep=fast)
        await c.calibrate("XAUUSD", [3], [1500, 14000])
        out = await tm.execute(plan(), QUOTE)
        return out, tm.trade("MPX-XAU-0922-abc-1-A")

    out, t = run(go())
    assert out == "open" and t["state"] == "OPEN"
    [order] = fake.requests(2106)
    assert order["orderType"] == 5 and order["tradeSide"] == 1 and order["volume"] == 1000
    assert order["relativeStopLoss"] == 670_000 and order["relativeTakeProfit"] == 2_980_000  # 1/100000 of price
    assert order["baseSlippagePrice"] == 2650.2 and order["label"] == "MAPEX"
    assert order["comment"] == "MPX-XAU-0922-abc-1-A"
    slip = s.max_slippage_points["XAUUSD"]  # written for 3-digit pipettes; XAUUSD points are 2-digit here
    assert order["slippageInPoints"] == max(1, round(slip / 10))
    [pos] = fake.positions.values()
    assert (pos["stopLoss"], pos["takeProfit"]) == (2643.5, 2680.0)
    assert db.get("points_digits:XAUUSD") is None  # the 1/100000 scale was right: nothing to learn
    assert all("stopLoss" in a and "takeProfit" in a for a in fake.requests(2110))  # Q-R10 both legs


@pytest.mark.parametrize("mode", ["reject", "silent"])
def test_rejected_or_unanswered_order_is_sent_once(mode):
    fake = FakeOpenApi()
    fake.reject_orders = "NOT_ENOUGH_MONEY" if mode == "reject" else None
    fake.silent_orders = mode == "silent"
    db = Store(":memory:")
    s = config.load(ENV)

    async def fast(_s):
        await asyncio.sleep(0)

    async def go():
        c = client_for(fake, store=db, timeout=0.3)
        tm = TradeManager(db, s, LiveVenue(c, s), clock=lambda: NOW, sleep=fast)
        await c.calibrate("XAUUSD", [3], [1500, 14000])
        return await tm.execute(plan(), QUOTE)

    out = run(go())
    assert len(fake.requests(2106)) == 1
    assert out.startswith("failed: rejected") if mode == "reject" else out == "failed: unknown outcome"


def test_execution_events_without_client_msg_id_are_matched_by_comment():
    fake = FakeOpenApi()
    fake.exec_with_cid = False

    async def go():
        c = client_for(fake)
        await c.calibrate("XAUUSD", [3], [1500, 14000])
        return await c.call("create_order", {"symbolId": 41, "orderType": "MARKET", "tradeSide": "SELL",
                                             "volume": 100, "relativeStopLoss": 500_000, "label": "MAPEX",
                                             "comment": "k-1"})

    data = run(go())
    assert data["positionId"] and data["executionPrice"] == 2650.0 and data["filledVolume"] == 100


def test_positions_close_and_deals_are_normalised():
    fake = FakeOpenApi()
    s = config.load(ENV)

    async def go():
        c = client_for(fake)
        venue = LiveVenue(c, s)
        await c.calibrate("XAUUSD", [3], [1500, 14000])
        fill = await venue.create_order("XAUUSD", {"orderType": "MARKET", "tradeSide": "BUY", "volume": 1000,
                                                   "relativeStopLoss": 670_000, "relativeTakeProfit": 2_980_000,
                                                   "label": "MAPEX", "comment": "k-2"})
        before = await venue.positions()
        await venue.close_position(fill["position_id"], 500)
        deals = await venue.deals_for(fill["position_id"], NOW - 60)
        return fill, before, deals, await venue.positions()

    fill, before, deals, after = run(go())
    assert fill["fill"] == 2650.2 and fill["volume"] == 1000
    [p] = before
    assert p["side"] == "buy" and p["sl"] == 2643.5 and p["tp"] == 2680.0 and p["label"] == "MAPEX"
    assert [d["closing"] for d in deals] == [False, True] and after[0]["volume"] == 500


def test_dropped_connection_fails_pending_requests_and_reconnects():
    fake = FakeOpenApi()

    async def go():
        c = client_for(fake, timeout=2.0)
        await c.calibrate("XAUUSD", [3], [1500, 14000])
        fake.silent_orders = True
        order = asyncio.create_task(c.call("create_order", {"symbolId": 41, "orderType": "MARKET",
                                                            "tradeSide": "BUY", "volume": 100}))
        await asyncio.sleep(0.05)
        await fake.sockets[-1].close()  # the server drops the socket mid-order
        with pytest.raises(TransportError):
            await order
        c._retry_at = 0
        q = await c.spot(["XAUUSD"])
        return q

    q = run(go())
    assert q["XAUUSD"].bid == 2650.0 and len(fake.connects) == 2
    assert len(fake.requests(2106)) == 1 and len(fake.requests(2127)) == 2  # re-subscribed, never resent


def test_view_only_token_has_no_trading_profile_and_order_errors_are_tool_errors():
    fake = FakeOpenApi()
    fake.scope = "SCOPE_VIEW"

    async def go():
        c = client_for(fake)
        await c.load_symbols()
        with pytest.raises(ToolError):
            await c.call("create_order", {"symbolId": 41, "orderType": "MARKET", "tradeSide": "BUY", "volume": 1})
        return c

    assert not run(go()).trading_profile


def test_settings_and_app_run_on_open_api(tmp_path):
    s = config.load({**OA_ENV, "DATA_DIR": str(tmp_path), "TRADING_MODE": "live", "LOT_XAUUSD": "0.1",
                     "LOT_BTCUSD": "0.01"})
    assert s.openapi and s.ctrader_access_token == "acc-1"
    assert not any("CTRADER_MCP_CONFIG" in w for w in s.warnings)
    partial = config.load({"CTRADER_CLIENT_ID": "cid"})
    assert not partial.openapi and any("CTRADER_CLIENT_SECRET" in w for w in partial.warnings)
    fake = FakeOpenApi()
    app = App(s, clock=lambda: NOW, openapi_connect=fake.connect)
    assert app.mode == "paper" and app.env == "unknown"  # nothing trades before the account is known
    asyncio.run(app.startup())
    assert app.env == "demo" and app.mode == "live" and isinstance(app.venue, LiveVenue)
    assert set(app.active) == {"XAUUSD", "BTCUSD"}
    boot = app.store.one("SELECT text FROM outbox WHERE dedupe LIKE 'boot:%'")["text"]
    assert "Modaliteti: LIVE" in boot and "Llogaria: demo" in boot and "⛔" not in boot
    reply = asyncio.run(app.command("/ctrader", "anything"))
    assert "Railway" in reply and "CTRADER_CLIENT_ID" in reply
    health = json.dumps(app.health())
    assert "acc-1" not in health and "csecret" not in health

import asyncio

import pytest

from mapex.core.timeutil import TF_SECONDS
from mapex.ctrader.client import AuthError, CTraderClient, RateLimiter, ToolError, TransportError, parse_trendbar
from mapex.data.candles import Candles, missing_bars
from mapex.data.quotes import Quotes
from tests.fake_ctrader import FakeCTrader, build_server, connector_for


def client_for(fake, **kw):
    return CTraderClient("https://fake/trading/mcp", "tok.en.x", connector=connector_for(build_server(fake)), **kw)


def run(coro):
    return asyncio.run(coro)


def test_symbols_calibration_and_spot():
    fake = FakeCTrader()

    async def go():
        c = client_for(fake)
        d = await c.calibrate("XAUUSD", [None, 3], [1500, 14000])
        q = await c.spot(["XAUUSD"])
        await c.close()
        return d, q, c

    d, q, c = run(go())
    assert d == 3 and q["XAUUSD"].bid == 2650.0 and round(q["XAUUSD"].spread, 2) == 0.2
    assert c.trading_profile


def test_calibration_failure_never_guesses():
    fake = FakeCTrader()

    async def go():
        c = client_for(fake)
        return await c.calibrate("XAUUSD", [None, 2], [1500, 14000])

    with pytest.raises(ValueError):
        run(go())


def test_unknown_symbol_never_sent_in_batch():
    fake = FakeCTrader()

    async def go():
        c = client_for(fake)
        await c.calibrate("XAUUSD", [3], [1500, 14000])
        return await c.raw_spot(["XAUUSD", "NOPE"])

    out = run(go())
    assert list(out) == ["XAUUSD"]  # P2: the unknown id never poisoned the batch
    sent = [a for t, a in fake.calls if t == "get_spot_prices"][-1]["symbolId"]
    assert sent == [41]


def test_trendbars_chunked_720h_and_paginated():
    fake = FakeCTrader()
    start = 1_700_000_000 - 1_700_000_000 % 3600
    fake.bars[("XAUUSD", "H1")] = [(start + i * 3600, 2600 + i * 0.01, 2601, 2599, 2600.5) for i in range(2000)]

    async def go():
        c = client_for(fake)
        await c.calibrate("XAUUSD", [3], [1500, 14000])
        return await c.trendbars("XAUUSD", "H1", start, start + 2000 * 3600)

    bars = run(go())
    assert len(bars) == 2000 and bars[0].t == start and bars[-1].t == start + 1999 * 3600
    windows = [a for t, a in fake.calls if t == "get_trendbars"]
    assert all(a["toTimestamp"] - a["fromTimestamp"] <= 720 * 3600 * 1000 for a in windows)
    assert all(a["period"] == "H_1" for a in windows)
    assert len(windows) >= 3


def test_auth_error_detected_and_hot_swap():
    fake = FakeCTrader()
    fake.auth_fail = True

    async def go():
        c = client_for(fake)
        with pytest.raises(AuthError):
            await c.load_symbols()
        assert c.auth_error_since is not None
        fake.auth_fail = False
        await c.set_credentials("https://fake/trading/mcp", "new.to.ken")
        syms = await c.load_symbols()
        await c.close()
        return c, syms

    c, syms = run(go())
    assert "XAUUSD" in syms and c.auth_error_since is None and c.token == "new.to.ken"


def test_missing_config_is_auth_error():
    async def go():
        return await CTraderClient().load_symbols()

    with pytest.raises(AuthError):
        run(go())


def test_mutation_timeout_is_not_retried():
    fake = FakeCTrader()
    fake.mode.add("timeout")

    async def go():
        c = client_for(fake, call_timeout=0.5)
        await c.load_symbols()
        with pytest.raises(TransportError):
            await c.call("create_order", {"symbolId": 41, "orderType": "MARKET", "tradeSide": "BUY", "volume": 100})

    run(go())
    assert [t for t, _ in fake.calls].count("create_order") == 1


def test_tool_rejection_surfaces():
    fake = FakeCTrader()

    async def go():
        c = client_for(fake)
        await c.load_symbols()
        await c.call("get_trendbars", {"symbolId": 41, "period": "M_2", "fromTimestamp": 0, "toTimestamp": 1})

    with pytest.raises(ToolError):
        run(go())


def test_rate_limiter_spacing():
    t = [0.0]

    async def go():
        rl = RateLimiter(5, clock=lambda: t[0])
        await rl.acquire()
        return rl.next_at

    assert run(go()) == pytest.approx(0.2)


def test_parse_trendbar_delta_form():
    b = parse_trendbar({"timestamp": 60_000, "low": 2650000, "deltaOpen": 100, "deltaHigh": 500, "deltaClose": 300}, 3)
    assert (b.t, b.o, b.h, b.l, b.c) == (60, 2650.1, 2650.5, 2650.0, 2650.3)
    assert parse_trendbar({"bad": 1}, 3) is None


def test_candles_cache_and_contiguity():
    fake = FakeCTrader()
    start = 1_790_000_000 - 1_790_000_000 % 60
    rows = [(start + i * 60, 2650, 2651, 2649, 2650.5) for i in range(30) if i != 25]
    fake.bars[("BTCUSD", "M1")] = [(t, o * 20, h * 20, l * 20, c * 20) for t, o, h, l, c in rows]

    async def go():
        c = client_for(fake)
        await c.calibrate("BTCUSD", [2], [25000, 240000])
        cs = Candles(c)
        await cs.refresh("BTCUSD", "M1", start + 30 * 60 + 5)
        ok = await cs.ensure_contiguous("BTCUSD", "M1", start + 30 * 60 + 5)
        return cs, ok

    cs, ok = run(go())
    assert not ok  # a missing bar is refetched once, then the tick is skipped
    assert missing_bars(cs.closed("BTCUSD", "M1", start + 30 * 60 + 5), "M1", "BTCUSD") == [start + 25 * 60]
    assert TF_SECONDS["M1"] == 60


def test_quote_freshness_and_skew():
    from mapex.ctrader.client import Quote
    q = Quotes()
    q.update({"XAUUSD": Quote(2650, 2650.2, 1000.0, 1000.0)})
    assert q.fresh("XAUUSD", 1004) and q.fresh("XAUUSD", 1006) is None
    q.update({"XAUUSD": Quote(2650, 2650.2, 1040.0, 1000.0)})  # broker clock 40 s ahead
    assert q.fresh("XAUUSD", 1001) is None

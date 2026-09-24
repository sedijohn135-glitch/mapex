import asyncio

import pytest

from mapex.core.timeutil import TF_SECONDS
from mapex.ctrader.client import AuthError, CTraderClient, RateLimiter, ToolError, TransportError, parse_trendbar
from mapex.data.candles import Candles, missing_bars
from mapex.data.quotes import Quotes
from tests.fake_ctrader import FakeCTrader, build_server, connector_for, evicting_connector


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


def test_calibration_finds_the_unique_digits_or_refuses():
    fake = FakeCTrader()

    async def go(cands, band):
        c = client_for(fake)
        return await c.calibrate("XAUUSD", cands, band)

    assert run(go([None, 2], [1500, 14000])) == 3  # wrong candidate 2, but only 10^3 lands the bid in the band
    with pytest.raises(ValueError):
        run(go([None], [1, 1_000_000]))  # a band wider than 10x is ambiguous: never guessed


@pytest.mark.parametrize("encoding,expected", [("pipettes", 3), ("display", 0), ("e5", 5), ("pip2", 2)])
def test_every_spot_and_bar_encoding_decodes_to_real_prices(encoding, expected):
    """The real server may send display floats, pipettes or 1/100000 units: all must decode to ~4287."""
    fake = FakeCTrader()
    fake.quotes["XAUUSD"] = (4287.42, 4287.48, 0)
    fake.spot_encoding = encoding
    fake.bar_encoding = encoding
    start = 1_790_000_000 - 1_790_000_000 % 3600
    fake.bars[("XAUUSD", "H1")] = [(start + i * 3600, 4280 + i, 4290 + i, 4270 + i, 4285.55 + i) for i in range(5)]

    async def go():
        c = client_for(fake)
        d = await c.calibrate("XAUUSD", [None, 3], [1500, 14000])
        q = await c.spot(["XAUUSD"])
        bars = await c.trendbars("XAUUSD", "H1", start, start + 5 * 3600)
        return d, q["XAUUSD"], bars

    d, q, bars = run(go())
    assert d == expected
    assert abs(q.bid - 4287.42) < 1e-6 and abs(q.ask - 4287.48) < 1e-6
    assert abs(bars[-1].c - 4289.55) < 1e-6 and len(bars) == 5


def test_session_errors_reconnect_instead_of_token_alarm():
    fake = FakeCTrader()
    fake.fail_once["get_version"] = "Bad Request: No valid session ID provided"

    async def go():
        c = client_for(fake)
        await c.load_symbols()
        return c, await c.call("get_version")

    c, out = run(go())
    assert out["version"] and c.auth_error_since is None  # retried on a fresh session, no 🔑


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


def test_every_timeframe_fetched_from_broker_never_aggregated():
    fake = FakeCTrader()

    async def go():
        c = client_for(fake)
        await c.calibrate("XAUUSD", [3], [1500, 14000])
        cs = Candles(c)
        for tf in ("MN1", "W1", "D1", "H4", "H1", "M15", "M5", "M1"):
            await cs.refresh("XAUUSD", tf, 1_790_000_000)

    run(go())
    periods = {a["period"] for t, a in fake.calls if t == "get_trendbars"}
    assert periods == {"MN_1", "W_1", "D_1", "H_4", "H_1", "M_15", "M_5", "M_1"}  # A3


def test_lost_session_is_reopened_and_resent(monkeypatch):
    monkeypatch.setattr("mapex.ctrader.client.BACKOFF_S", 0)
    fake = FakeCTrader()
    fake.fail_symbol[101] = 4  # four "Session not found" answers in a row, then fine

    async def go():
        c = client_for(fake)
        return await c.calibrate("BTCUSD", [2], [25000, 240000]), c

    digits, c = run(go())
    assert digits == 2 and c.sessions_opened == 5  # the first session + one reopen per rejection


def test_order_rejected_for_a_lost_session_is_resent_and_filled_once(monkeypatch):
    """The server answers "Session not found" before any tool runs (MCP spec 404): resending cannot duplicate."""
    monkeypatch.setattr("mapex.ctrader.client.BACKOFF_S", 0)
    fake = FakeCTrader()

    async def go():
        c = client_for(fake)
        await c.load_symbols()
        fake.session_drops = 2
        await c.call("create_order", {"symbolId": 41, "orderType": "MARKET", "tradeSide": "BUY", "volume": 100})

    run(go())
    assert len(fake.positions) == 1 and [t for t, _ in fake.calls].count("create_order") == 1


def test_string_timestamps_follow_the_live_schema(monkeypatch):
    """Railway log: get_trendbars rejected numeric timestamps ("expected string, received number")."""
    start = 1_789_000_000 // 3600 * 3600
    for fmt in ("iso", "ms"):
        fake = FakeCTrader()
        fake.ts_format = fmt
        fake.bars[("XAUUSD", "H1")] = [(start + i * 3600, 2600, 2601, 2599, 2600.5) for i in range(10)]

        async def go(fake=fake):
            c = client_for(fake)
            await c.calibrate("XAUUSD", [3], [1500, 14000])
            first = await c.trendbars("XAUUSD", "H1", start, start + 10 * 3600)
            await c.trendbars("XAUUSD", "H1", start, start + 10 * 3600)
            return first

        assert len(run(go())) == 10
        sent = [a["fromTimestamp"] for t, a in fake.calls if t == "get_trendbars"]
        assert all(isinstance(x, str) for x in sent)
        if fmt == "iso":
            assert sent == ["2026-09-10T00:00:00Z"] * 2
        else:  # ISO refused once, then epoch-ms text from then on
            assert sent[0].endswith("Z") and sent[1:] == [str(start * 1000)] * 2


def test_one_reused_session_owned_by_one_task_serves_every_caller():
    """Railway logs: a session closed from another loop cancelled uvicorn (D-64), and bursts of short sessions
    (~80 per symbol while loading history) were answered "Session not found" (D-67)."""
    fake = FakeCTrader()
    fake.bars[("XAUUSD", "D1")] = [(1_700_000_000 + i * 86400, 2600, 2601, 2599, 2600.5) for i in range(400)]
    inner = connector_for(build_server(fake))
    opened = []

    def connect(url, token):
        opened.append(asyncio.current_task())
        return inner(url, token)

    async def go():
        c = CTraderClient("https://fake/trading/mcp", "tok.en.x", connector=connect)
        tasks = [asyncio.create_task(c.calibrate(n, [d], b)) for n, d, b in
                 (("BTCUSD", 2, [25000, 240000]), ("XAUUSD", 3, [1500, 14000]))]
        digits = await asyncio.gather(*tasks)
        await asyncio.create_task(c.trendbars("XAUUSD", "D1", 1_700_000_000, 1_700_000_000 + 400 * 86400))
        return digits, tasks, c._owner

    digits, tasks, owner = run(go())
    assert digits == [2, 3] and opened == [owner] and owner not in tasks
    assert len([t for t, _ in fake.calls if t == "get_trendbars"]) >= 14  # 720 h chunks, all on one session


def test_server_keeping_only_the_newest_session_never_rejects_mapex():
    fake = FakeCTrader()
    connect = evicting_connector(fake, build_server(fake))

    async def go():
        c = CTraderClient("https://fake/trading/mcp", "tok.en.x", connector=connect)
        await asyncio.gather(*(c.calibrate(n, [d], b) for n, d, b in
                               (("BTCUSD", 2, [25000, 240000]), ("XAUUSD", 3, [1500, 14000]))),
                             *(c.call("get_version") for _ in range(5)))

    run(go())
    assert fake.evicted == 0


def test_order_session_error_inside_a_tool_result_is_never_resent():
    fake = FakeCTrader()
    fake.fail_once["create_order"] = "Session not found; re-initialize"

    async def go():
        c = client_for(fake)
        await c.load_symbols()
        with pytest.raises(TransportError):
            await c.call("create_order", {"symbolId": 41, "orderType": "MARKET", "tradeSide": "BUY", "volume": 100})

    run(go())
    assert [t for t, _ in fake.calls].count("create_order") == 1

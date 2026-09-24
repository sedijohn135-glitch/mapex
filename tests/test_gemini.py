"""D-71: Gemini maps (GEM1), MAPEX executes — the map intake and the token-protected /mcp endpoint."""

import asyncio
import json

import pytest
from starlette.testclient import TestClient

from mapex import config
from mapex.core import primitives as pr
from mapex.core.timeutil import TF_SECONDS
from mapex.ctrader.client import Quote
from mapex.gemini_map import MapRejected, accept, parse
from mapex.main import App
from mapex.mcp_api import ict_clock
from mapex.pipeline import current_map, run_executor, save_map
from tests.helpers import ny_ts
from tests.synth import market_m1

TOKEN = "k" * 32
BLIND = {"no_valid_strategic_map", "active_causal_chain_missing", "strategic_map_stale"}


@pytest.fixture(scope="module")
def mkt():
    m = market_m1(days_m1=10, days_total=120, seed=3, price=2650.0)
    now = m["M1"][-1].t + 65
    closed = {tf: [b for b in bars if b.t + TF_SECONDS.get(tf, 31 * 86400) <= now] for tf, bars in m.items()}
    return closed, now, closed["M1"][-1].c, pr.atr(closed["D1"], 14)


def gem1(price, atr, bias="sell", **zone):
    up = 1 if bias == "sell" else -1  # a sell zone sits above price, a buy zone below
    lo, hi = sorted((price + up * 0.3 * atr, price + up * (0.3 * atr + 4)))
    kz = {"id": "CHAIN_A", "direction": bias, "timeframe": "H1", "zone_type": "FVG", "zone_low": f"{lo:.2f}",
          "zone_high": hi, "generating_liquidity_id": "LIQ_001", "tp1": price - up * 10, "tp2": price - up * 25,
          **zone}
    root = hi + 2 if bias == "sell" else lo - 2
    return {"strategic_bias": bias.upper(), "key_zones": [kz], "final_lrlr_objective": price - up * 40,
            "liquidity_registry": [{"id": "LIQ_001", "type": "SWING_H", "timeframe": "H1", "price_level": root,
                                    "lps": 72, "status": "SWEPT_WICK"}]}


def test_a_gemini_map_is_checked_then_becomes_the_executor_map(mkt, tmp_path):
    bars, now, price, atr = mkt
    s = config.load({"DATA_DIR": str(tmp_path)})
    raw = "```json\n" + json.dumps(gem1(price, atr)) + "\n```"  # pasted as Gemini prints it
    res = accept(raw, "XAUUSD", s, bars, price, now)
    m = res.json
    z = m["key_zones"][0]
    assert res.valid and m["source"] == "gemini" and m["strategic_bias"] == "sell"
    assert z["zone_low"] == f"{price + 0.3 * atr:.2f}" and z["time_horizon"] in ("INTRADAY", "SWING_HTF")
    assert m["active_causal_chain"]["root_liquidity_id"] == "LIQ_001" and float(m["session_context"]["session_atr"])
    app = App(s, clock=lambda: now)
    save_map(app.store, "XAUUSD", now, res)
    got, meta = current_map(app.store, "XAUUSD")
    assert got["source"] == "gemini" and meta["zone_keys"]["CHAIN_A"].endswith("|gemini")
    out = run_executor(app.store, s, "XAUUSD", now, bars, price, price + 0.2, True)
    assert not [d for d in out.decisions if d.reason in BLIND]  # GEM2 preflight accepts the Gemini map


def test_bad_maps_are_refused_with_the_reason(mkt):
    bars, now, price, atr = mkt
    s = config.load({})

    def reasons(g):
        with pytest.raises(MapRejected) as exc:
            accept(g, "XAUUSD", s, bars, price, now)
        return " ".join(exc.value.reasons)

    assert "no JSON object" in reasons("the map is above")
    assert "strategic_bias" in reasons({**gem1(price, atr), "strategic_bias": "neutral"})
    assert "wrong side" in reasons(gem1(price, atr, zone_low=price - 30, zone_high=price - 20))
    assert "not a price" in reasons(gem1(price, atr, zone_low="axis 12", zone_high=13))
    assert "D1 ATR" in reasons(gem1(price, atr, zone_low=price + 5 * atr, zone_high=price + 5 * atr + 3))
    assert "not the sell bias" in reasons(gem1(price, atr, direction="buy"))
    assert "tp1/tp2" in reasons({**gem1(price, atr, tp1=None, tp2=price + 900), "final_lrlr_objective": None})


def test_repairs_are_reported_as_warnings(mkt):
    bars, now, price, atr = mkt
    g = gem1(price, atr, tp1=None, generating_liquidity_id="LIQ_404")
    g["key_zones"].append({**g["key_zones"][0], "id": "CHAIN_B", "zone_low": price - 50, "zone_high": price - 40})
    res = accept(g, "XAUUSD", config.load({}), bars, price, now)
    z = res.json["key_zones"]
    assert [x["id"] for x in z] == ["CHAIN_A"]  # CHAIN_B below a sell price is dropped, CHAIN_A still trades
    assert z[0]["generating_liquidity_id"] == "LIQ_CHAIN_A" and float(z[0]["tp1"]) == pytest.approx(price - 25)
    text = " ".join(res.meta["warnings"])
    assert "CHAIN_B" in text and "LIQ_404" in text and "wrong side" in text
    assert parse({"a": 1}) == {"a": 1}


def service(tmp_path, mkt, **env):
    bars, now, price, _ = mkt
    s = config.load({"DATA_DIR": str(tmp_path), **env})
    app = App(s, clock=lambda: now)
    app.active = ["XAUUSD"]
    app.candles.cache = {("XAUUSD", tf): b for tf, b in bars.items()}

    async def cached(sym, tf, t):
        return app.candles.cache[(sym, tf)]

    app.candles.refresh = cached
    app.quotes.update({"XAUUSD": Quote(price, price + 0.25, now, now)})
    return app


def rpc(client, method, params=None, key=TOKEN, headers=None):
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    h = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json", **(headers or {})}
    return client.post(f"/mcp{f'?key={key}' if key else ''}", json=body, headers=h)


def call(client, name, **args):
    r = rpc(client, "tools/call", {"name": name, "arguments": args})
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    assert not result.get("isError"), result
    return result.get("structuredContent") or json.loads(result["content"][0]["text"])


def test_mcp_needs_the_token_and_gives_gemini_live_data(tmp_path, mkt):
    bars, now, price, atr = mkt
    app = service(tmp_path, mkt, MCP_TOKEN=TOKEN)
    with TestClient(app.asgi()) as c:
        assert c.get("/health").status_code == 200  # public: Railway's health check
        assert rpc(c, "tools/list", key=None).status_code == 401
        assert rpc(c, "tools/list", key="wrong").status_code == 401
        tools = rpc(c, "tools/list", key=None, headers={"Authorization": f"Bearer {TOKEN}"}).json()["result"]["tools"]
        assert {t["name"] for t in tools} == {"market_snapshot", "market_candles", "submit_gem1_map",
                                              "executor_status"}
        snap = call(c, "market_snapshot", symbol="xauusd")
        assert snap["bid"] == round(price, 2) and snap["pdh"] == round(bars["D1"][-1].h, 2)
        assert snap["session_atr"] > 0 and snap["weekly_open"] is not None
        assert {"mapex_entry_window", "ict_now", "ict_next"} <= set(snap)
        got = call(c, "market_candles", symbol="XAUUSD", timeframe="m15", count=3)
        assert len(got["bars"]) == 3 and got["bars"][-1][4] == round(bars["M15"][-1].c, 2)
        assert call(c, "submit_gem1_map", symbol="XAUUSD", gem1_json="{}")["accepted"] is False
        ok = call(c, "submit_gem1_map", symbol="XAUUSD", gem1_json=json.dumps(gem1(price, atr)))
        assert ok["accepted"] and ok["zones"][0]["id"] == "CHAIN_A"
        st = call(c, "executor_status", symbol="XAUUSD")
        assert st["map"]["source"] == "gemini" and not st["map"]["stale"] and st["zones"][0]["state"] == "WATCH"
    assert TOKEN not in json.dumps(app.health())


def test_mcp_is_closed_without_a_token_and_mapex_mapper_is_off(tmp_path, mkt):
    _, now, _, _ = mkt
    app = service(tmp_path, mkt)
    assert app.s.map_source == "gemini" and any("MCP_TOKEN" in w for w in app.s.warnings)
    with TestClient(app.asgi()) as c:
        assert rpc(c, "tools/list").status_code == 503
    asyncio.run(app.map_symbol("XAUUSD", now))
    assert current_map(app.store, "XAUUSD")[0] is None  # Gemini owns the map: no built-in GEM1 run
    assert config.load({"MAP_SOURCE": "mapex"}).map_source == "mapex"
    assert any("MAP_SOURCE" in x for x in config.load({"MAP_SOURCE": "x"}).errors)


def test_old_gemini_map_alerts_once(tmp_path, mkt):
    bars, now, price, atr = mkt
    app = service(tmp_path, mkt, MCP_TOKEN=TOKEN)
    old = now - 7 * 3600
    save_map(app.store, "XAUUSD", old, accept(gem1(price, atr), "XAUUSD", app.s, bars, price, old))
    res = run_executor(app.store, app.s, "XAUUSD", now, bars, price, price + 0.2, True)
    app.gemini_alert("XAUUSD", now, res)  # 20:00 NY: no kill zone, nothing to miss yet
    assert not app.store.all("SELECT text FROM outbox WHERE dedupe LIKE 'gemini-%'")
    for _ in range(2):
        app.gemini_alert("XAUUSD", now + 7 * 3600, res)  # 03:00 NY London kill zone
    sent = app.store.all("SELECT text FROM outbox WHERE dedupe LIKE 'gemini-%'")
    assert len(sent) == 1 and "skadoi" in sent[0]["text"]


def test_ict_clock_knows_every_window_of_the_owners_prompt():
    c = ict_clock("XAUUSD", ny_ts(2026, 9, 22, 10, 55))  # Tuesday
    assert c["ict_now"] == ["London Close", "AM Silver Bullet", "Macro London Close"]
    assert c["ict_next"] == {"windows": ["Macro NY Lunch 11:50-12:10"], "starts_ny": "Tue 11:50", "in_min": 55}
    assert ict_clock("XAUUSD", ny_ts(2026, 9, 22, 12, 30))["ict_now"] == ["NY Lunch - no trade"]
    assert ict_clock("XAUUSD", ny_ts(2026, 9, 22, 1, 0))["ict_next"]["windows"] == ["London Opening Range 01:30-02:00"]
    weekend = ict_clock("XAUUSD", ny_ts(2026, 9, 25, 17, 30))  # Friday after the close: gold is shut
    assert weekend["ict_now"] == [] and weekend["ict_next"]["starts_ny"] == "Sun 19:00"

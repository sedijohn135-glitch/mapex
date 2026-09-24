"""Service-level tests: start-up without secrets, /health independence, mode selection, lease, log redaction."""

import asyncio
import base64
import json
import logging

from mapex import config, guards
from mapex.main import App, RedactFilter, effective_mode, resolve_credentials, setup_logging
from mapex.store import Store
from tests.fake_ctrader import FakeCTrader, build_server, connector_for
from tests.helpers import ny_ts

NOW = ny_ts(2026, 9, 22, 9, 22)


def tok(env):
    head = base64.urlsafe_b64encode(json.dumps({"environment": env}).encode()).decode().rstrip("=")
    return f"{head}.payload.signature123"


def test_starts_with_no_secrets_and_health_is_200(tmp_path):
    s = config.load({"DATA_DIR": str(tmp_path)})
    app = App(s, clock=lambda: NOW)
    asyncio.run(app.startup())
    from starlette.testclient import TestClient
    r = TestClient(app.asgi()).get("/health")
    assert r.status_code == 200
    h = r.json()
    assert h["mode"] == "paper" and h["ctrader"]["configured"] is False
    assert "🚀 MAPEX u ndez" in app.store.one("SELECT text FROM outbox")["text"]


def test_health_never_depends_on_broker_and_never_leaks_token(tmp_path):
    t = tok("demo")
    s = config.load({"DATA_DIR": str(tmp_path), "CTRADER_MCP_TOKEN": t, "LOT_XAUUSD": "0.1"})

    def dead_connector(url, token):
        raise ConnectionError("cTrader unreachable")

    app = App(s, clock=lambda: NOW, connector=dead_connector)
    asyncio.run(app.startup())
    from starlette.testclient import TestClient
    r = TestClient(app.asgi()).get("/health")
    assert r.status_code == 200 and t not in r.text and "payload" not in r.text


def test_effective_mode_live_confirmation():
    live = config.load({"TRADING_MODE": "live"})
    assert effective_mode(live, "demo") == ("live", False)
    assert effective_mode(live, "live") == ("paper", True)  # M7: live account without confirmation
    assert effective_mode(live, "unknown") == ("paper", True)
    ok = config.load({"TRADING_MODE": "live", "CONFIRM_LIVE_ACCOUNT": "YES"})
    assert effective_mode(ok, "live") == ("live", False)
    assert effective_mode(config.load({}), "live") == ("paper", False)


def test_credentials_precedence():
    db = Store(":memory:")
    s = config.load({"CTRADER_MCP_CONFIG": tok("demo"), "CTRADER_MCP_TOKEN": tok("live")})
    assert resolve_credentials(s, db)[1] == tok("demo")
    db.put("ctrader_config", f"Bearer {tok('live')}")
    assert resolve_credentials(s, db)[2] == "telegram"


def test_lease_only_one_leader():
    db = Store(":memory:")
    assert db.acquire_lease("a", 1000)
    assert not db.acquire_lease("b", 1010)  # the loser runs read-only
    assert db.acquire_lease("b", 1100)  # after the TTL the lease moves


def test_log_redaction_masks_tokens(caplog):
    t = tok("live")
    f = RedactFilter([t, "123456:SECRETSECRET"])
    rec = logging.LogRecord("x", logging.INFO, "", 0, "Authorization: Bearer %s bot123456:SECRETSECRET %s",
                            (t, t), None)
    f.filter(rec)
    out = rec.getMessage()
    assert t not in out and "SECRETSECRET" not in out
    setup_logging("INFO", [t])


def test_live_app_with_fake_broker_calibrates_and_commands(tmp_path):
    fake = FakeCTrader()
    s = config.load({"DATA_DIR": str(tmp_path), "CTRADER_MCP_TOKEN": tok("demo"), "TRADING_MODE": "live",
                     "LOT_XAUUSD": "0.10", "LOT_BTCUSD": "0.01"})
    app = App(s, clock=lambda: NOW, connector=connector_for(build_server(fake)))

    async def go():
        await app.startup()
        out = {}
        for cmd, args in (("/stop", ""), ("/status", ""), ("/resume", ""), ("/flat", ""), ("/flat", "yes"),
                          ("/trades", "7"), ("/map", "XAUUSD"), ("/health", "")):
            out[(cmd, args)] = await app.command(cmd, args)
        out["ctrader"] = await app.command("/ctrader", f'"url": "https://x/trading/mcp", "headers": '
                                                       f'{{"Authorization": "Bearer {tok("live")}"}}')
        return out

    out = asyncio.run(go())
    assert app.active == ["XAUUSD", "BTCUSD"] and app.client.digits == {"XAUUSD": 3, "BTCUSD": 2}
    assert app.mode == "paper" and app.forced_paper  # a live token without CONFIRM_LIVE_ACCOUNT -> paper
    assert "Kill switch: ON" in out[("/status", "")]
    assert guards.kill_switch(app.store) is None
    assert "Konfirmo" in out[("/flat", "")] and "U mbyllën 0" in out[("/flat", "yes")]
    assert "Nuk ka hartë" in out[("/map", "XAUUSD")]
    assert "Tokeni u rinovua" in out["ctrader"] and tok("live") not in out["ctrader"]


def test_expired_token_alerts_once_and_trips_after_15_minutes(tmp_path):
    fake = FakeCTrader()
    fake.auth_fail = True
    clock = [NOW]
    s = config.load({"DATA_DIR": str(tmp_path), "CTRADER_MCP_TOKEN": tok("demo"), "LOT_XAUUSD": "0.1"})
    app = App(s, clock=lambda: clock[0], connector=connector_for(build_server(fake)))
    asyncio.run(app.startup())
    asyncio.run(app.heartbeat_tick())
    texts = [r["text"] for r in app.store.all("SELECT text FROM outbox")]
    assert sum("TOKENI I CTRADER SKADOI" in t for t in texts) == 1
    assert guards.kill_switch(app.store) is None
    clock[0] += 16 * 60
    asyncio.run(app.heartbeat_tick())
    assert "tokeni" in guards.kill_switch(app.store)
    assert sum("TOKENI I CTRADER SKADOI" in r["text"] for r in app.store.all("SELECT text FROM outbox")) == 1


def test_tracebacks_are_redacted_too():
    f = RedactFilter(["123456:SECRETSECRET"])
    try:
        raise ConnectionError("POST https://api.telegram.org/bot123456:SECRETSECRET/sendMessage failed")
    except ConnectionError:
        import sys
        rec = logging.LogRecord("x", logging.ERROR, "", 0, "send failed", (), sys.exc_info())
    f.filter(rec)
    out = logging.Formatter().format(rec)
    assert "SECRETSECRET" not in out and "send failed" in out


def test_startup_message_explains_each_symbol(tmp_path):
    fake = FakeCTrader()
    fake.quotes["BTCUSD"] = (5.0, 5.1, 0)  # a price no digits can decode into the BTC band
    s = config.load({"DATA_DIR": str(tmp_path), "CTRADER_MCP_TOKEN": tok("demo"), "LOT_XAUUSD": "5.0",
                     "LOT_BTCUSD": "0.01"})
    app = App(s, clock=lambda: NOW, connector=connector_for(build_server(fake)))
    asyncio.run(app.startup())
    boot = app.store.one("SELECT text FROM outbox WHERE dedupe LIKE 'boot:%'")["text"]
    assert "⛔ XAUUSD: loti refuzohet" in boot and "MAX_LOT" in boot
    assert "⛔ BTCUSD: çmimet nuk u dekoduan (bid i marrë:" in boot
    stop = app.store.one("SELECT text FROM outbox WHERE dedupe LIKE 'kill:%'")["text"]
    assert "bid i marrë" in stop

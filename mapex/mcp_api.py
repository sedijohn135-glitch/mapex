"""D-71: the /mcp endpoint Gemini connects to — live candles and levels out, GEM1 maps in.

Nothing here can place, change or close an order: a map only tells the GEM2 executor where to watch, and a trade
still opens only when P1+P2+P3+P4 = 100 and every guard passes. /mcp answers only with MCP_TOKEN
(`?key=` in the connector URL, or `Authorization: Bearer`); /health stays public.
"""

from __future__ import annotations

import hmac
import logging
from urllib.parse import parse_qs

from mcp.server.mcpserver import MCPServer
from starlette.responses import JSONResponse

from mapex import gemini_map
from mapex.core import levels as lv
from mapex.core import primitives as pr
from mapex.core.timeutil import current_session, fmt_ny, killzone, market_open, ny, ny_at, ny_midnight, week_start
from mapex.executor.state import load_states
from mapex.guards import kill_switch, open_trades
from mapex.pipeline import current_map, save_map

log = logging.getLogger("mapex.mcp")
TFS = ("M1", "M5", "M15", "H1", "H4", "D1", "W1", "MN1")
MAX_CANDLES = 500
# The owner's ICT clock, New York time (ICT Sniper V13 §3.2 kill zones, §3.3 macros = stated time ±10 min, §5.5
# Silver Bullets). Context for Gemini only: MAPEX itself enters only inside the GEM2 kill zones (mapex_entry_window).
ICT_TIMES = (
    ("Asian Range (M5 high/low)", "19:00", "24:00"),
    ("London Opening Range", "01:30", "02:00"),
    ("London Open Kill Zone", "02:00", "05:00"),
    ("London Silver Bullet", "03:00", "04:00"),
    ("NY Opening Range", "07:00", "07:30"),
    ("NY Open Kill Zone", "07:00", "10:00"),
    ("Judas Swing window", "09:30", "10:00"),
    ("Equities Opening Range (indices)", "09:30", "10:00"),
    ("London Close", "10:00", "12:00"),
    ("AM Silver Bullet", "10:00", "11:00"),
    ("NY Lunch - no trade", "12:00", "13:00"),
    ("PM Opening Range", "13:30", "14:00"),
    ("PM Session", "13:30", "16:00"),
    ("PM Silver Bullet", "14:00", "15:00"),
    ("Last Hour", "15:00", "16:00"),
    ("Macro London Open 02:33", "02:23", "02:43"),
    ("Macro London Continuation 04:03", "03:53", "04:13"),
    ("Macro Pre-NY Open", "07:50", "08:10"),
    ("Macro Pre-Open", "08:50", "09:10"),
    ("Macro NY Open", "09:50", "10:10"),
    ("Macro London Close", "10:50", "11:10"),
    ("Macro NY Lunch", "11:50", "12:10"),
    ("Macro PM Session Start", "13:10", "13:30"),
    ("Macro PM", "14:50", "15:10"),
    ("Macro Last Hour 15:15", "15:05", "15:25"),
    ("Macro Last Hour 15:40", "15:30", "15:50"),
    ("Macro Last Hour 15:50", "15:40", "16:00"),
    ("Macro Last Hour 16:00", "15:50", "16:10"),
)
INSTRUCTIONS = (
    "MAPEX is the owner's GEM2 executor on IC Markets cTrader. You are the GEM1 strategist: read prices only from "
    "market_snapshot and market_candles (times are New York), build the GEM1 map, send it with submit_gem1_map and "
    "fix whatever it reports. MAPEX then watches the zones and trades only when GEM2 scores 100."
)


def _min(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def ict_clock(symbol: str, now: float) -> dict:
    """ICT windows open right now and the next one(s) to open, DST-safe; none while the symbol's market is shut."""
    d = ny(now)
    t = d.hour * 60 + d.minute
    active = [n for n, a, b in ICT_TIMES if _min(a) <= t < _min(b)] if market_open(symbol, now) else []
    starts = sorted((ny_at(now, *divmod(_min(a), 60), day), n, a, b) for day in range(4) for n, a, b in ICT_TIMES)
    starts = [x for x in starts if x[0] > now and market_open(symbol, x[0] + 60)]
    first = starts[0][0] if starts else None
    return {"ict_now": active,
            "ict_next": None if first is None else {
                "windows": [f"{n} {a}-{b}" for s, n, a, b in starts if s == first],
                "starts_ny": f"{ny(first):%a %H:%M}", "in_min": int((first - now) // 60)}}


class TokenGate:
    """ASGI guard for /mcp: 503 while MCP_TOKEN is unset, 401 for a wrong or missing key."""

    def __init__(self, app, token: str):
        self.app, self.token = app, token

    def allowed(self, scope) -> bool:
        key = (parse_qs(scope.get("query_string", b"").decode()).get("key") or [""])[0]
        for name, value in scope.get("headers", []):
            if name == b"authorization" and value.decode().lower().startswith("bearer "):
                key = key or value.decode()[7:].strip()
        return bool(key) and hmac.compare_digest(key.encode(), self.token.encode())

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith("/mcp"):
            if not self.token:
                await JSONResponse({"error": "MCP_TOKEN is not set on Railway"}, 503)(scope, receive, send)
                return
            if not self.allowed(scope):
                await JSONResponse({"error": "unauthorized"}, 401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def build(app):
    """The service's ASGI app: /mcp (token) + /health and / (public)."""
    server = MCPServer("mapex", instructions=INSTRUCTIONS)

    def symbol_of(symbol: str) -> str:
        sym = symbol.strip().upper()
        if sym not in app.active:
            raise ValueError(f"{sym} is not active on MAPEX (active: {', '.join(app.active) or 'none yet'})")
        return sym

    async def bars(sym: str, now: float, tfs) -> dict:
        for tf in tfs:
            await app.candles.refresh(sym, tf, now)
        return {tf: app.candles.closed(sym, tf, now) for tf in tfs}

    async def quote(sym: str, now: float):
        q = app.quotes.fresh(sym, now)
        if q is None:
            app.quotes.update(await app.client.spot([sym]))
            q = app.quotes.fresh(sym, now)
        return q

    @server.tool()
    async def market_snapshot(symbol: str) -> dict:
        """Live bid/ask, New York time, session, the ICT windows open now and the next one (kill zones, opening
        ranges, Silver Bullets, macros, NY Lunch), the GEM2 window where MAPEX may enter, session ATR, D1 ATR and the
        key levels (NY midnight open, weekly open, previous day/week/month high and low). Call it before mapping."""
        sym = symbol_of(symbol)
        now = app.clock()
        b = await bars(sym, now, ("M15", "H1", "D1", "W1", "MN1"))
        q = await quote(sym, now)
        dec = app.s.display_decimals.get(sym, 2)

        def f(x):
            return None if x is None else round(x, dec)

        def prev(tf, side):
            return f(getattr(b[tf][-1], side)) if b[tf] else None

        s_atr, _ = lv.session_atr(b["M15"], b["H1"], now)
        return {
            "symbol": sym, "date_ny": f"{ny(now):%Y-%m-%d}", "time_ny": fmt_ny(now),
            "weekday_ny": ny(now).strftime("%A"),
            "bid": f(q.bid) if q else None, "ask": f(q.ask) if q else None, "spread": f(q.spread) if q else None,
            "session": current_session(now), "mapex_entry_window": killzone(now), **ict_clock(sym, now),
            "session_atr": f(s_atr), "d1_atr14": f(pr.atr(b["D1"], 14)) if len(b["D1"]) > 14 else None,
            "ny_midnight_open": f(lv.day_open_at(b["M15"], ny_midnight(now))),
            "weekly_open": f(lv.day_open_at(b["H1"], week_start(now))),
            "pdh": prev("D1", "h"), "pdl": prev("D1", "l"), "pwh": prev("W1", "h"), "pwl": prev("W1", "l"),
            "pmh": prev("MN1", "h"), "pml": prev("MN1", "l"),
            "decimals": dec, "price_band": app.s.price_bands.get(sym),
        }

    @server.tool()
    async def market_candles(symbol: str, timeframe: str, count: int = 200) -> dict:
        """Closed candles from IC Markets, oldest first, as [time_ny, open, high, low, close].
        timeframe: M1, M5, M15, H1, H4, D1, W1 or MN1; count: up to 500."""
        sym, tf = symbol_of(symbol), timeframe.strip().upper()
        if tf not in TFS:
            raise ValueError(f"timeframe must be one of {', '.join(TFS)}")
        now = app.clock()
        dec = app.s.display_decimals.get(sym, 2)
        rows = (await bars(sym, now, (tf,)))[tf][-max(1, min(count, MAX_CANDLES)):]
        return {"symbol": sym, "timeframe": tf, "time_zone": "America/New_York",
                "columns": ["time_ny", "open", "high", "low", "close"],
                "bars": [[f"{ny(x.t):%Y-%m-%d %H:%M}", *(round(v, dec) for v in (x.o, x.h, x.l, x.c))]
                         for x in rows]}

    @server.tool()
    async def submit_gem1_map(symbol: str, gem1_json: str) -> dict:
        """Send the complete GEM1 JSON (as text). MAPEX checks every price against live data; the accepted map
        replaces the previous one and the executor starts watching its CHAIN_A/B at once."""
        sym = symbol_of(symbol)
        if app.s.map_source != "gemini":
            return {"accepted": False, "errors": ["MAP_SOURCE=mapex on Railway: the built-in mapper owns the map"]}
        now = app.clock()
        b = await bars(sym, now, ("M15", "H1", "D1"))
        q = await quote(sym, now)
        price = q.bid if q else (b["M15"][-1].c if b["M15"] else 0.0)
        try:
            res = gemini_map.accept(gem1_json, sym, app.s, b, price, now)
        except gemini_map.MapRejected as exc:
            log.info("%s Gemini map refused: %s", sym, exc)
            return {"accepted": False, "errors": exc.reasons}
        save_map(app.store, sym, now, res)
        _, meta = current_map(app.store, sym)
        log.info("%s Gemini map %s accepted: bias=%s zones=%s", sym, meta.get("map_id"),
                 res.json["strategic_bias"], [z["id"] for z in res.json["key_zones"]])
        return {"accepted": True, "map_id": meta.get("map_id"), "bias": res.json["strategic_bias"],
                "zones": [{k: z[k] for k in ("id", "zone_low", "zone_high", "time_horizon", "tp1", "tp2")}
                          for z in res.json["key_zones"]],
                "valid_until_ny": fmt_ny(now + app.s.map_max_age_h * 3600), "warnings": res.meta["warnings"]}

    @server.tool()
    async def executor_status(symbol: str) -> dict:
        """The map MAPEX is trading from, each zone's GEM2 state, the latest executor events and open MAPEX trades."""
        sym = symbol_of(symbol)
        now = app.clock()
        m, meta = current_map(app.store, sym)
        states = load_states(app.store, sym)
        events = app.store.all("SELECT ts, chain, state, output, reason FROM events WHERE symbol=? "
                               "ORDER BY id DESC LIMIT 10", (sym,))
        zones = []
        for kz in (m or {}).get("key_zones", []):
            st = states.get(meta["zone_keys"].get(kz["id"], ""))
            zones.append({"id": kz["id"], "zone": f"{kz['zone_low']}-{kz['zone_high']}",
                          "state": st.state if st else "WATCH", "sweeps": st.sweep_count if st else 0})
        return {
            "symbol": sym, "time_ny": fmt_ny(now), "mode": app.mode, "kill_switch": kill_switch(app.store),
            "map": None if not m else {
                "map_id": meta.get("map_id"), "source": m.get("source", "mapex"), "bias": m["strategic_bias"],
                "created_ny": fmt_ny(meta["created_at"]), "age_min": int((now - meta["created_at"]) // 60),
                "stale": now - meta["created_at"] > app.s.map_max_age_h * 3600},
            "zones": zones,
            "events": [{"time_ny": fmt_ny(r["ts"]), "chain": r["chain"], "state": r["state"], "output": r["output"],
                        "reason": r["reason"]} for r in events],
            "open_trades": [{"side": t["side"], "state": t["state"], "entry": t["entry_fill"], "sl": t["sl"]}
                            for t in open_trades(app.store) if t["symbol"] == sym],
        }

    async def health(_request):
        try:
            return JSONResponse(app.health())
        except Exception as exc:  # noqa: BLE001 — /health must answer while the process lives
            return JSONResponse({"status": "degraded", "error": type(exc).__name__})

    server.custom_route("/health", methods=["GET"])(health)
    server.custom_route("/", methods=["GET"])(health)
    web = server.streamable_http_app(streamable_http_path="/mcp", json_response=True, stateless_http=True,
                                     host="0.0.0.0")
    return TokenGate(web, app.s.mcp_token)

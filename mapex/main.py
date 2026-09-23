"""MAPEX entrypoint: one asyncio process — scheduler, lease, /health, Telegram. Run: python -m mapex.main"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
import uuid

from mapex import config, guards
from mapex.config import Settings
from mapex.core.timeutil import fmt_ny, market_open, trading_day
from mapex.ctrader.broker import LiveVenue, TradeManager
from mapex.ctrader.client import AuthError, CTraderClient, CTraderError, http_connector
from mapex.ctrader.decode import DEFAULT_URL, DecodeError, mask, parse_mcp_config, token_environment
from mapex.ctrader.paper import PaperVenue
from mapex.data.candles import Candles
from mapex.data.quotes import Quotes
from mapex.executor.state import load_states
from mapex.mapper.engine import brief
from mapex.pipeline import current_map, run_executor, run_mapper
from mapex.store import Store
from mapex.telegram import TelegramBot, e, msg_startup, msg_token_expired

log = logging.getLogger("mapex")
AUTH_TRIP_S = 15 * 60


# ---------------------------------------------------------------- logging with secret redaction (P9)

class RedactFilter(logging.Filter):
    PATTERNS = [re.compile(r"Bearer\s+[A-Za-z0-9._\-]+"), re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-.]+"),
                re.compile(r"bot\d+:[A-Za-z0-9_\-]+")]

    def __init__(self, secrets: list[str] | None = None):
        super().__init__()
        self.secrets = [x for x in (secrets or []) if x and len(x) >= 8]

    def redact(self, text: str) -> str:
        for sec in self.secrets:
            text = text.replace(sec, mask(sec))
        for p in self.PATTERNS:
            text = p.sub("***", text)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        red = self.redact(msg)
        if red != msg:
            record.msg, record.args = red, ()
        if record.exc_info and not record.exc_text:  # tracebacks may carry URLs with tokens
            record.exc_text = self.redact(logging.Formatter().formatException(record.exc_info))
        return True


def setup_logging(level: str, secrets: list[str]) -> RedactFilter:
    f = RedactFilter(secrets)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(f)
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(getattr(logging, level, logging.INFO))
    for noisy in ("httpx", "httpx2", "mcp", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return f


def effective_mode(s: Settings, env: str) -> tuple[str, bool]:
    """(mode, forced_paper). A live-environment (or undecodable) token needs CONFIRM_LIVE_ACCOUNT=YES (M7)."""
    if s.trading_mode != "live":
        return "paper", False
    if env == "demo" or s.confirm_live:
        return "live", False
    return "paper", True


def resolve_credentials(s: Settings, store: Store) -> tuple[str, str, str]:
    """Precedence: Telegram /ctrader override (kv) -> CTRADER_MCP_CONFIG -> CTRADER_MCP_URL + CTRADER_MCP_TOKEN."""
    for source, raw in (("telegram", store.get("ctrader_config")), ("env", s.ctrader_config)):
        if raw:
            try:
                url, token = parse_mcp_config(raw)
                return url, token, source
            except DecodeError:
                s.errors.append(f"cTrader configuration from {source} could not be parsed")
    if s.ctrader_token:
        try:
            url, token = parse_mcp_config(s.ctrader_token)
        except DecodeError:
            url, token = DEFAULT_URL, s.ctrader_token
        return s.ctrader_url or url, token, "env"
    return "", "", "none"


class App:
    def __init__(self, s: Settings, clock=time.time, connector=http_connector, tg_http=None, db_path=None):
        self.s, self.clock = s, clock
        self.owner = uuid.uuid4().hex[:12]
        self.leader = False
        self.db_error = None
        try:
            self.store = Store(db_path or s.db_path)
        except Exception as exc:  # noqa: BLE001 — never die: run in memory, trip the kill switch loudly
            self.db_error = f"{type(exc).__name__}: {exc}"
            self.store = Store(":memory:")
        url, token, self.cred_source = resolve_credentials(s, self.store)
        self.client = CTraderClient(url, token, connector=connector, clock=clock)
        self.env = token_environment(token) if token else "unknown"
        self.mode, self.forced_paper = effective_mode(s, self.env)
        self.candles = Candles(self.client)
        self.quotes = Quotes()
        self.venue = self._venue(self.mode)
        self.tm = TradeManager(self.store, s, self.venue, clock=clock, account=self.env)
        self.tm.auth_ok = lambda: self.client.auth_error_since is None
        self.tm.skew = lambda: self.client.skew_s
        self.bot = TelegramBot(s.telegram_token, s.telegram_chat_id, self.store, http=tg_http, clock=clock)
        self.active: list[str] = []
        self.disabled: dict[str, str] = {}
        self.map_due: dict[str, float] = {}
        self.last_m1: dict[str, int] = {}
        self.last_error: str | None = None
        self.auth_alerted = False
        self.flat_armed = 0.0
        self.replay_task: asyncio.Task | None = None
        self.started_at = clock()

    def _venue(self, mode: str):
        if mode == "live":
            return LiveVenue(self.client, self.s)
        return PaperVenue(self.store, self.s, lambda sym: self.quotes.fresh(sym, self.clock()), self.clock)

    # ------------------------------------------------------------- start-up
    async def startup(self) -> None:
        now = self.clock()
        boot = int(self.started_at)
        self.leader = self.store.acquire_lease(self.owner, now)
        if self.db_error:
            guards.trip(self.store, "gabim baze të dhënash", now)
        notes = list(self.s.errors)
        if not self.s.volume_mounted:
            notes.append("Pa Volume në Railway: historiku dhe mbrojtja nga urdhrat e dyfishtë humbasin në çdo deploy.")
        await self.connect()
        for sym in self.s.symbols:
            if sym in self.s.lots and sym in self.active:
                log.info(config.lot_mapping_line(sym, self.s.lots[sym], self.s.contract_size[sym]))
        lots = {k: v for k, v in self.s.lots.items() if k in self.active or not self.client.configured}
        self.store.outbox_add(f"boot:{boot}", msg_startup(self.mode, self.env, lots, self.s, self.forced_paper, notes),
                              critical=True, now=now)
        if self.leader and self.active:
            with contextlib.suppress(CTraderError):
                await self.tm.reconcile()

    async def connect(self) -> None:
        """Symbol map + digits calibration against a live bid; a failed calibration disables the symbol."""
        self.active = []
        if not self.client.configured:
            return
        try:
            await self.client.load_symbols()
        except AuthError:
            self.on_auth_error()
            return
        except CTraderError as exc:
            self.last_error = f"connect: {exc}"
            return
        for sym in self.s.symbols:
            cands = [self.s.price_digits.get(sym), config.DEFAULT_PIP_DIGITS.get(sym)]
            try:
                await self.client.calibrate(sym, cands, self.s.price_bands.get(sym, [0, 1e12]))
                self.active.append(sym)
                self.disabled.pop(sym, None)
            except DecodeError as exc:
                self.disabled[sym] = str(exc)
                guards.trip(self.store, f"çmimet nuk u dekoduan ({sym})", self.clock())
            except CTraderError as exc:
                self.disabled[sym] = str(exc)

    def on_auth_error(self) -> None:
        now = self.clock()
        since = self.client.auth_error_since or now
        if not self.auth_alerted:
            self.auth_alerted = True
            self.store.outbox_add(f"auth:{int(since)}", msg_token_expired(), critical=True, now=now)
        if now - since > AUTH_TRIP_S:
            guards.trip(self.store, "tokeni i cTrader nuk pranohet prej >15 min", now)

    # ------------------------------------------------------------- ticks
    async def lease_tick(self) -> None:
        was = self.leader
        self.leader = self.store.acquire_lease(self.owner, self.clock())
        if self.leader and not was:
            log.info("lease acquired (%s)", self.owner)

    async def quote_tick(self) -> float:
        """Poll live quotes; returns the next interval (1 s near action, else 10 s)."""
        if not (self.leader and self.active):
            return 10.0
        try:
            self.quotes.update(await self.client.spot(self.active))
            self.auth_alerted = False if self.client.auth_error_since is None else self.auth_alerted
        except AuthError:
            self.on_auth_error()
            return 10.0
        except CTraderError as exc:
            self.last_error = f"quotes: {exc}"
            return 10.0
        return 1.0 if (guards.open_trades(self.store) or self.near_zone()) else 10.0

    def near_zone(self) -> bool:
        for sym in self.active:
            m, _ = current_map(self.store, sym)
            q = self.quotes.last.get(sym)
            if not m or not q:
                continue
            s_atr = float(m["session_context"]["session_atr"])
            for kz in m["key_zones"]:
                lo, hi = float(kz["zone_low"]), float(kz["zone_high"])
                d = 0 if lo <= q.bid <= hi else min(abs(q.bid - lo), abs(q.bid - hi))
                if d <= 0.5 * s_atr:
                    return True
        return False

    async def map_symbol(self, sym: str, now: float) -> None:
        for tf in ("MN1", "W1", "D1", "H4", "H1", "M15"):
            await self.candles.refresh(sym, tf, now)
        bars = {tf: self.candles.closed(sym, tf, now) for tf in ("MN1", "W1", "D1", "H4", "H1", "M15")}
        q = self.quotes.last.get(sym)
        price = q.bid if q else (bars["M15"][-1].c if bars["M15"] else 0.0)
        res = run_mapper(self.store, self.s, sym, bars, price, now)
        log.info("%s map: valid=%s reason=%s", sym, res.valid, res.reason)
        self.map_due[sym] = (int(now) // 3600 + 1) * 3600 + 60  # next H1 close + 60 s

    async def minute_tick(self) -> None:
        if not self.leader:
            return
        now = self.clock()
        guards.daily_roll(self.store, now)
        for sym in list(self.active):
            try:
                await self.symbol_tick(sym, now)
            except AuthError:
                self.on_auth_error()
            except Exception as exc:  # noqa: BLE001 — one symbol's failure must not stop the other
                self.last_error = f"{sym}: {type(exc).__name__}: {exc}"
                log.exception("tick %s", sym)

    async def symbol_tick(self, sym: str, now: float) -> None:
        for tf in ("M1", "M5", "M15"):
            await self.candles.refresh(sym, tf, now)
        ok = True
        for tf in ("M1", "M5", "M15"):
            ok = await self.candles.ensure_contiguous(sym, tf, now) and ok
        if isinstance(self.venue, PaperVenue):
            for b in self.candles.closed(sym, "M1", now):
                if b.t > self.last_m1.get(sym, int(now) - 120):
                    self.venue.on_bar(sym, b)
        m1 = self.candles.closed(sym, "M1", now)
        if m1:
            self.last_m1[sym] = m1[-1].t
        if now >= self.map_due.get(sym, 0):
            await self.map_symbol(sym, now)
        q = self.quotes.fresh(sym, now)
        bars = {tf: self.candles.closed(sym, tf, now) for tf in ("M1", "M5", "M15", "D1")}
        res = run_executor(self.store, self.s, sym, now, bars, q.bid if q else None, q.ask if q else None,
                           market_open(sym, now), ok)
        if res.rerun_mapper:
            self.map_due[sym] = 0
        if res.plan:
            fresh = self.quotes.fresh(sym, self.clock())
            out = await self.tm.execute(res.plan, fresh)
            log.info("%s %s -> %s", sym, res.plan.setup_key, out)

    async def manage_tick(self) -> None:
        if self.leader and guards.open_trades(self.store):
            now = self.clock()
            try:
                await self.tm.manage({sym: self.quotes.fresh(sym, now) for sym in self.active})
            except AuthError:
                self.on_auth_error()
            except CTraderError as exc:
                self.last_error = f"manage: {exc}"

    async def reconcile_tick(self) -> None:
        if self.leader and self.active:
            try:
                await self.tm.reconcile()
            except AuthError:
                self.on_auth_error()
            except CTraderError as exc:
                self.last_error = f"reconcile: {exc}"

    async def heartbeat_tick(self) -> None:
        if not self.client.configured:
            return
        try:
            await self.client.call("get_version")
            if not self.active:
                await self.connect()
        except AuthError:
            self.on_auth_error()
        except CTraderError as exc:
            self.last_error = f"heartbeat: {exc}"

    # ------------------------------------------------------------- commands (owner chat only)
    async def command(self, cmd: str, args: str) -> str | None:
        now = self.clock()
        if cmd == "/stop":
            self.store.put(guards.KILL_KEY, "ndalur me /stop")
            return "⏸ Hyrjet e reja u ndalën. Pozicionet e hapura menaxhohen si zakonisht. Rifillo me /resume."
        if cmd == "/resume":
            guards.resume(self.store)
            return "▶️ MAPEX rifilloi hyrjet."
        if cmd == "/flat":
            if args.lower() == "yes" and now - self.flat_armed < 120:
                n = await self.tm.flat()
                return f"✅ U mbyllën {n} pozicione MAPEX."
            self.flat_armed = now
            return "Konfirmo brenda 2 minutave me: /flat yes"
        if cmd in ("/status", "/health"):
            return self.status_text(short=cmd == "/health")
        if cmd == "/map":
            sym = (args or (self.active or self.s.symbols)[0]).upper()
            m, meta = current_map(self.store, sym)
            if not m:
                return f"Nuk ka hartë të vlefshme për {e(sym)}."
            return f"<pre>{e(brief(m))}</pre>"
        if cmd == "/trades":
            days = int(args) if args.strip().isdigit() else 7
            rows = self.store.all("SELECT * FROM trades WHERE opened_at >= ? ORDER BY opened_at",
                                  (int(now - days * 86400),))
            if not rows:
                return f"Asnjë tregti në {days} ditët e fundit."
            lines = [f"📒 Tregtitë e {days} ditëve të fundit:"]
            for r in rows:
                res = "hapur" if r["result_r"] is None and r["state"] != "CLOSED" else (
                    "?" if r["result_r"] is None else f"{r['result_r']:+.2f}R")
                lines.append(f"• {fmt_ny(r['opened_at'])} {e(r['symbol'])} {r['side'].upper()} {r['state']} {res}")
            return "\n".join(lines)
        if cmd == "/replay":
            parts = args.upper().split()
            sym = parts[0] if parts else (self.active or ["XAUUSD"])[0]
            days = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 30
            if sym not in self.active:
                return f"{e(sym)} nuk është aktiv."
            if self.replay_task and not self.replay_task.done():
                return "Një replay po punon ende."
            self.replay_task = asyncio.create_task(self.replay(sym, min(days, 60)))
            return f"⏳ Replay {e(sym)} {days} ditë filloi — raporti vjen kur të mbarojë (disa minuta)."
        if cmd == "/ctrader":
            return await self.set_ctrader(args)
        return ("Komandat: /status /map /stop /resume /flat /trades 7 /replay XAUUSD 30 /ctrader KONFIGURIMI "
                "/health")

    async def set_ctrader(self, raw: str) -> str:
        try:
            url, token = parse_mcp_config(raw)
        except DecodeError:
            return "❌ Konfigurimi nuk u lexua. Kopjoje të plotë nga cTrader Web → Settings → Remote MCP."
        self.store.put("ctrader_config", raw)
        await self.client.set_credentials(url, token)
        self.env = token_environment(token)
        self.auth_alerted = False
        mode, forced = effective_mode(self.s, self.env)
        if mode != self.mode:
            if guards.open_trades(self.store):
                guards.trip(self.store, "llogaria ndryshoi me pozicione të hapura — rinis shërbimin", self.clock())
            else:
                self.mode, self.forced_paper = mode, forced
                self.venue = self._venue(mode)
                self.tm.venue = self.venue
        self.tm.account = self.env
        await self.connect()
        return (f"✅ Tokeni u rinovua ({e(mask(token))}) · Llogaria: {e(self.env)} · Modaliteti: {self.mode.upper()}"
                f"{' (PAPER i detyruar: mungon CONFIRM_LIVE_ACCOUNT=YES)' if self.forced_paper else ''}"
                f" · Simbolet aktive: {', '.join(self.active) or 'asnjë'}")

    async def replay(self, sym: str, days: int) -> None:
        from mapex.replay import fetch_history, run_replay
        try:
            end = int(self.clock()) // 60 * 60
            bars = await fetch_history(self.client, sym, days, end)
            rep = await run_replay(sym, bars, end - days * 86400, end, self.s)
            self.store.outbox_add(f"replay:{sym}:{end}", rep.text())
        except Exception as exc:  # noqa: BLE001
            log.exception("replay")
            self.store.outbox_add(f"replay-err:{int(self.clock())}", f"Replay dështoi: {e(type(exc).__name__)}")

    def status_text(self, short: bool = False) -> str:
        now = self.clock()
        ks = guards.kill_switch(self.store)
        lines = [f"Modaliteti: {self.mode.upper()} (llogari {e(self.env)}) · Kill switch: "
                 f"{'ON — ' + e(ks) if ks else 'OFF'}"]
        for sym in self.s.symbols:
            m, meta = current_map(self.store, sym)
            if sym in self.disabled:
                lines.append(f"{sym}: çaktivizuar ({e(self.disabled[sym][:80])})")
                continue
            if not m:
                lines.append(f"{sym}: pa hartë të vlefshme")
                continue
            states = load_states(self.store, sym)
            zs = []
            for kz in m["key_zones"]:
                st = states.get(meta["zone_keys"].get(kz["id"], ""))
                zs.append(f"{kz['id']} {st.state if st else 'WATCH'}"
                          f"{f' (sweep {st.sweep_count})' if st and st.sweep_count else ''}")
            lines.append(f"{sym}: harta {fmt_ny(meta['created_at'])} NY · bias {m['strategic_bias'].upper()} · "
                         + " · ".join(zs))
        opened = guards.open_trades(self.store)
        pos = ", ".join(f"{t['symbol']} {t['side'].upper()} {t['lots']:.2f} @{t['entry_fill']}, SL {t['sl']}, "
                        f"TP {t['tp_server']}" for t in opened)
        lines.append(f"Pozicione: {len(opened)}{' (' + pos + ')' if pos else ''}")
        st = guards.day_stats(self.store, now)
        lines.append(f"Sot: {st['trades']} tregti · {st['consecutive_losses']} humbje radhazi · {st['sum_r']:+.1f}R")
        ages = []
        for sym in self.active:
            a = self.quotes.age(sym, now)
            q = self.quotes.last.get(sym)
            ages.append(f"{sym} {'—' if a is None else f'{a:.0f}s'}"
                        f"{f' spread {q.spread:.2f}' if q else ''}")
        lines.append("Të dhënat: " + (" · ".join(ages) or "pa lidhje me cTrader"))
        if short:
            return "\n".join([lines[0], lines[-3], lines[-1]])
        return "\n".join(lines)

    def health(self) -> dict:
        """Always 200 while the process lives; never depends on cTrader; never contains a token (P7, P9)."""
        now = self.clock()
        maps = {}
        for sym in self.s.symbols:
            m, meta = current_map(self.store, sym)
            maps[sym] = None if not m else {"map_id": meta.get("map_id"), "bias": m["strategic_bias"],
                                            "created_at": meta.get("created_at"),
                                            "zones": [kz["id"] for kz in m["key_zones"]]}
        zones = {sym: {k: v.state for k, v in load_states(self.store, sym).items()} for sym in self.s.symbols}
        return {
            "status": "ok", "mode": self.mode, "forced_paper": self.forced_paper, "environment": self.env,
            "leader": self.leader, "lease": self.store.lease_info(), "uptime_s": int(now - self.started_at),
            "ctrader": {"configured": self.client.configured, "token": mask(self.client.token),
                        "auth_error": self.client.auth_error_since is not None,
                        "trading_profile": self.client.trading_profile, "active_symbols": self.active,
                        "disabled": self.disabled},
            "quote_age_s": {s: self.quotes.age(s, now) for s in self.active},
            "maps": maps, "zone_states": zones,
            "open_trades": [dict(symbol=t["symbol"], side=t["side"], state=t["state"], lots=t["lots"])
                            for t in guards.open_trades(self.store)],
            "guards": {"kill_switch": guards.kill_switch(self.store), **guards.day_stats(self.store, now)},
            "trading_day": trading_day(now), "last_error": self.last_error,
            "warnings": self.s.warnings, "errors": self.s.errors,
            "volume_mounted": self.s.volume_mounted, "data_dir": str(self.s.data_dir),
        }

    # ------------------------------------------------------------- run
    def asgi(self):
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse
        from starlette.routing import Route

        async def health(_request):
            try:
                return JSONResponse(self.health())
            except Exception as exc:  # noqa: BLE001 — /health must answer while the process lives
                return JSONResponse({"status": "degraded", "error": type(exc).__name__})

        return Starlette(routes=[Route("/health", health), Route("/", health)])

    async def every(self, fn, interval: float) -> None:
        while True:
            try:
                await fn()
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"{fn.__name__}: {type(exc).__name__}"
                log.exception("%s", fn.__name__)
            await asyncio.sleep(interval)

    async def minute_loop(self) -> None:
        while True:
            now = time.time()
            await asyncio.sleep(max(0.5, (int(now) // 60 + 1) * 60 + 3 - now))  # ~3 s after each M1 close
            try:
                await self.minute_tick()
            except Exception:  # noqa: BLE001
                log.exception("minute tick")

    async def quote_loop(self) -> None:
        while True:
            try:
                interval = await self.quote_tick()
            except Exception:  # noqa: BLE001
                log.exception("quotes")
                interval = 10.0
            await asyncio.sleep(interval)

    async def telegram_loop(self) -> None:
        while True:
            try:
                if self.leader and self.bot.enabled:
                    await self.bot.poll_once(self.command, timeout=20)
                else:
                    await asyncio.sleep(5)
            except Exception as exc:  # noqa: BLE001 — network hiccups: back off and retry
                log.warning("telegram poll: %s", type(exc).__name__)
                await asyncio.sleep(5)

    async def outbox_tick(self) -> None:
        if self.leader:
            await self.bot.flush_outbox()

    async def run(self) -> None:
        import uvicorn

        await self.startup()
        server = uvicorn.Server(uvicorn.Config(self.asgi(), host="0.0.0.0", port=self.s.port, log_level="warning"))
        tasks = [asyncio.create_task(c) for c in (
            self.every(self.lease_tick, 15), self.quote_loop(), self.minute_loop(), self.every(self.manage_tick, 5),
            self.every(self.reconcile_tick, 30), self.every(self.heartbeat_tick, 300), self.telegram_loop(),
            self.every(self.outbox_tick, 2))]
        try:
            await server.serve()
        finally:
            for t in tasks:
                t.cancel()
            await self.client.close()


def main() -> None:
    s = config.load()
    setup_logging(s.log_level, [s.ctrader_token, s.telegram_token, s.ctrader_config])
    for w in s.warnings + s.errors:
        log.warning(w)
    app = App(s)
    log.info("MAPEX starting: mode=%s env=%s symbols=%s", app.mode, app.env, s.symbols)
    asyncio.run(app.run())


if __name__ == "__main__":
    main()

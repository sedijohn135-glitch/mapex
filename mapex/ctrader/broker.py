"""Order flow shared by live and paper (only the injected venue differs, broker-execution §3-§7).

Money rules: the setup_key INSERT is the lock; create_order is sent once; a timeout is reconciled by
comment, never resent; SL/TP ride on the market order as relative points, are exactified with an
amend that ALWAYS carries both legs (Q-R10), and are read back; any verification failure closes the
position and trips the kill switch. Only positions labelled MAPEX are ever amended or closed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time

from mapex import guards
from mapex.config import LIVE_LABEL, Settings
from mapex.ctrader.client import AuthError, ToolError, TransportError
from mapex.ctrader.decode import (
    DecodeError,
    check_volume,
    decode_position_price,
    price_field,
    round_volume_step,
    to_points,
)
from mapex.telegram import display_id, msg_entry, msg_exit, msg_no_sl, msg_token_expired, msg_tp1, short_model

log = logging.getLogger("mapex.broker")
RECONCILE_BACKOFF = (1, 2, 5, 10, 20, 30, 30, 30)  # ~2 min
MANUAL_GRACE_S = 60
DEALS_LAG_S = 60
VOLUME_STEP = {"XAUUSD": 100, "BTCUSD": 1}  # cents (0.01 lot); DECISIONS D-31


class LiveVenue:
    """cTrader Remote MCP trading tools, normalised."""

    mode = "live"

    def __init__(self, client, settings: Settings):
        self.client = client
        self.s = settings

    def digits(self, symbol: str) -> int:
        return self.client.digits[symbol]

    def supports_range(self) -> bool:
        props = self.client.tools.get("create_order") or set()
        return not props or {"slippageInPoints", "baseSlippagePrice"} <= props

    def _name(self, sid) -> str | None:
        for name, info in self.client.symbol_map.items():
            if int(info.get("symbolId", -1)) == int(sid):
                return name
        return None

    def _px(self, name: str, v) -> float | None:
        return decode_position_price(v, self.client.digits.get(name, 2), self.s.price_bands.get(name, [0, 1e12]))

    def _pos(self, p: dict) -> dict | None:
        name = self._name(p.get("symbolId", -1))
        if name is None:
            return None
        return {"position_id": str(p.get("positionId")), "symbol": name,
                "side": "buy" if str(p.get("tradeSide", "")).upper() == "BUY" else "sell",
                "volume": int(p.get("volume") or 0), "entry": self._px(name, p.get("entryPrice")),
                "sl": self._px(name, p.get("stopLoss")), "tp": self._px(name, p.get("takeProfit")),
                "label": p.get("label") or "", "comment": p.get("comment") or ""}

    async def create_order(self, symbol: str, args: dict) -> dict:
        await self.client.load_symbols()
        data = await self.client.call("create_order", {"symbolId": self.client.symbol_id(symbol), **args})
        pos = data.get("position") or {}
        return {"order_id": str(data.get("orderId") or (data.get("order") or {}).get("orderId") or ""),
                "position_id": str(data.get("positionId") or pos.get("positionId") or "") or None,
                "fill": self._px(symbol, data.get("executionPrice") or pos.get("entryPrice")),
                "volume": int(data.get("filledVolume") or pos.get("volume") or 0) or None, "raw": data}

    async def amend_position(self, position_id: str, stop_loss: float, take_profit: float) -> dict:
        """P-AMEND-SAFE: both legs on every call — omitting one REMOVES it (Q-R10)."""
        if stop_loss is None or take_profit is None:
            raise ValueError("amend_position requires both stopLoss and takeProfit")
        return await self.client.call("amend_position", {"positionId": int(position_id), "stopLoss": stop_loss,
                                                         "takeProfit": take_profit})

    async def close_position(self, position_id: str, volume: int) -> dict:
        return await self.client.call("close_position", {"positionId": int(position_id), "volume": int(volume)})

    async def positions(self) -> list[dict]:
        await self.client.load_symbols()
        data = await self.client.call("get_positions")
        return [p for p in (self._pos(x) for x in data.get("positions", []) or []) if p]

    async def deals_for(self, position_id: str, since: float) -> list[dict]:
        data = await self.client.call("get_position_details", {"positionId": int(position_id)})
        deals = data.get("deals") or []
        if not deals:
            data = await self.client.call("get_deals", {"fromTimestamp": int(since * 1000) - 60_000,
                                                        "toTimestamp": int(time.time() * 1000) + 60_000})
            deals = [d for d in data.get("deals", []) or [] if str(d.get("positionId")) == str(position_id)]
        out = []
        for d in deals:
            name = self._name(d.get("symbolId", -1)) if d.get("symbolId") is not None else None
            out.append({"price": d.get("executionPrice"), "volume": int(d.get("volume") or d.get("filledVolume") or 0),
                        "closing": bool(d.get("isClosing") or d.get("closePositionDetail")), "symbol": name,
                        "side": str(d.get("tradeSide", "")).lower()})
        return out


class TradeManager:
    def __init__(self, store, settings: Settings, venue, clock=time.time, sleep=asyncio.sleep,
                 account: str = "demo"):
        self.store, self.s, self.venue = store, settings, venue
        self.clock, self.sleep = clock, sleep
        self.account = account
        self.auth_ok = lambda: True
        self.skew = lambda: 0.0
        self.no_sl_alerted: set[str] = set()
        self.lock = asyncio.Lock()  # one broker conversation at a time: execute / manage / reconcile / flat

    # ------------------------------------------------------------- helpers
    def _log(self, key: str, tool: str, req: dict, resp, t0: float, ok: bool) -> None:
        self.store.order_log(key, tool, req, resp, int((time.monotonic() - t0) * 1000), ok, now=self.clock())

    def _update(self, key: str, **fields) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        self.store.execute(f"UPDATE trades SET {cols} WHERE setup_key=?", (*fields.values(), key))

    def trade(self, key: str):
        return self.store.one("SELECT * FROM trades WHERE setup_key=?", (key,))

    def _dec(self, symbol: str) -> int:
        return self.s.display_decimals.get(symbol, 2)

    async def _safe(self, key, tool, fn, *args):
        t0 = time.monotonic()
        try:
            res = await fn(*args)
            self._log(key, tool, {"args": [str(a) for a in args]}, res, t0, True)
            return res
        except Exception as exc:
            self._log(key, tool, {"args": [str(a) for a in args]}, {"error": type(exc).__name__, "msg": str(exc)[:300]},
                      t0, False)
            raise

    # ------------------------------------------------------------- entry
    def build_order(self, plan) -> dict:
        sym = plan.symbol
        band = self.s.price_bands[sym]
        d = self.venue.digits(sym)
        dec = self._dec(sym)
        decision = price_field(plan.decision_price, dec, band)
        sl, tp = price_field(plan.sl, dec, band), price_field(plan.tp_server, dec, band)
        cents = check_volume(self.s.lots.get(sym), self.s.contract_size[sym], self.s.max_lot)
        args = {"orderType": "MARKET_RANGE", "tradeSide": "BUY" if plan.side == "buy" else "SELL", "volume": cents,
                "baseSlippagePrice": decision, "slippageInPoints": int(self.s.max_slippage_points[sym]),
                "relativeStopLoss": to_points(abs(decision - sl), d),
                "relativeTakeProfit": to_points(abs(tp - decision), d),
                "label": LIVE_LABEL, "comment": plan.setup_key}
        if hasattr(self.venue, "supports_range") and not self.venue.supports_range():
            args = {k: v for k, v in args.items() if k not in ("baseSlippagePrice", "slippageInPoints")}
            args["orderType"] = "MARKET"
        return args

    async def _execute(self, plan, quote) -> str:
        now = self.clock()
        why = guards.check_entry(self.s, self.store, plan, quote, now, self.auth_ok(), self.skew())
        if why:
            self.store.event(now, plan.symbol, plan.zone_key, plan.chain, "BLOCKED", "NO-ORDER", reason="; ".join(why))
            return "blocked: " + "; ".join(why)
        try:
            args = self.build_order(plan)
        except (DecodeError, KeyError) as exc:
            self.store.event(now, plan.symbol, plan.zone_key, plan.chain, "BLOCKED", "NO-ORDER", reason=str(exc))
            return f"blocked: {exc}"
        info = {"zone_label": plan.zone_label, "root_type": plan.root_type, "root_price": plan.root_price,
                "root_lps": plan.root_lps, "sweep_count": plan.sweep_count, "sweep_type": plan.sweep_type,
                "session": plan.session, "model": short_model(plan.strategy_used, plan.model), "r_tp1": plan.r_tp1,
                "r_tp2": plan.r_tp2, "order": args}
        try:
            with self.store.tx():  # the claim: UNIQUE(setup_key) is the lock
                self.store.execute(
                    "INSERT INTO trades(setup_key, symbol, side, lots, volume_cents, entry_planned, sl, tp1, tp2, tp3, "
                    "tp_server, risk, state, mode, zone_key, chain, info, opened_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?, 'SENDING', ?,?,?,?,?)",
                    (plan.setup_key, plan.symbol, plan.side, self.s.lots[plan.symbol], args["volume"],
                     plan.decision_price, plan.sl, plan.tp1, plan.tp2, plan.tp3, plan.tp_server, plan.risk,
                     self.venue.mode, plan.zone_key, plan.chain, json.dumps(info), int(now)))
        except sqlite3.IntegrityError:
            return "duplicate"
        key = plan.setup_key
        try:
            resp = await self._send(key, plan.symbol, args)
        except TransportError:
            return await self.reconcile_sending(key)
        except AuthError:
            self._update(key, state="FAILED", closed_at=int(self.clock()))
            self.store.outbox_add(f"auth:{key}", msg_token_expired(), critical=True)
            return "failed: auth"
        except ToolError as exc:
            self._update(key, state="FAILED", closed_at=int(self.clock()))
            self.store.outbox_add(f"reject:{key}", f"🛑 <b>Urdhri u refuzua</b> — {plan.symbol}: "
                                                   f"{str(exc)[:200]}", critical=True)
            return f"failed: rejected ({exc})"
        self._update(key, state="OPEN", order_id=resp.get("order_id"), position_id=resp.get("position_id"),
                     entry_fill=resp.get("fill") or plan.decision_price, volume_open=resp.get("volume"))
        if not resp.get("position_id"):
            return await self.reconcile_sending(key)
        return await self.protect(key)

    async def _send(self, key: str, symbol: str, args: dict) -> dict:
        try:
            return await self._safe(key, "create_order", self.venue.create_order, symbol, args)
        except ToolError as exc:
            txt = str(exc)
            if args.get("orderType") == "MARKET_RANGE" and any(k in txt for k in ("MARKET_RANGE", "orderType",
                                                                                   "slippage")):
                # the server rejected the order type before any fill: MARKET fallback (spec §3.3)
                fallback = {k: v for k, v in args.items() if k not in ("baseSlippagePrice", "slippageInPoints")}
                fallback["orderType"] = "MARKET"
                return await self._safe(key, "create_order", self.venue.create_order, symbol, fallback)
            raise

    async def find_by_comment(self, key: str) -> dict | None:
        for p in await self.venue.positions():
            if p["label"] == LIVE_LABEL and p["comment"] == key:
                return p
        return None

    async def reconcile_sending(self, key: str) -> str:
        """Timeout/unknown outcome: NEVER resend. Poll positions by comment; adopt or fail + kill switch."""
        for delay in RECONCILE_BACKOFF:
            await self.sleep(delay)
            try:
                p = await self.find_by_comment(key)
            except (TransportError, ToolError, AuthError):
                continue
            if p:
                self._update(key, state="OPEN", position_id=p["position_id"], entry_fill=p["entry"],
                             volume_open=p["volume"])
                return await self.protect(key)
        self._update(key, state="FAILED", closed_at=int(self.clock()))
        guards.trip(self.store, "verifikimi i urdhrit dështoi", self.clock())
        return "failed: unknown outcome"

    async def _read(self, pid: str) -> dict | None:
        for p in await self.venue.positions():
            if p["position_id"] == str(pid):
                return p
        return None

    def _ok(self, p: dict, t, tick: float) -> bool:
        return (p is not None and p["sl"] is not None and p["tp"] is not None
                and abs(p["sl"] - t["sl"]) <= tick + 1e-9 and abs(p["tp"] - t["tp_server"]) <= tick + 1e-9
                and p["side"] == t["side"] and abs(p["volume"] - t["volume_cents"]) <= 1)

    async def protect(self, key: str) -> str:
        """First-fill volume proof, exact SL/TP (both legs), read-back verification (spec §3.6-3.7, §5)."""
        t = self.trade(key)
        pid, sym = t["position_id"], t["symbol"]
        tick = self.s.tick(sym)
        p = await self._read(pid)
        for delay in RECONCILE_BACKOFF[:3]:
            if p is not None:
                break
            await self.sleep(delay)
            p = await self._read(pid)
        if p is None:
            return "open (position not visible yet)"
        step = VOLUME_STEP.get(sym, 1)
        if abs(p["volume"] - t["volume_cents"]) > step:
            await self._safe(key, "close_position", self.venue.close_position, pid, p["volume"])
            self._update(key, state="CLOSING", closed_at=int(self.clock()))
            guards.trip(self.store, f"volumi nuk përputhet (pritej {t['volume_cents']}, erdhi {p['volume']})",
                        self.clock())
            return "closed: volume mismatch"
        for _attempt in range(2):
            try:
                await self._safe(key, "amend_position", self.venue.amend_position, pid, t["sl"], t["tp_server"])
            except (ToolError, TransportError):
                pass
            self._update(key, last_amend_at=int(self.clock()))
            p = await self._read(pid)
            if self._ok(p, t, tick):
                self._update(key, entry_fill=p["entry"] or t["entry_fill"],
                             risk=abs((p["entry"] or t["entry_fill"]) - t["sl"]))
                self.notify_entry(key)
                return "open"
            if p is None:
                return "open (closed during protection)"
        await self._safe(key, "close_position", self.venue.close_position, pid, p["volume"])
        self._update(key, state="CLOSING", closed_at=int(self.clock()))
        guards.trip(self.store, "verifikimi i urdhrit dështoi", self.clock())
        return "closed: verification failed"

    def notify_entry(self, key: str) -> None:
        t = self.trade(key)
        info = json.loads(t["info"] or "{}")
        n = self.store.one("SELECT COUNT(*) AS n FROM trades WHERE state != 'FAILED' AND opened_at <= ? AND "
                           "opened_at >= ?", (t["opened_at"], t["opened_at"] - 86400))["n"]
        text = msg_entry({
            "side": t["side"], "symbol": t["symbol"], "entry": t["entry_fill"], "lots": t["lots"], "sl": t["sl"],
            "tp1": t["tp1"], "tp1_r": info.get("r_tp1") or 0, "tp2": t["tp2"], "tp2_r": info.get("r_tp2") or 0,
            "zone_label": info.get("zone_label", ""), "root_type": info.get("root_type", ""),
            "root_price": info.get("root_price"), "root_lps": info.get("root_lps"),
            "sweep_count": info.get("sweep_count"), "sweep_type": info.get("sweep_type"), "scores": (25, 25, 25, 25),
            "session": info.get("session", ""), "model": info.get("model", ""), "account": self.account,
            "paper": self.venue.mode == "paper", "id": display_id(t["symbol"], t["opened_at"], n)},
            self._dec(t["symbol"]))
        self.store.outbox_add(f"entry:{key}", text)

    # ------------------------------------------------------------- management
    async def _manage(self, quotes: dict) -> None:
        trades = [t for t in guards.open_trades(self.store) if t["mode"] == self.venue.mode]
        if not trades:
            return
        positions = {p["position_id"]: p for p in await self.venue.positions() if p["label"] == LIVE_LABEL}
        for t in trades:
            if t["state"] == "SENDING":
                continue
            p = positions.get(str(t["position_id"]))
            if p is None:
                await self.handle_closed(t)
                continue
            await self._manage_one(t, p, quotes.get(t["symbol"]))

    async def _manage_one(self, t, p: dict, q) -> None:
        key, pid, now = t["setup_key"], t["position_id"], self.clock()
        tick = self.s.tick(t["symbol"])
        if p["sl"] is None and pid not in self.no_sl_alerted:
            self.no_sl_alerted.add(pid)
            self.store.outbox_add(f"nosl:{pid}", msg_no_sl(t["symbol"], pid), critical=True)
        if t["manual"]:
            return  # the owner took over: never fight manual changes
        mine_sl = t["entry_fill"] if t["be_done"] else t["sl"]
        changed = (p["sl"] is None or abs(p["sl"] - mine_sl) > tick + 1e-9
                   or p["tp"] is None or abs(p["tp"] - t["tp_server"]) > tick + 1e-9)
        if changed and now - (t["last_amend_at"] or 0) > MANUAL_GRACE_S:
            self._update(key, manual=1)
            self.store.event(now, t["symbol"], t["zone_key"], t["chain"], "MANUAL", "ADOPTED",
                             reason=f"owner SL/TP adopted: {p['sl']} / {p['tp']}")
            return
        if t["partial_done"] or q is None:
            return
        buy = t["side"] == "buy"
        if (buy and q.bid >= t["tp1"]) or (not buy and q.ask <= t["tp1"]):
            step = VOLUME_STEP.get(t["symbol"], 1)
            vol = round_volume_step(int(p["volume"] * self.s.tp1_close_pct / 100), step)
            if 0 < vol < p["volume"]:
                await self._safe(key, "close_position", self.venue.close_position, pid, vol)
            # breakeven ONLY after TP1, both legs sent
            await self._safe(key, "amend_position", self.venue.amend_position, pid, t["entry_fill"],
                             p["tp"] if p["tp"] is not None else t["tp_server"])
            self._update(key, partial_done=1, be_done=1, state="BE", last_amend_at=int(self.clock()))
            if self.s.notify_exits:
                self.store.outbox_add(f"tp1:{key}", msg_tp1(key, self.s.tp1_close_pct))

    async def handle_closed(self, t) -> None:
        key, now = t["setup_key"], self.clock()
        if t["state"] != "CLOSING":
            self._update(key, state="CLOSING", closed_at=int(now))
            t = self.trade(key)
        deals = []
        try:
            deals = [d for d in await self.venue.deals_for(t["position_id"], t["opened_at"] or now) if d["closing"]]
        except (ToolError, TransportError, AuthError):
            pass
        result = None
        if deals and t["risk"]:
            sgn = 1 if t["side"] == "buy" else -1
            entry = t["entry_fill"]
            sym = t["symbol"]
            total = sum(d["volume"] for d in deals) or 1
            px = [decode_position_price(d["price"], self.venue.digits(sym), self.s.price_bands[sym]) for d in deals]
            if all(x is not None for x in px):
                moved = sum(d["volume"] * sgn * (x - entry) for d, x in zip(deals, px, strict=True))
                result = moved / total / t["risk"]
        elif now - (t["closed_at"] or now) < DEALS_LAG_S:
            return  # get_deals propagation lag (Q-R11): retry next loop
        self._update(key, state="CLOSED", result_r=result, closed_at=int(now))
        guards.record_result(self.s, self.store, result, now)
        if self.s.notify_exits:
            kind = "SL" if result is not None and result < 0 else "TP"
            self.store.outbox_add(f"exit:{key}", msg_exit(kind, result))

    # ------------------------------------------------------------- reconciliation / flat
    async def _reconcile(self) -> dict:
        """Start-up and every 30 s: DB trades <-> broker positions (label MAPEX only)."""
        out = {"adopted": 0, "orphans": 0, "closed": 0}
        positions = [p for p in await self.venue.positions() if p["label"] == LIVE_LABEL]
        by_pid = {p["position_id"]: p for p in positions}
        by_comment = {p["comment"]: p for p in positions}
        known = set()
        for t in guards.open_trades(self.store):
            if t["mode"] != self.venue.mode:
                continue
            p = by_pid.get(str(t["position_id"])) or by_comment.get(t["setup_key"])
            if p:
                known.add(p["position_id"])
                if t["state"] == "SENDING" or str(t["position_id"]) != p["position_id"]:
                    self._update(t["setup_key"], state="OPEN", position_id=p["position_id"], entry_fill=p["entry"],
                                 volume_open=p["volume"])
                    out["adopted"] += 1
                    await self.protect(t["setup_key"])
            elif t["state"] == "SENDING":
                if self.clock() - (t["opened_at"] or 0) > 150:
                    self._update(t["setup_key"], state="FAILED", closed_at=int(self.clock()))
            else:
                await self.handle_closed(t)
                out["closed"] += 1
        for p in positions:
            if p["position_id"] in known:
                continue
            row = self.store.one("SELECT 1 FROM trades WHERE position_id=?", (p["position_id"],))
            if row:
                continue
            failed = self.store.one("SELECT * FROM trades WHERE setup_key=? AND state='FAILED'", (p["comment"],))
            if failed:  # the fill surfaced after the reconcile window: adopt it, never a second order
                self._update(failed["setup_key"], state="OPEN", position_id=p["position_id"], entry_fill=p["entry"],
                             volume_open=p["volume"])
                out["adopted"] += 1
                await self.protect(failed["setup_key"])
                continue
            key = p["comment"] or f"ORPHAN-{p['position_id']}"
            if self.store.one("SELECT 1 FROM trades WHERE setup_key=?", (key,)):
                key = f"ORPHAN-{p['position_id']}"
            self.store.execute(
                "INSERT INTO trades(setup_key, symbol, side, lots, volume_cents, entry_fill, sl, tp_server, state, "
                "mode, position_id, manual, opened_at, info) VALUES(?,?,?,?,?,?,?,?, 'OPEN', ?,?, 1, ?, '{}')",
                (key, p["symbol"], p["side"], p["volume"] / 100 / self.s.contract_size.get(p["symbol"], 1),
                 p["volume"], p["entry"], p["sl"], p["tp"], self.venue.mode, p["position_id"], int(self.clock())))
            out["orphans"] += 1
            if p["sl"] is None:
                self.no_sl_alerted.add(p["position_id"])
                self.store.outbox_add(f"nosl:{p['position_id']}", msg_no_sl(p["symbol"], p["position_id"]),
                                      critical=True)
        return out

    async def _flat(self) -> int:
        n = 0
        for p in await self.venue.positions():
            if p["label"] != LIVE_LABEL:
                continue  # never touch the owner's own positions
            await self._safe(f"flat:{p['position_id']}", "close_position", self.venue.close_position,
                             p["position_id"], p["volume"])
            n += 1
        return n

    async def execute(self, plan, quote) -> str:
        async with self.lock:
            return await self._execute(plan, quote)

    async def manage(self, quotes: dict) -> None:
        async with self.lock:
            await self._manage(quotes)

    async def reconcile(self) -> dict:
        async with self.lock:
            return await self._reconcile()

    async def flat(self) -> int:
        async with self.lock:
            return await self._flat()

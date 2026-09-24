"""cTrader Open API client: JSON over WebSocket (port 5036) behind the CTraderClient interface (D-69).

One authenticated connection (application auth -> account auth), kept alive with heartbeats; every request is matched
to its answer by clientMsgId; spot prices stream in through a subscription. The MCP-style tool calls of the rest of
MAPEX (get_trendbars, create_order, amend_position, ...) are translated here, so the broker, guards and executor
stay unchanged.

Units (spotware/openapi-proto-messages): spot and trendbar prices are integers in 1/100000; order and position prices
are decimals; relativeStopLoss/TakeProfit are 1/100000 of the price; volume is cents; slippageInPoints is in symbol
points. Enums are sent as numbers and read as numbers or names; int64 fields are read from numbers or strings.
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import logging
import time
from collections.abc import Callable

from mapex.ctrader.client import (
    HISTORICAL_TOOLS,
    PERIODS,
    AuthError,
    CTraderClient,
    ToolError,
    TransportError,
)

log = logging.getLogger("mapex.ctrader.openapi")

HOSTS = {"demo": "demo.ctraderapi.com", "live": "live.ctraderapi.com"}
PORT = 5036
HEARTBEAT_S = 10.0

HEARTBEAT, COMMON_ERROR = 51, 50
APP_AUTH_REQ, ACCOUNT_AUTH_REQ, VERSION_REQ, NEW_ORDER_REQ = 2100, 2102, 2104, 2106
AMEND_SLTP_REQ, CLOSE_POSITION_REQ, SYMBOLS_LIST_REQ, SYMBOL_BY_ID_REQ = 2110, 2111, 2114, 2116
RECONCILE_REQ, EXECUTION_EVENT, SUBSCRIBE_SPOTS_REQ, SPOT_EVENT = 2124, 2126, 2127, 2131
ORDER_ERROR_EVENT, DEAL_LIST_REQ, GET_TRENDBARS_REQ, OA_ERROR_RES = 2132, 2133, 2137, 2142
TOKEN_INVALIDATED_EVENT, CLIENT_DISCONNECT_EVENT, ACCOUNTS_BY_TOKEN_REQ = 2147, 2148, 2149
ACCOUNT_DISCONNECT_EVENT, REFRESH_TOKEN_REQ, DEALS_BY_POSITION_REQ = 2164, 2173, 2179

ORDER_TYPE = {"MARKET": 1, "MARKET_RANGE": 5}
SIDE = {"BUY": 1, "SELL": 2}
SIDE_NAME = {1: "BUY", 2: "SELL", "BUY": "BUY", "SELL": "SELL"}
PERIOD = {"M_1": 1, "M_5": 5, "M_15": 7, "M_30": 8, "H_1": 9, "H_4": 10, "D_1": 12, "W_1": 13, "MN_1": 14}
# largest toTimestamp - fromTimestamp per trendbar period (Open API limits), in seconds, minus a minute of margin
WINDOW_S = {"M_1": 302_400, "M_5": 302_400, "M_15": 21_168_000, "M_30": 21_168_000, "H_1": 21_168_000,
            "H_4": 31_622_400, "D_1": 31_622_400, "W_1": 158_112_000, "MN_1": 158_112_000}
EXEC_TYPE = {2: "ORDER_ACCEPTED", 3: "ORDER_FILLED", 4: "ORDER_REPLACED", 5: "ORDER_CANCELLED", 6: "ORDER_EXPIRED",
             7: "ORDER_REJECTED", 8: "ORDER_CANCEL_REJECTED", 11: "ORDER_PARTIAL_FILL"}
FAILED_EXEC = {"ORDER_REJECTED", "ORDER_CANCELLED", "ORDER_EXPIRED", "ORDER_CANCEL_REJECTED"}
TOKEN_CODES = {"OA_AUTH_TOKEN_EXPIRED", "CH_ACCESS_TOKEN_INVALID"}
AUTH_CODES = TOKEN_CODES | {"ACCOUNT_NOT_AUTHORIZED", "CH_CLIENT_AUTH_FAILURE", "CH_CLIENT_NOT_AUTHENTICATED",
                            "CH_CTID_TRADER_ACCOUNT_NOT_FOUND", "CH_OA_CLIENT_NOT_FOUND", "RET_NO_SUCH_LOGIN",
                            "RET_ACCOUNT_DISABLED"}
DEAL_WEEK_MS = 604_800_000  # ProtoOADealListReq: toTimestamp - fromTimestamp <= 1 week

TOOLS = {  # the MCP-style tools this client answers, with the fields each accepts (trading_profile, supports_range)
    "get_version": set(), "get_symbols": set(), "get_symbol_details": {"symbolId"}, "get_spot_prices": {"symbolId"},
    "get_trendbars": {"symbolId", "period", "fromTimestamp", "toTimestamp"},
    "create_order": {"symbolId", "orderType", "tradeSide", "volume", "baseSlippagePrice", "slippageInPoints",
                     "relativeStopLoss", "relativeTakeProfit", "label", "comment"},
    "amend_position": {"positionId", "stopLoss", "takeProfit"}, "close_position": {"positionId", "volume"},
    "get_positions": set(), "get_position_details": {"positionId"}, "get_deals": {"fromTimestamp", "toTimestamp"},
}


class TokenError(AuthError):
    """The access token was refused: refresh it, re-authorise, then the request may be sent again (it ran nothing)."""


def _int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _exec_type(v) -> str:
    return EXEC_TYPE.get(v, str(v)) if isinstance(v, int) else str(v)


async def ws_connect(url: str):
    from websockets.asyncio.client import connect

    return await connect(url, max_size=2**24, open_timeout=20, ping_interval=20, ping_timeout=20)


class _Wait:
    """A request waiting for its answer. `accept(payloadType, payload)` returns None to keep waiting."""

    def __init__(self, fut: asyncio.Future, accept: Callable, comment: str | None = None,
                 position_id: int | None = None):
        self.fut, self.accept, self.comment, self.position_id = fut, accept, comment, position_id

    def feed(self, pt: int, p: dict) -> None:
        if self.fut.done():
            return
        try:
            out = self.accept(pt, p)
        except Exception as exc:  # noqa: BLE001 — handed to the caller
            self.fut.set_exception(exc)
            return
        if out is not None:
            self.fut.set_result(out)


class OpenApiClient(CTraderClient):
    def __init__(self, client_id: str, client_secret: str, access_token: str, refresh_token: str, account_id: str,
                 store=None, connect: Callable = ws_connect, clock: Callable[[], float] = time.time,
                 call_timeout: float = 20.0, env_hint: str = "demo"):
        super().__init__("", access_token, connector=None, clock=clock, call_timeout=call_timeout)
        self.client_id, self.client_secret = client_id, client_secret
        self.refresh_token = refresh_token
        self.account_ref = str(account_id or "").strip()
        self.account_id: int | None = None  # the resolved ctidTraderAccountId
        self.store = store
        self.ws_connect = connect
        self.env = "unknown"
        self.host_env = env_hint if env_hint in HOSTS else "demo"
        self.url = f"wss://{HOSTS[self.host_env]}:{PORT}"
        self.tools = {k: set(v) for k, v in TOOLS.items()}
        self.ws = None
        self._tasks: list[asyncio.Task] = []
        self._conn_lock: asyncio.Lock | None = None
        self._lock_loop = None
        self._waits: dict[str, _Wait] = {}
        self._ids = itertools.count(1)
        self._spots: dict[int, dict] = {}
        self._subscribed: set[int] = set()
        self._spot_skew_ms = 0.0
        self._retry_at = 0.0
        self._backoff = 1.0
        self._load_saved_tokens()

    # ------------------------------------------------------------- credentials
    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self.account_ref and (self.token or self.refresh_token))

    def _seed(self) -> str:
        return hashlib.sha256(f"{self.token}|{self.refresh_token}".encode()).hexdigest()[:16]

    def _load_saved_tokens(self) -> None:
        """Tokens refreshed earlier are kept in the volume (kv); new tokens put in Railway win over them."""
        if self.store is None:
            return
        seed = self._seed()
        if self.store.get("oa_seed") != seed:
            self.store.put("oa_seed", seed)
            for k in ("oa_access_token", "oa_refresh_token", "oa_expires_at"):
                self.store.put(k, None)
            return
        self.token = self.store.get("oa_access_token") or self.token
        self.refresh_token = self.store.get("oa_refresh_token") or self.refresh_token

    async def set_credentials(self, url: str, token: str) -> None:
        raise ToolError("Open API: kredencialet ndryshohen te Railway → Variables (CTRADER_…)")

    # ------------------------------------------------------------- connection
    async def close(self) -> None:
        ws, self.ws = self.ws, None
        tasks, self._tasks = self._tasks, []
        loop = asyncio.get_running_loop()
        for t in tasks:
            if not t.done() and t.get_loop() is loop:
                t.cancel()
        if ws is not None:
            try:
                await ws.close()
            except Exception as exc:  # noqa: BLE001
                log.debug("close: %s", type(exc).__name__)
        self._fail_waits(TransportError("Open API connection closed"))

    def _fail_waits(self, err: Exception) -> None:
        for w in list(self._waits.values()):
            if not w.fut.done():
                w.fut.set_exception(err)
        self._waits.clear()

    async def _ensure(self) -> None:
        loop = asyncio.get_running_loop()
        if self.ws is not None and self._tasks and self._tasks[0].get_loop() is loop and not self._tasks[0].done():
            return
        if self._conn_lock is None or self._lock_loop is not loop:  # a lock per event loop
            self._conn_lock, self._lock_loop = asyncio.Lock(), loop
        async with self._conn_lock:
            if self.ws is not None and self._tasks and self._tasks[0].get_loop() is loop and not self._tasks[0].done():
                return
            await self.close()
            if self.clock() < self._retry_at:
                raise TransportError(f"Open API reconnecting in {int(self._retry_at - self.clock()) + 1}s")
            try:
                await self._connect()
                self._backoff = 1.0
            except BaseException as exc:
                if isinstance(exc, asyncio.CancelledError):
                    raise
                await self.close()
                self._retry_at = self.clock() + self._backoff
                self._backoff = min(self._backoff * 2, 60.0)
                log.warning("Open API connect failed (next try in %ds): %s: %s", int(self._retry_at - self.clock()),
                            type(exc).__name__, str(exc)[:200])
                if isinstance(exc, AuthError | ToolError | TransportError):
                    raise
                raise TransportError(f"Open API connect: {type(exc).__name__}: {str(exc)[:200]}") from None

    async def _open(self, env: str) -> None:
        self.url = f"wss://{HOSTS[env]}:{PORT}"
        self.ws = await asyncio.wait_for(self.ws_connect(self.url), self.call_timeout)
        self.stats["sessions"] += 1
        self._tasks = [asyncio.create_task(self._read_loop(self.ws), name="openapi-read"),
                       asyncio.create_task(self._heartbeat(self.ws), name="openapi-heartbeat")]
        await self._rpc(APP_AUTH_REQ, {"clientId": self.client_id, "clientSecret": self.client_secret})

    async def _connect(self) -> None:
        """Application auth -> the account behind CTRADER_ACCOUNT_ID (ctid or login) on its own host -> account auth."""
        await self._open(self.host_env)
        account, scope = await self._with_token(self._find_account)
        env = "live" if account.get("isLive") in (True, "true", 1) else "demo"
        if env != self.host_env:  # a live account must be authorised on the live host (and demo on demo)
            await self.close()
            self.host_env = env
            await self._open(env)
        self.account_id, self.env = _int(account.get("ctidTraderAccountId")), env
        await self._with_token(lambda: self._rpc(ACCOUNT_AUTH_REQ, {"ctidTraderAccountId": self.account_id,
                                                                     "accessToken": self.token}))
        if scope in (0, "SCOPE_VIEW"):
            self.tools.pop("create_order", None)
            log.warning("Open API token has view-only scope: MAPEX cannot place orders")
        log.info("Open API connected: %s account %s (%s)", env, account.get("traderLogin"), self.account_id)
        if self._subscribed:
            await self._subscribe(sorted(self._subscribed))

    async def _find_account(self) -> tuple[dict, object]:
        _, p = await self._rpc(ACCOUNTS_BY_TOKEN_REQ, {"accessToken": self.token})
        accounts = p.get("ctidTraderAccount") or []
        for a in accounts:
            if self.account_ref in (str(_int(a.get("ctidTraderAccountId"))), str(_int(a.get("traderLogin")))):
                return a, p.get("permissionScope")
        logins = ", ".join(str(a.get("traderLogin")) for a in accounts) or "asnjë"
        raise AuthError(f"CTRADER_ACCOUNT_ID {self.account_ref} nuk është te ky token (llogaritë: {logins})")

    async def _with_token(self, fn):
        """Run fn; if the access token is refused, refresh it once and run fn again."""
        try:
            return await fn()
        except TokenError:
            await self._refresh()
            return await fn()

    async def _refresh(self) -> None:
        if not self.refresh_token:
            raise AuthError("access token refused and CTRADER_REFRESH_TOKEN is missing")
        try:
            _, p = await self._rpc(REFRESH_TOKEN_REQ, {"refreshToken": self.refresh_token})
        except AuthError as exc:
            raise AuthError(f"refresh token refused: {str(exc)[:120]}") from None
        self.token = str(p.get("accessToken") or "")
        self.refresh_token = str(p.get("refreshToken") or self.refresh_token)
        expires = _int(p.get("expiresIn"))
        if self.store is not None:
            self.store.put("oa_access_token", self.token)
            self.store.put("oa_refresh_token", self.refresh_token)
            self.store.put("oa_expires_at", str(int(self.clock()) + expires) if expires else None)
        log.info("Open API access token refreshed (valid %d days)", expires // 86400)

    async def _heartbeat(self, ws) -> None:
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_S)
                await ws.send(json.dumps({"payloadType": HEARTBEAT, "payload": {}}))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — the reader notices the dead socket
            log.debug("heartbeat: %s", type(exc).__name__)

    async def _read_loop(self, ws) -> None:
        reason = "Open API connection closed"
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                pt, p, cid = _int(msg.get("payloadType")), msg.get("payload") or {}, msg.get("clientMsgId")
                if pt == SPOT_EVENT:
                    self._on_spot(p)
                elif pt == HEARTBEAT:
                    continue
                elif pt in (TOKEN_INVALIDATED_EVENT, CLIENT_DISCONNECT_EVENT, ACCOUNT_DISCONNECT_EVENT):
                    reason = f"Open API: {p.get('reason') or pt}"
                    log.warning("%s — reconnecting", reason)
                    break
                elif pt in (EXECUTION_EVENT, ORDER_ERROR_EVENT) and cid not in self._waits:
                    self._route_execution(pt, p)
                elif cid in self._waits:
                    self._waits[cid].feed(pt, p)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — a dropped socket ends the loop; pending requests fail below
            reason = f"Open API connection lost: {type(exc).__name__}"
        finally:
            if self.ws is ws:
                self.ws = None
                self._fail_waits(TransportError(reason))
                for t in self._tasks:
                    if t is not asyncio.current_task():
                        t.cancel()
            try:
                await ws.close()
            except Exception:  # noqa: BLE001, S110
                pass

    def _route_execution(self, pt: int, p: dict) -> None:
        """Execution events that carry no clientMsgId are matched by order comment or position id."""
        order, position = p.get("order") or {}, p.get("position") or {}
        comment = (order.get("tradeData") or {}).get("comment") or (position.get("tradeData") or {}).get("comment")
        pid = _int(position.get("positionId") or order.get("positionId") or p.get("positionId"), -1)
        for w in list(self._waits.values()):
            if (w.comment and comment == w.comment) or (w.position_id is not None and pid == w.position_id):
                w.feed(pt, p)

    def _on_spot(self, p: dict) -> None:
        sid = _int(p.get("symbolId"), -1)
        q = self._spots.setdefault(sid, {})
        for k in ("bid", "ask"):
            if p.get(k) not in (None, "", 0, "0"):
                q[k] = _int(p[k])
        if p.get("timestamp"):
            self._spot_skew_ms = _int(p["timestamp"]) - self.clock() * 1000

    def _error(self, p: dict) -> Exception:
        code = str(p.get("errorCode") or "ERROR")
        desc = str(p.get("description") or "")[:200]
        text = f"{code}: {desc}" if desc else code
        if code in TOKEN_CODES:
            return TokenError(text)
        if code in AUTH_CODES:
            return AuthError(text)
        if code == "REQUEST_FREQUENCY_EXCEEDED":
            return ToolError(f"rate limit ({text})")
        return ToolError(text)

    async def _send(self, pt: int, payload: dict, accept: Callable, comment: str | None = None,
                    position_id: int | None = None):
        ws = self.ws
        if ws is None:
            raise TransportError("Open API not connected")
        cid = f"mpx{next(self._ids)}"
        fut = asyncio.get_running_loop().create_future()
        self._waits[cid] = _Wait(fut, accept, comment, position_id)
        try:
            await ws.send(json.dumps({"clientMsgId": cid, "payloadType": pt, "payload": payload}))
            return await asyncio.wait_for(fut, self.call_timeout)
        except TimeoutError:
            if self.ws is ws:  # an unanswered request: treat the socket as dead, the next call reconnects
                log.warning("Open API request %s timed out; reconnecting", pt)
                await self.close()
            raise TransportError("timeout") from None
        except (AuthError, ToolError, TransportError):
            raise
        except Exception as exc:  # noqa: BLE001 — a send on a dead socket
            raise TransportError(f"Open API send: {type(exc).__name__}") from None
        finally:
            self._waits.pop(cid, None)

    async def _rpc(self, pt: int, payload: dict) -> tuple[int, dict]:
        def accept(rpt, rp):
            if rpt in (OA_ERROR_RES, COMMON_ERROR):
                raise self._error(rp)
            return rpt, rp

        return await self._send(pt, payload, accept)

    async def _order(self, pt: int, payload: dict, done: set[str], comment: str | None = None,
                     position_id: int | None = None) -> dict:
        """Send an order-type request once and wait for its execution event (never resent on a timeout)."""
        def accept(rpt, rp):
            if rpt in (OA_ERROR_RES, COMMON_ERROR, ORDER_ERROR_EVENT):
                raise self._error(rp)
            if rpt != EXECUTION_EVENT:
                return None
            kind = _exec_type(rp.get("executionType"))
            if kind in FAILED_EXEC:
                raise ToolError(f"{kind}: {rp.get('errorCode') or ''}".strip())
            return rp if kind in done else None

        return await self._send(pt, payload, accept, comment, position_id)

    # ------------------------------------------------------------- MCP-style tools
    async def _call_once(self, tool: str, args: dict) -> dict:
        if not self.configured:
            raise AuthError("cTrader Open API configuration missing")
        handler = getattr(self, f"_t_{tool}", None)
        if handler is None or tool not in self.tools:
            raise ToolError(f"tool {tool} not offered by the Open API client")
        await (self.historical if tool in HISTORICAL_TOOLS else self.general).acquire()
        await self._ensure()
        self.stats["requests"] += 1
        try:
            data = await handler(args)
        except TokenError:  # expired mid-session: refresh, re-authorise; the refused request ran nothing
            await self._refresh()
            await self._rpc(ACCOUNT_AUTH_REQ, {"ctidTraderAccountId": self.account_id, "accessToken": self.token})
            data = await handler(args)
        self.auth_error_since = None
        return data

    def _acct(self, **kw) -> dict:
        return {"ctidTraderAccountId": self.account_id, **kw}

    def _name_of(self, sid) -> str | None:
        for name, info in self.symbol_map.items():
            if _int(info.get("symbolId"), -1) == _int(sid, -2):
                return name
        return None

    async def _t_get_version(self, a: dict) -> dict:
        _, p = await self._rpc(VERSION_REQ, {})
        return {"version": p.get("version")}

    async def _t_get_symbols(self, a: dict) -> dict:
        _, p = await self._rpc(SYMBOLS_LIST_REQ, self._acct())
        return {"symbols": [{"symbolId": _int(s.get("symbolId")), "symbolName": s.get("symbolName") or "",
                             "enabled": s.get("enabled", True)} for s in p.get("symbol") or []]}

    async def _t_get_symbol_details(self, a: dict) -> dict:
        _, p = await self._rpc(SYMBOL_BY_ID_REQ, self._acct(symbolId=[_int(x) for x in a.get("symbolId", [])]))
        return {"symbols": [{k: s.get(k) for k in ("symbolId", "digits", "pipPosition", "lotSize", "stepVolume",
                                                    "minVolume", "maxVolume")} for s in p.get("symbol") or []]}

    async def _subscribe(self, ids: list[int]) -> None:
        try:
            await self._rpc(SUBSCRIBE_SPOTS_REQ, self._acct(symbolId=ids, subscribeToSpotTimestamp=True))
        except ToolError as exc:
            if "ALREADY_SUBSCRIBED" not in str(exc):
                raise

    async def _t_get_spot_prices(self, a: dict) -> dict:
        ids = [_int(x) for x in a.get("symbolId", [])]
        new = [i for i in ids if i not in self._subscribed]
        if new:
            await self._subscribe(new)
            self._subscribed.update(new)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + min(5.0, self.call_timeout)
        while not all({"bid", "ask"} <= self._spots.get(i, {}).keys() for i in ids) and loop.time() < deadline:
            await asyncio.sleep(0.05)
        now_ms = self.clock() * 1000 + self._spot_skew_ms
        return {"prices": [{"symbolId": i, "bid": q["bid"], "ask": q["ask"], "timestamp": now_ms}
                           for i in ids if {"bid", "ask"} <= (q := self._spots.get(i, {})).keys()]}

    async def _t_get_trendbars(self, a: dict) -> dict:
        _, p = await self._rpc(GET_TRENDBARS_REQ, self._acct(
            symbolId=_int(a["symbolId"]), period=PERIOD[a["period"]], fromTimestamp=_int(a["fromTimestamp"]),
            toTimestamp=_int(a["toTimestamp"])))
        keys = ("low", "deltaOpen", "deltaHigh", "deltaClose", "utcTimestampInMinutes")
        rows = [{k: _int(b.get(k)) for k in keys} for b in p.get("trendbar") or []]
        return {"trendbars": rows, "hasMore": bool(p.get("hasMore"))}

    def window_s(self, tf: str) -> int:
        return WINDOW_S[PERIODS[tf]] - 60

    async def _t_create_order(self, a: dict) -> dict:
        """Relative SL/TP and slippage arrive at the broker's points scale (the calibrated spot digits, 5 here);
        Open API wants 1/100000 of price for SL/TP and symbol points for slippage."""
        name = self._name_of(a["symbolId"])
        d = self.digits.get(name or "", 5)
        req = self._acct(symbolId=_int(a["symbolId"]), orderType=ORDER_TYPE[a["orderType"]],
                         tradeSide=SIDE[a["tradeSide"]], volume=_int(a["volume"]))
        for k in ("relativeStopLoss", "relativeTakeProfit"):
            if a.get(k) is not None:
                req[k] = int(round(float(a[k]) * 10 ** (5 - d)))
        if a["orderType"] == "MARKET_RANGE":
            sym_digits = _int((self.symbol_map.get(name or "") or {}).get("digits"), d)
            req["baseSlippagePrice"] = float(a["baseSlippagePrice"])
            req["slippageInPoints"] = max(1, int(round(float(a["slippageInPoints"]) * 10 ** (sym_digits - d))))
        for k in ("label", "comment"):
            if a.get(k):
                req[k] = str(a[k])
        ev = await self._order(NEW_ORDER_REQ, req, {"ORDER_FILLED", "ORDER_PARTIAL_FILL"}, comment=req.get("comment"))
        order, pos, deal = ev.get("order") or {}, ev.get("position") or {}, ev.get("deal") or {}
        return {"orderId": _int(order.get("orderId")) or None,
                "positionId": _int(pos.get("positionId") or deal.get("positionId") or order.get("positionId")) or None,
                "executionPrice": deal.get("executionPrice") or pos.get("price") or order.get("executionPrice"),
                "filledVolume": _int(deal.get("filledVolume") or (pos.get("tradeData") or {}).get("volume")) or None,
                "position": self._position(pos) if pos else {}}

    async def _t_amend_position(self, a: dict) -> dict:
        pid = _int(a["positionId"])
        req = self._acct(positionId=pid, stopLoss=float(a["stopLoss"]), takeProfit=float(a["takeProfit"]))
        await self._order(AMEND_SLTP_REQ, req, set(EXEC_TYPE.values()) - FAILED_EXEC, position_id=pid)
        return {"ok": True}

    async def _t_close_position(self, a: dict) -> dict:
        pid = _int(a["positionId"])
        await self._order(CLOSE_POSITION_REQ, self._acct(positionId=pid, volume=_int(a["volume"])),
                          {"ORDER_ACCEPTED", "ORDER_FILLED", "ORDER_PARTIAL_FILL"}, position_id=pid)
        return {"ok": True}

    def _position(self, p: dict) -> dict:
        td = p.get("tradeData") or {}
        return {"positionId": _int(p.get("positionId")), "symbolId": _int(td.get("symbolId")),
                "tradeSide": SIDE_NAME.get(td.get("tradeSide"), str(td.get("tradeSide"))),
                "volume": _int(td.get("volume")), "entryPrice": p.get("price"), "stopLoss": p.get("stopLoss"),
                "takeProfit": p.get("takeProfit"), "label": td.get("label") or "", "comment": td.get("comment") or ""}

    async def _t_get_positions(self, a: dict) -> dict:
        _, p = await self._rpc(RECONCILE_REQ, self._acct())
        return {"positions": [self._position(x) for x in p.get("position") or []]}

    def _deal(self, d: dict) -> dict:
        return {"dealId": _int(d.get("dealId")), "positionId": _int(d.get("positionId")),
                "symbolId": _int(d.get("symbolId")), "executionPrice": d.get("executionPrice"),
                "volume": _int(d.get("filledVolume") or d.get("volume")),
                "tradeSide": SIDE_NAME.get(d.get("tradeSide"), str(d.get("tradeSide"))),
                "closePositionDetail": d.get("closePositionDetail"),
                "executionTimestamp": _int(d.get("executionTimestamp"))}

    async def _t_get_position_details(self, a: dict) -> dict:
        now_ms = int(self.clock() * 1000)
        try:
            _, p = await self._rpc(DEALS_BY_POSITION_REQ, self._acct(
                positionId=_int(a["positionId"]), fromTimestamp=now_ms - 30 * 86_400_000, toTimestamp=now_ms + 60_000))
        except ToolError:
            return {"deals": []}  # the broker falls back to the deal list
        return {"deals": [self._deal(d) for d in p.get("deal") or []]}

    async def _t_get_deals(self, a: dict) -> dict:
        to = _int(a.get("toTimestamp")) or int(self.clock() * 1000)
        frm = max(_int(a.get("fromTimestamp")), to - DEAL_WEEK_MS)
        _, p = await self._rpc(DEAL_LIST_REQ, self._acct(fromTimestamp=frm, toTimestamp=to))
        return {"deals": [self._deal(d) for d in p.get("deal") or []], "hasMore": bool(p.get("hasMore"))}

    # ------------------------------------------------------------- symbols
    async def calibrate(self, name: str, candidates: list[int | None], band: list[float]) -> int:
        """Symbol details first (digits, lot size, volume step); then the base calibration on a streamed bid."""
        await self.load_symbols()
        info = self.symbol_map.get(name.upper())
        if info is not None and "digits" not in info:
            data = await self.call("get_symbol_details", {"symbolId": [self.symbol_id(name)]})
            for s in data.get("symbols", []):
                info.update({k: _int(v) for k, v in s.items() if v is not None and k != "symbolId"})
        return await super().calibrate(name, [5, *candidates], band)

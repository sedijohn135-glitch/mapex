"""cTrader Remote MCP client (client side only, mcp==2.2.0).

Retries only read tools; a mutation times out into TransportError so the caller reconciles instead of resending
(broker-execution §3.5) — only a "Session not found" rejection, which runs nothing, is resent (D-65). One reused MCP
session is owned by a single task that serves every request in turn (D-67), so anyio scopes never cross tasks (D-64).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from mcp.shared.exceptions import MCPError

from mapex.core.primitives import Bar
from mapex.core.timeutil import PERIODS
from mapex.ctrader.decode import calibrate_digits, to_display

log = logging.getLogger("mapex.ctrader")

AUTH_RE = re.compile(r"unauthori|forbidden|expired|invalid token|\b401\b|\b403\b", re.I)
# MCP transport sessions (Mcp-Session-Id) also fail with "session" errors: reconnect first, never an auth alarm
# on its own (DECISIONS D-61). A token that really expired fails the reconnect with 401 -> AuthError.
SESSION_RE = re.compile(r"session", re.I)
LOST_SESSION_RE = re.compile(r"session (not found|terminated|expired)|re-?initiali[sz]e|no valid session", re.I)
RATE_RE = re.compile(r"rate limit|too many requests|\b429\b", re.I)
HISTORICAL_TOOLS = {"get_trendbars", "get_order_history", "get_deals"}
MUTATIONS = {"create_order", "amend_order", "cancel_order", "amend_position", "close_position"}
MAX_WINDOW_S = 720 * 3600  # Q-R7
READ_RETRIES = 2
SESSION_RETRIES = 5  # "Session not found; re-initialize": the session is reopened and the request resent (D-65)
BACKOFF_S = 0.3


class CTraderError(Exception):
    pass


class AuthError(CTraderError):
    """Token expired / unauthorised: halt new entries, alert, retry every 60 s."""


class ToolError(CTraderError):
    """The server rejected the request: fix the caller, never retry as-is."""


class TransportError(CTraderError):
    """Timeout or network failure: the outcome of a mutation is UNKNOWN."""


@dataclass(frozen=True)
class Quote:
    bid: float
    ask: float
    ts: float  # broker timestamp (s)
    fetched_at: float  # local time the quote was received

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2


class RateLimiter:
    """Minimum spacing between calls: 50/s general, 5/s historical (P4)."""

    def __init__(self, per_second: float, clock: Callable[[], float] = time.monotonic):
        self.gap = 1.0 / per_second
        self.clock = clock
        self.next_at = 0.0
        self.lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self.lock:
            now = self.clock()
            wait = self.next_at - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = self.clock()
            self.next_at = max(now, self.next_at) + self.gap


@asynccontextmanager
async def http_connector(url: str, token: str):
    """Streamable-HTTP connection with the bearer token (the token never reaches a log)."""
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"},
                                  timeout=httpx2.Timeout(30.0, read=60.0)) as http:
        # no DELETE on exit: the server answers 404 to every one (Railway logs); it expires sessions itself
        transport = streamable_http_client(url, http_client=http, terminate_on_close=False)
        async with Client(transport, read_timeout_seconds=30) as client:
            yield client


def ms_of(value) -> float:
    """Broker timestamp (epoch ms number, digit string or ISO 8601) -> epoch ms."""
    if isinstance(value, str) and not value.strip().isdigit():
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).timestamp() * 1000
    return float(value)


def iso(ms: float) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _types(prop: dict | None) -> set[str]:
    prop = prop or {}
    t = prop.get("type")
    out = set(t) if isinstance(t, list) else {t} if t else set()
    for alt in prop.get("anyOf") or prop.get("oneOf") or []:
        out |= _types(alt)
    return out


def _lost_session(exc: BaseException) -> bool:
    """A JSON-RPC answer "Session not found" (HTTP 404 on the session id): the server rejected the request before
    any tool ran, so it is safe to send it again on a reopened session — orders included (MCP spec, D-65)."""
    return isinstance(exc, MCPError) and bool(LOST_SESSION_RE.search(str(exc.message)))


async def _drop(stack: AsyncExitStack) -> None:
    """Close a session from the task that opened it; a dead session must never raise."""
    try:
        await stack.aclose()
    except BaseException as exc:  # noqa: BLE001
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            raise
        log.debug("close: %s", type(exc).__name__)


def _classify(text: str) -> CTraderError:
    if AUTH_RE.search(text or ""):
        return AuthError((text or "unauthorised")[:200])
    if SESSION_RE.search(text or ""):
        return TransportError((text or "session")[:200])
    return ToolError((text or "tool error")[:500])


class CTraderClient:
    def __init__(self, url: str = "", token: str = "", connector=http_connector,
                 clock: Callable[[], float] = time.time, call_timeout: float = 20.0):
        self.url, self.token = url, token
        self.connector = connector
        self.clock = clock
        self.call_timeout = call_timeout
        self.tools: dict[str, set[str]] = {}
        self.schemas: dict[str, dict[str, dict]] = {}  # tool -> input properties (types drive the argument encoding)
        self.ms_text: set[str] = set()  # tools whose string timestamps are epoch-ms text rather than ISO 8601
        self.symbol_map: dict[str, dict] = {}
        self.digits: dict[str, int] = {}  # spot encoding, calibrated on a live bid
        self.bar_digits: dict[str, int] = {}  # trendbar encoding, calibrated on the last close
        self.bands: dict[str, list[float]] = {}
        self.raw_bid: dict[str, float] = {}
        self.last_auth_detail: str | None = None
        self.general = RateLimiter(50)
        self.historical = RateLimiter(5)
        self.auth_error_since: float | None = None
        self.last_error: str | None = None
        self.skew_s = 0.0
        self.sessions_opened = 0
        self._queue: asyncio.Queue | None = None
        self._owner: asyncio.Task | None = None

    # ------------------------------------------------------------- connection
    @property
    def configured(self) -> bool:
        return bool(self.url and self.token)

    @property
    def trading_profile(self) -> bool:
        return "create_order" in self.tools

    async def set_credentials(self, url: str, token: str) -> None:
        """Hot-swap (Telegram /ctrader): the session is reopened with the new token and its tools re-read."""
        await self.close()
        self.url, self.token = url, token
        self.tools, self.schemas, self.ms_text = {}, {}, set()
        self.auth_error_since = None

    async def close(self) -> None:
        """Stop the session owner; it closes the session in its own task."""
        owner, self._owner, self._queue = self._owner, None, None
        if owner is not None and not owner.done() and owner.get_loop() is asyncio.get_running_loop():
            owner.cancel()
            await asyncio.gather(owner, return_exceptions=True)

    def _mailbox(self) -> asyncio.Queue:
        loop = asyncio.get_running_loop()
        if self._owner is None or self._owner.done() or self._owner.get_loop() is not loop:
            self._queue = asyncio.Queue()
            self._owner = loop.create_task(self._serve(self._queue), name="ctrader-session")
        return self._queue

    async def _serve(self, queue: asyncio.Queue) -> None:
        """The only task that opens, uses and closes the MCP session. Requests go out one at a time on one reused
        session: many short sessions made the live server answer "Session not found" (Railway logs, D-67).
        A lost session is reopened and the request resent (D-65); a timeout is never resent."""
        stack: AsyncExitStack | None = None
        session = None
        fut = None
        try:
            while True:
                tool, args, fut = await queue.get()
                for attempt in range(SESSION_RETRIES + 1):
                    if fut.done():  # the caller gave up before its request went out
                        break
                    try:
                        async with asyncio.timeout(self.call_timeout):
                            if session is None:
                                stack = AsyncExitStack()
                                session = await stack.enter_async_context(self.connector(self.url, self.token))
                                self.sessions_opened += 1
                                if not self.tools:
                                    self._load_tools(await session.list_tools())
                            if tool not in self.tools:
                                raise ToolError(f"tool {tool} not offered by this connection (data-only token?)")
                            res = await session.call_tool(tool, self._fit_args(tool, self._filter_args(tool, args)))
                        if not fut.done():
                            fut.set_result(res)
                        break
                    except Exception as exc:  # noqa: BLE001 — handed to the caller
                        lost = _lost_session(exc)
                        if lost or not isinstance(exc, ToolError | MCPError):  # the session itself is suspect
                            if stack is not None:
                                await _drop(stack)
                            stack = session = None
                        if lost and attempt < SESSION_RETRIES:
                            await asyncio.sleep(BACKOFF_S * (attempt + 1))
                            continue
                        if not fut.done():
                            fut.set_exception(exc)
                        break
        finally:
            closed = TransportError("cTrader session closed")
            for pending in [fut] + [queue.get_nowait()[2] for _ in range(queue.qsize())]:
                if pending is not None and not pending.done():
                    pending.set_exception(closed)
            if stack is not None:
                await _drop(stack)

    def _wrap(self, exc: BaseException) -> CTraderError:
        while isinstance(exc, BaseExceptionGroup) and len(exc.exceptions) == 1:
            exc = exc.exceptions[0]
        if isinstance(exc, CTraderError):
            return exc
        text = f"{type(exc).__name__}: {exc}".replace(self.token or "\0", "***")
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status in (401, 403) or AUTH_RE.search(text):
            return AuthError(f"HTTP {status}: {text[:200]}" if status else text[:200])
        if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
            return TransportError("timeout")
        return TransportError(text[:300])

    def _filter_args(self, tool: str, args: dict) -> dict:
        """Schema-fields-only enforcement (playbook 1.5): drop keys the live schema does not declare."""
        allowed = self.tools.get(tool)
        if not allowed:
            return args
        dropped = [k for k in args if k not in allowed]
        if dropped:
            log.warning("%s: dropping undeclared fields %s", tool, dropped)
        return {k: v for k, v in args.items() if k in allowed}

    def _fit_args(self, tool: str, args: dict) -> dict:
        """Numbers the live schema declares as strings are sent as text; timestamps as ISO 8601 (or epoch-ms text
        when the schema or the server says so). Railway log: get_trendbars rejected numeric timestamps (D-65)."""
        props = self.schemas.get(tool) or {}
        out = {}
        for k, v in args.items():
            types = _types(props.get(k))
            if isinstance(v, int | float) and not isinstance(v, bool) and "string" in types \
                    and not types & {"integer", "number"}:
                v = (str(int(v)) if tool in self.ms_text else iso(v)) if k.endswith("Timestamp") else str(v)
            out[k] = v
        return out

    def _load_tools(self, listed) -> None:
        self.tools = {t.name: set((t.input_schema or {}).get("properties", {}).keys()) for t in listed.tools}
        self.schemas = {t.name: dict((t.input_schema or {}).get("properties", {})) for t in listed.tools}
        for tool, props in self.schemas.items():
            for k, prop in props.items():
                if k.endswith("Timestamp"):
                    log.info("schema %s.%s: %s", tool, k, json.dumps(prop)[:200])
                    hint = f"{prop.get('description', '')} {prop.get('format', '')}".lower()
                    if "string" in _types(prop) and re.search(r"milli|epoch|unix", hint) and "iso" not in hint:
                        self.ms_text.add(tool)

    async def call(self, tool: str, args: dict | None = None) -> dict:
        args = dict(args or {})
        mutation = tool in MUTATIONS
        attempt = 0
        while True:
            try:
                return await self._call_once(tool, args)
            except AuthError as exc:
                self.auth_error_since = self.auth_error_since or self.clock()
                self.last_auth_detail = str(exc).replace(self.token or "\0", "***")[:200]
                raise
            except ToolError as exc:
                if not mutation and RATE_RE.search(str(exc)) and attempt < READ_RETRIES:
                    await asyncio.sleep(0.5 * 2**attempt)
                    attempt += 1
                    continue
                if not mutation and "Timestamp" in str(exc) and tool not in self.ms_text \
                        and any("string" in _types(p) for k, p in self.schemas.get(tool, {}).items()
                                if k.endswith("Timestamp")):
                    log.warning("%s rejected ISO timestamps; switching to epoch-ms text", tool)
                    self.ms_text.add(tool)
                    continue
                raise
            except TransportError:
                if mutation or attempt >= READ_RETRIES:
                    raise
                await asyncio.sleep(min(BACKOFF_S * 2**attempt, 3.0))
                attempt += 1

    async def _call_once(self, tool: str, args: dict) -> dict:
        if not self.configured:
            raise AuthError("cTrader configuration missing")
        await (self.historical if tool in HISTORICAL_TOOLS else self.general).acquire()
        fut = asyncio.get_running_loop().create_future()
        self._mailbox().put_nowait((tool, args, fut))
        try:
            res = await fut
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            err = self._wrap(exc)
            self.last_error = f"{tool}: {err}"
            raise err from None
        text = "\n".join(getattr(c, "text", "") for c in (res.content or []))
        if res.is_error:
            err = _classify(text)
            self.last_error = f"{tool}: {err}"
            raise err
        self.auth_error_since = None
        if isinstance(res.structured_content, dict):
            return res.structured_content
        try:
            data = json.loads(text) if text else {}
        except ValueError:
            data = {"text": text}
        if isinstance(data, str) and AUTH_RE.search(data):
            raise AuthError(data[:200])
        return data if isinstance(data, dict) else {"result": data}

    # ------------------------------------------------------------- symbols & digits
    async def load_symbols(self) -> dict[str, dict]:
        if not self.symbol_map:
            data = await self.call("get_symbols")
            for s in data.get("symbols", data.get("result", [])) or []:
                name = str(s.get("symbolName", "")).upper()
                if name:
                    self.symbol_map[name] = s
        return self.symbol_map

    def symbol_id(self, name: str) -> int:
        info = self.symbol_map.get(name.upper())
        if info is None:
            raise ToolError(f"symbol {name} not in broker catalog")
        return int(info["symbolId"])

    def meta_digits(self, name: str) -> int | None:
        info = self.symbol_map.get(name.upper(), {})
        for key in ("pipDigits", "digits"):
            if isinstance(info.get(key), int):
                return info[key]
        return None

    async def calibrate(self, name: str, candidates: list[int | None], band: list[float]) -> int:
        """Resolve digits and prove them against a live bid (raises DecodeError: symbol disabled + alert)."""
        raw = await self.raw_spot([name])
        if name not in raw:
            raise ToolError(f"no live quote for {name}")
        self.raw_bid[name] = float(raw[name]["bid"])
        self.bands[name] = band
        d = calibrate_digits(self.raw_bid[name], [self.meta_digits(name), *candidates], band)
        self.digits[name] = d
        return d

    # ------------------------------------------------------------- market data
    async def raw_spot(self, names: list[str]) -> dict[str, dict]:
        await self.load_symbols()
        known = [n for n in names if n.upper() in self.symbol_map]  # P2 / Q-R8: never send an unknown id
        if not known:
            return {}
        ids = {self.symbol_id(n): n for n in known}
        data = await self.call("get_spot_prices", {"symbolId": list(ids)})
        out = {}
        for p in data.get("prices", []) or []:
            name = ids.get(int(p.get("symbolId", -1)))
            if name and p.get("bid") and p.get("ask"):
                out[name] = p
        return out

    async def spot(self, names: list[str]) -> dict[str, Quote]:
        now = self.clock()
        out = {}
        for name, p in (await self.raw_spot([n for n in names if n in self.digits])).items():
            d = self.digits[name]
            ts = ms_of(p.get("timestamp") or now * 1000) / 1000
            self.skew_s = ts - now
            out[name] = Quote(to_display(p["bid"], d), to_display(p["ask"], d), ts, now)
        return out

    async def trendbars(self, name: str, tf: str, frm: int, to: int) -> list[Bar]:
        """Closed-and-forming bars in [frm, to), chunked into <= 720 h windows, deduped by open time (Q-R7)."""
        await self.load_symbols()
        sid = self.symbol_id(name)
        bars: dict[int, Bar] = {}  # raw (unscaled) values; scaled once the encoding is known
        start = frm
        while start < to:
            end = min(to, start + MAX_WINDOW_S)
            cursor = start
            while True:
                data = await self.call("get_trendbars", {"symbolId": sid, "period": PERIODS[tf],
                                                         "fromTimestamp": cursor * 1000, "toTimestamp": end * 1000})
                rows = data.get("trendbars") or data.get("trendBars") or data.get("bars") or []
                parsed = [b for b in (parse_trendbar(r, 0) for r in rows) if b is not None]
                for b in parsed:
                    bars[b.t] = b
                if not data.get("hasMore") or not parsed or max(b.t for b in parsed) + 1 >= end:
                    break
                cursor = max(b.t for b in parsed) + 1
            start = end
        raw = [bars[t] for t in sorted(bars) if frm <= t < to]
        if not raw:
            return raw
        if name not in self.bar_digits:  # trendbars may be encoded differently from spot: prove it on a close
            self.bar_digits[name] = calibrate_digits(raw[-1].c, [self.digits.get(name)],
                                                     self.bands.get(name, [0, 1e12]))
        k = 10 ** self.bar_digits[name]
        return [Bar(b.t, b.o / k, b.h / k, b.l / k, b.c / k) for b in raw]


def parse_trendbar(row: dict[str, Any], digits: int) -> Bar | None:
    try:
        ts = row.get("timestamp")
        if ts is None and row.get("utcTimestampInMinutes") is not None:
            ts = int(row["utcTimestampInMinutes"]) * 60_000
        ts = int(ms_of(ts)) // 1000
        if "open" in row and "close" in row:
            o, h, l, c = (float(row[k]) for k in ("open", "high", "low", "close"))
        else:
            low = float(row["low"])
            o, h, c = (low + float(row.get(k, 0)) for k in ("deltaOpen", "deltaHigh", "deltaClose"))
            l = low
        s = 10**digits
        return Bar(ts, o / s, h / s, l / s, c / s)
    except (KeyError, TypeError, ValueError):
        return None

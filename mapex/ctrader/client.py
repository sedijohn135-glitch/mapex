"""cTrader Remote MCP client (client side only, mcp==2.2.0).

Retries only read tools; a mutation is sent exactly once and a timeout surfaces as TransportError so the caller
reconciles instead of resending (broker-execution §3.5).
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
from datetime import datetime
from typing import Any

from mapex.core.primitives import Bar
from mapex.core.timeutil import PERIODS
from mapex.ctrader.decode import calibrate_digits, to_display

log = logging.getLogger("mapex.ctrader")

AUTH_RE = re.compile(r"unauthori|forbidden|expired|invalid token|\b401\b|\b403\b", re.I)
# MCP transport sessions (Mcp-Session-Id) also fail with "session" errors: reconnect first, never an auth alarm
# on its own (DECISIONS D-61). A token that really expired fails the reconnect with 401 -> AuthError.
SESSION_RE = re.compile(r"session", re.I)
RATE_RE = re.compile(r"rate limit|too many requests|\b429\b", re.I)
HISTORICAL_TOOLS = {"get_trendbars", "get_order_history", "get_deals"}
MUTATIONS = {"create_order", "amend_order", "cancel_order", "amend_position", "close_position"}
MAX_WINDOW_S = 720 * 3600  # Q-R7
READ_RETRIES = 2


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
        async with Client(streamable_http_client(url, http_client=http), read_timeout_seconds=30) as client:
            yield client


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
        self.stack: AsyncExitStack | None = None
        self.session = None
        self.tools: dict[str, set[str]] = {}
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
        self.lock = asyncio.Lock()

    # ------------------------------------------------------------- connection
    @property
    def configured(self) -> bool:
        return bool(self.url and self.token)

    @property
    def trading_profile(self) -> bool:
        return "create_order" in self.tools

    async def set_credentials(self, url: str, token: str) -> None:
        """Hot-swap (Telegram /ctrader): drop the session, the next call reconnects."""
        await self.close()
        self.url, self.token = url, token
        self.auth_error_since = None

    async def close(self) -> None:
        if self.stack is not None:
            try:
                await self.stack.aclose()
            except Exception as exc:  # noqa: BLE001 — closing a dead session must never raise
                log.debug("close: %s", type(exc).__name__)
        self.stack, self.session = None, None

    async def _ensure(self):
        if self.session is not None:
            return self.session
        if not self.configured:
            raise AuthError("cTrader configuration missing")
        stack = AsyncExitStack()
        try:
            session = await asyncio.wait_for(stack.enter_async_context(self.connector(self.url, self.token)),
                                             self.call_timeout)
            listed = await asyncio.wait_for(session.list_tools(), self.call_timeout)
        except BaseException as exc:
            await stack.aclose()
            raise self._wrap(exc) from None
        self.tools = {t.name: set((t.input_schema or {}).get("properties", {}).keys()) for t in listed.tools}
        self.stack, self.session = stack, session
        return session

    def _wrap(self, exc: BaseException) -> CTraderError:
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

    async def call(self, tool: str, args: dict | None = None) -> dict:
        args = dict(args or {})
        mutation = tool in MUTATIONS
        attempts = 1 if mutation else READ_RETRIES + 1
        for attempt in range(attempts):
            try:
                return await self._call_once(tool, args)
            except AuthError as exc:
                self.auth_error_since = self.auth_error_since or self.clock()
                self.last_auth_detail = str(exc).replace(self.token or "\0", "***")[:200]
                raise
            except ToolError as exc:
                if not mutation and RATE_RE.search(str(exc)) and attempt + 1 < attempts:
                    await asyncio.sleep(0.5 * 2**attempt)
                    continue
                raise
            except TransportError:
                await self.close()
                if mutation or attempt + 1 >= attempts:
                    raise
                await asyncio.sleep(0.5 * 2**attempt)
        raise TransportError("unreachable")

    async def _call_once(self, tool: str, args: dict) -> dict:
        async with self.lock:
            session = await self._ensure()
        if self.tools and tool not in self.tools:
            raise ToolError(f"tool {tool} not offered by this connection (data-only token?)")
        await (self.historical if tool in HISTORICAL_TOOLS else self.general).acquire()
        try:
            res = await asyncio.wait_for(session.call_tool(tool, self._filter_args(tool, args)), self.call_timeout)
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
            ts = float(p.get("timestamp") or now * 1000) / 1000
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
        if isinstance(ts, str) and not ts.strip().isdigit():
            ts = datetime.fromisoformat(ts.strip().replace("Z", "+00:00")).timestamp() * 1000
        ts = int(float(ts)) // 1000
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

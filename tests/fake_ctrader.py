"""In-process fake of the cTrader Remote MCP (trading profile), reproducing the documented quirks:
Q-R1 period enum, Q-R4 relative SL/TP on market orders, Q-R7 720 h cap, Q-R8 batch poisoning,
Q-R10 amend omit-removes, pipettes on market data. Records every call."""

from __future__ import annotations

import asyncio
import itertools
from datetime import datetime

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.shared.exceptions import MCPError

SYMBOLS = {"XAUUSD": (41, 3), "BTCUSD": (101, 2)}
PERIODS = {"M_1", "M_5", "M_15", "M_30", "H_1", "H_4", "D_1", "W_1", "MN_1"}


class FakeCTrader:
    def __init__(self, price_encoding: str = "display"):
        self.calls: list[tuple[str, dict]] = []
        self.quotes = {"XAUUSD": (2650.00, 2650.20, 0), "BTCUSD": (60000.0, 60020.0, 0)}  # bid, ask, ts(ms)
        self.bars: dict[tuple[str, str], list[tuple]] = {}
        self.positions: dict[int, dict] = {}
        self.deals: list[dict] = []
        self.ids = itertools.count(1000)
        self.mode: set[str] = set()
        self.price_encoding = price_encoding
        self.auth_fail = False
        self.delay = 0.0
        self.fail_modes: dict[str, str] = {}
        self.now_ms = 1_790_000_000_000
        self.spot_encoding = "pipettes"  # pipettes | display | e5 | pip2
        self.bar_encoding = "pipettes"
        self.points_digits: dict[str, int] = {}  # how the server reads relative SL/TP points (default: digits)
        self.fail_once: dict[str, str] = {}
        self.fail_symbol: dict[int, int] = {}  # symbolId -> number of spot calls answered "Session not found"
        self.session_drops = 0  # the next N requests (any tool) are rejected with "Session not found", unexecuted
        self.newest = 0  # evicting_connector: the only session the server still knows
        self.evicted = 0  # requests rejected because a newer session replaced theirs
        self.ts_format: str | None = None  # None: numeric timestamps; "iso" | "ms": string timestamps (live server)

    # ------------------------------------------------------------ helpers
    def sym(self, sid: int) -> str:
        for name, (i, _) in SYMBOLS.items():
            if i == sid:
                return name
        raise ToolError("uProxy error: UNKNOWN_SYMBOL")

    def enc(self, name: str, price: float | None):
        if price is None:
            return None
        if self.price_encoding == "pipettes":
            return round(price * 10 ** SYMBOLS[name][1])
        return round(price, 2)

    def log(self, tool: str, args: dict):
        if self.session_drops > 0:
            self.session_drops -= 1
            raise lost_session()
        self.calls.append((tool, {k: v for k, v in args.items() if v is not None}))
        if self.auth_fail:
            raise ToolError("401 Unauthorized: token expired")
        if tool in self.fail_once:
            raise ToolError(self.fail_once.pop(tool))

    def parse_ts(self, text: str) -> int:
        if self.ts_format == "ms":
            if not text.isdigit():
                raise ToolError("Input validation error: fromTimestamp/toTimestamp: expected epoch milliseconds")
            return int(text)
        try:
            return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            raise ToolError("Input validation error: fromTimestamp/toTimestamp: invalid ISO datetime") from None

    def scale(self, name: str, encoding: str):
        return {"pipettes": 10 ** SYMBOLS[name][1], "display": 1, "e5": 10**5, "pip2": 100}[encoding]

    def raw(self, name: str, price: float, encoding: str):
        v = price * self.scale(name, encoding)
        return round(v, 2) if encoding == "display" else round(v)

    def pos_view(self, p: dict) -> dict:
        name = self.sym(p["symbolId"])
        return {**p, "entryPrice": self.enc(name, p["entryPrice"]), "stopLoss": self.enc(name, p["stopLoss"]),
                "takeProfit": self.enc(name, p["takeProfit"])}

    def close(self, pid: int, volume: int, price: float | None = None):
        p = self.positions[pid]
        name = self.sym(p["symbolId"])
        bid, ask, _ = self.quotes[name]
        px = price if price is not None else (bid if p["tradeSide"] == "BUY" else ask)
        self.deals.append({"dealId": next(self.ids), "positionId": pid, "executionPrice": self.enc(name, px),
                           "volume": volume, "tradeSide": "SELL" if p["tradeSide"] == "BUY" else "BUY",
                           "executionTimestamp": self.now_ms, "dealStatus": "FILLED", "isClosing": True})
        p["volume"] -= volume
        if p["volume"] <= 0:
            del self.positions[pid]

    def open_manual(self, name: str, side: str, volume: int, label: str = "", comment: str = "") -> int:
        pid = next(self.ids)
        bid, ask, _ = self.quotes[name]
        self.positions[pid] = {"positionId": pid, "symbolId": SYMBOLS[name][0], "tradeSide": side, "volume": volume,
                               "entryPrice": ask if side == "BUY" else bid, "stopLoss": None, "takeProfit": None,
                               "label": label, "comment": comment}
        return pid


def build_server(fake: FakeCTrader) -> MCPServer:
    srv = MCPServer("fake-ctrader")

    @srv.tool()
    def get_version() -> dict:
        fake.log("get_version", {})
        return {"version": "fake-1.0.18"}

    @srv.tool()
    def get_symbols() -> dict:
        fake.log("get_symbols", {})
        return {"symbols": [{"symbolId": sid, "symbolName": n, "enabled": True} for n, (sid, _) in SYMBOLS.items()]}

    @srv.tool()
    def get_spot_prices(symbolId: list[int]) -> dict:
        fake.log("get_spot_prices", {"symbolId": symbolId})
        for sid in symbolId:
            if fake.fail_symbol.get(sid, 0) > 0:
                fake.fail_symbol[sid] -= 1
                raise lost_session()
        known = {sid: n for n, (sid, _) in SYMBOLS.items()}
        if any(s not in known for s in symbolId):
            return {"prices": []}  # Q-R8 batch poisoning
        out = []
        for s in symbolId:
            n = known[s]
            bid, ask, ts = fake.quotes[n]
            out.append({"symbolId": s, "bid": fake.raw(n, bid, fake.spot_encoding),
                        "ask": fake.raw(n, ask, fake.spot_encoding), "timestamp": ts or fake.now_ms})
        return {"prices": out}

    if fake.ts_format:
        @srv.tool(name="get_trendbars")
        def get_trendbars_text(symbolId: int, period: str, fromTimestamp: str, toTimestamp: str) -> dict:
            fake.log("get_trendbars", {"symbolId": symbolId, "period": period, "fromTimestamp": fromTimestamp,
                                       "toTimestamp": toTimestamp})
            return trendbars(symbolId, period, fake.parse_ts(fromTimestamp), fake.parse_ts(toTimestamp))
    else:
        @srv.tool()
        def get_trendbars(symbolId: int, period: str, fromTimestamp: int, toTimestamp: int) -> dict:
            fake.log("get_trendbars", {"symbolId": symbolId, "period": period, "fromTimestamp": fromTimestamp,
                                       "toTimestamp": toTimestamp})
            return trendbars(symbolId, period, fromTimestamp, toTimestamp)

    def trendbars(symbolId: int, period: str, fromTimestamp: int, toTimestamp: int) -> dict:
        if period not in PERIODS:
            raise ToolError("Input validation error: period")
        if toTimestamp - fromTimestamp > 720 * 3600 * 1000:
            raise ToolError("Time range exceeds upstream cap of 720h (PT720H = 30 days).")
        name = fake.sym(symbolId)
        enc = fake.bar_encoding
        tf = {"M_1": "M1", "M_5": "M5", "M_15": "M15", "M_30": "M30", "H_1": "H1", "H_4": "H4", "D_1": "D1",
              "W_1": "W1", "MN_1": "MN1"}[period]
        rows = [b for b in fake.bars.get((name, tf), []) if fromTimestamp <= b[0] * 1000 < toTimestamp]
        page, more = rows[:1000], len(rows) > 1000
        return {"trendbars": [{"timestamp": b[0] * 1000, "open": fake.raw(name, b[1], enc),
                               "high": fake.raw(name, b[2], enc), "low": fake.raw(name, b[3], enc),
                               "close": fake.raw(name, b[4], enc)} for b in page],
                "hasMore": more}

    @srv.tool()
    async def create_order(symbolId: int, orderType: str, tradeSide: str, volume: int,
                           relativeStopLoss: int | None = None, relativeTakeProfit: int | None = None,
                           stopLoss: float | None = None, takeProfit: float | None = None,
                           baseSlippagePrice: float | None = None, slippageInPoints: int | None = None,
                           label: str | None = None, comment: str | None = None) -> dict:
        fake.log("create_order", {k: v for k, v in locals().items() if k != "fake"})
        name = fake.sym(symbolId)
        if "reject" in fake.mode:
            raise ToolError("INVALID_REQUEST: market closed")
        if orderType in ("MARKET", "MARKET_RANGE") and (stopLoss is not None or takeProfit is not None):
            raise ToolError("create_order: Absolute stopLoss is not supported for MARKET orders")
        if volume <= 0:
            raise ToolError("volume must be positive")
        bid, ask, _ = fake.quotes[name]
        d = fake.points_digits.get(name, SYMBOLS[name][1])
        fill = ask if tradeSide == "BUY" else bid
        sgn = 1 if tradeSide == "BUY" else -1
        sl = None if relativeStopLoss is None or "no_sl" in fake.mode else fill - sgn * relativeStopLoss / 10**d
        tp = None if relativeTakeProfit is None else fill + sgn * relativeTakeProfit / 10**d
        vol = volume * 100 if "volume_x100" in fake.mode else volume
        if "partial_fill" in fake.mode:
            vol = volume // 2
        pid = None
        if "timeout_nofill" not in fake.mode:
            pid = next(fake.ids)
            fake.positions[pid] = {"positionId": pid, "symbolId": symbolId, "tradeSide": tradeSide, "volume": vol,
                                   "entryPrice": fill, "stopLoss": sl, "takeProfit": tp, "label": label or "",
                                   "comment": comment or ""}
        if "timeout" in fake.mode or "timeout_nofill" in fake.mode:
            await asyncio.sleep(5)
        return {"orderId": next(fake.ids), "positionId": pid, "executionPrice": fake.enc(name, fill),
                "filledVolume": vol, "dealStatus": "FILLED"}

    @srv.tool()
    def amend_position(positionId: int, stopLoss: float | None = None, takeProfit: float | None = None) -> dict:
        fake.log("amend_position", {"positionId": positionId, "stopLoss": stopLoss, "takeProfit": takeProfit})
        p = fake.positions.get(positionId)
        if p is None:
            raise ToolError("POSITION_NOT_FOUND")
        if "amend_ignored" not in fake.mode:
            p["stopLoss"] = stopLoss  # Q-R10: an omitted leg is REMOVED
        p["takeProfit"] = takeProfit
        return {"position": fake.pos_view(p)}

    @srv.tool()
    def close_position(positionId: int, volume: int) -> dict:
        fake.log("close_position", {"positionId": positionId, "volume": volume})
        if positionId not in fake.positions:
            raise ToolError("POSITION_NOT_FOUND")
        fake.close(positionId, volume)
        return {"closed": True}

    @srv.tool()
    def get_positions() -> dict:
        fake.log("get_positions", {})
        return {"positions": [fake.pos_view(p) for p in fake.positions.values()], "orders": []}

    @srv.tool()
    def get_position_details(positionId: int) -> dict:
        fake.log("get_position_details", {"positionId": positionId})
        p = fake.positions.get(positionId)
        if p is None:
            return {"position": None, "deals": [d for d in fake.deals if d["positionId"] == positionId]}
        return {"position": fake.pos_view(p), "deals": [d for d in fake.deals if d["positionId"] == positionId]}

    @srv.tool()
    def get_deals(fromTimestamp: int, toTimestamp: int) -> dict:
        fake.log("get_deals", {"fromTimestamp": fromTimestamp, "toTimestamp": toTimestamp})
        return {"deals": list(fake.deals), "hasMore": False}

    return srv


def lost_session() -> MCPError:
    return MCPError(-32001, "Session not found; re-initialize")


def connector_for(server):
    """CTraderClient connector that talks to the in-process fake server."""
    from mcp import Client

    def connect(url, token):
        return Client(server)

    return connect


class _Guarded:
    def __init__(self, client, fake, mine: int):
        self.client, self.fake, self.mine = client, fake, mine

    async def list_tools(self):
        return await self.client.list_tools()

    async def call_tool(self, name, args):
        if self.mine != self.fake.newest:
            self.fake.evicted += 1
            raise lost_session()
        return await self.client.call_tool(name, args)


def evicting_connector(fake: FakeCTrader, server):
    """A server that keeps only the newest session per token: requests on an older one get "Session not found"."""
    from contextlib import asynccontextmanager

    from mcp import Client

    @asynccontextmanager
    async def connect(url, token):
        fake.newest += 1
        mine = fake.newest
        async with Client(server) as client:
            yield _Guarded(client, fake, mine)

    return connect

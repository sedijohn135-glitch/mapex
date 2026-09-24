"""In-process fake of the cTrader Open API JSON protocol (spotware/openapi-proto-messages payload types).

Answers the way the JSON gateway may: ids sometimes as strings, enums sometimes as names, execution events with or
without the request's clientMsgId. Records every request."""

from __future__ import annotations

import asyncio
import itertools
import json

SPOT_SCALE = 100_000
PERIOD_LIMIT_MS = {1: 302_400_000, 5: 302_400_000, 7: 21_168_000_000, 8: 21_168_000_000, 9: 21_168_000_000,
                   10: 31_622_400_000, 12: 31_622_400_000, 13: 158_112_000_000, 14: 158_112_000_000}


class FakeWS:
    def __init__(self, server: FakeOpenApi, url: str):
        self.server, self.url = server, url
        self.q: asyncio.Queue = asyncio.Queue()
        self.closed = False

    async def send(self, text: str) -> None:
        if self.closed:
            raise ConnectionError("socket closed")
        for out in self.server.handle(self, json.loads(text)):
            self.q.put_nowait(json.dumps(out))

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.q.put_nowait(None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.q.get()
        if item is None:
            raise StopAsyncIteration
        return item


class FakeOpenApi:
    def __init__(self):
        self.client_id, self.client_secret = "cid", "csecret"
        self.valid_access, self.valid_refresh = {"acc-1"}, {"ref-1"}
        self.accounts = [{"ctidTraderAccountId": "777", "isLive": False, "traderLogin": "5123"}]
        self.scope = "SCOPE_TRADE"
        self.symbols = {41: ("XAUUSD", 2, 10_000), 101: ("BTCUSD", 2, 100)}  # id: name, digits, lotSize (cents)
        self.quotes = {41: (2650.00, 2650.20), 101: (60000.0, 60020.0)}
        self.bars: dict[tuple[int, int], list[tuple]] = {}  # (symbolId, period) -> [(t_s, o, h, l, c)]
        self.positions: dict[int, dict] = {}
        self.deals: list[dict] = []
        self.ids = itertools.count(5000)
        self.tokens = itertools.count(2)
        self.connects: list[str] = []
        self.sockets: list[FakeWS] = []
        self.sent: list[tuple[int, dict]] = []
        self.reject_orders: str | None = None
        self.silent_orders = False
        self.exec_with_cid = True
        self.now_ms = 1_790_000_000_000

    async def connect(self, url: str) -> FakeWS:
        self.connects.append(url)
        ws = FakeWS(self, url)
        self.sockets.append(ws)
        return ws

    def requests(self, pt: int) -> list[dict]:
        return [p for t, p in self.sent if t == pt]

    # ------------------------------------------------------------------ protocol
    def handle(self, ws: FakeWS, msg: dict) -> list[dict]:
        pt, p, cid = int(msg["payloadType"]), msg.get("payload") or {}, msg.get("clientMsgId")
        if pt == 51:
            return []
        self.sent.append((pt, p))

        def res(rpt, payload):
            return {"clientMsgId": cid, "payloadType": rpt, "payload": payload}

        def err(code, desc=""):
            return res(2142, {"errorCode": code, "description": desc})

        if pt == 2100:
            ok = (p.get("clientId"), p.get("clientSecret")) == (self.client_id, self.client_secret)
            ws.app = ok
            return [res(2101, {})] if ok else [err("CH_CLIENT_AUTH_FAILURE", "bad client credentials")]
        if not getattr(ws, "app", False):
            return [err("CH_CLIENT_NOT_AUTHENTICATED")]
        if pt == 2173:
            if p.get("refreshToken") not in self.valid_refresh:
                return [err("CH_ACCESS_TOKEN_INVALID", "refresh token unknown")]
            n = next(self.tokens)
            self.valid_refresh.discard(p["refreshToken"])
            self.valid_access, self.valid_refresh = {f"acc-{n}"}, self.valid_refresh | {f"ref-{n}"}
            return [res(2174, {"accessToken": f"acc-{n}", "tokenType": "bearer", "expiresIn": 2_628_000,
                               "refreshToken": f"ref-{n}"})]
        if pt == 2149:
            if p.get("accessToken") not in self.valid_access:
                return [err("CH_ACCESS_TOKEN_INVALID", "token expired")]
            return [res(2150, {"accessToken": p["accessToken"], "permissionScope": self.scope,
                               "ctidTraderAccount": self.accounts})]
        if pt == 2102:
            if p.get("accessToken") not in self.valid_access:
                return [err("CH_ACCESS_TOKEN_INVALID", "token expired")]
            acc = next((a for a in self.accounts if int(a["ctidTraderAccountId"]) == p["ctidTraderAccountId"]), None)
            if acc is None or bool(acc["isLive"]) != ("live." in ws.url):
                return [err("CH_CTID_TRADER_ACCOUNT_NOT_FOUND", "account not on this host")]
            ws.acct = True
            return [res(2103, {"ctidTraderAccountId": p["ctidTraderAccountId"]})]
        if pt == 2104:
            return [res(2105, {"version": "fake-oa-1"})]
        if not getattr(ws, "acct", False):
            return [err("ACCOUNT_NOT_AUTHORIZED")]
        if pt == 2114:
            return [res(2115, {"symbol": [{"symbolId": str(i), "symbolName": n, "enabled": True}
                                          for i, (n, _, _) in self.symbols.items()]})]
        if pt == 2116:
            return [res(2117, {"symbol": [{"symbolId": i, "digits": self.symbols[i][1], "pipPosition": 1,
                                           "lotSize": self.symbols[i][2], "stepVolume": 1, "minVolume": 1}
                                          for i in p["symbolId"] if i in self.symbols]})]
        if pt == 2127:
            return [res(2128, {})] + [self.spot(i) for i in p["symbolId"]]
        if pt == 2137:
            return [self.trendbars(p, res, err)]
        if pt == 2106:
            return self.new_order(p, cid, err)
        if pt == 2110:
            pos = self.positions.get(p["positionId"])
            if pos is None:
                return [res(2132, {"errorCode": "POSITION_NOT_FOUND"})]
            pos["stopLoss"], pos["takeProfit"] = p.get("stopLoss"), p.get("takeProfit")  # omitted = removed
            return [self.exec_event(cid, "ORDER_REPLACED", pos)]
        if pt == 2111:
            return self.close(p, cid)
        if pt == 2124:
            return [res(2125, {"position": [self.view(x) for x in self.positions.values()]})]
        if pt == 2179:
            mine = [d for d in self.deals if d["positionId"] == p["positionId"]]
            return [res(2180, {"deal": mine, "hasMore": False})]
        if pt == 2133:
            if p["toTimestamp"] - p["fromTimestamp"] > 604_800_000:
                return [err("INCORRECT_BOUNDARIES", "range over one week")]
            return [res(2134, {"deal": list(self.deals), "hasMore": False})]
        return [err("UNSUPPORTED", str(pt))]

    def spot(self, sid: int) -> dict:
        bid, ask = self.quotes[sid]
        return {"payloadType": 2131, "payload": {"symbolId": sid, "bid": round(bid * SPOT_SCALE),
                                                 "ask": round(ask * SPOT_SCALE), "timestamp": self.now_ms}}

    def trendbars(self, p: dict, res, err) -> dict:
        if not isinstance(p.get("period"), int) or p["period"] not in PERIOD_LIMIT_MS:
            return err("INVALID_REQUEST", "period")
        if p["toTimestamp"] - p["fromTimestamp"] > PERIOD_LIMIT_MS[p["period"]]:
            return err("INCORRECT_BOUNDARIES", "range too long for period")
        rows = []
        for t, o, h, lo, c in self.bars.get((p["symbolId"], p["period"]), []):
            if p["fromTimestamp"] <= t * 1000 < p["toTimestamp"]:
                low = round(lo * SPOT_SCALE)
                rows.append({"volume": 10, "low": str(low), "deltaOpen": round(o * SPOT_SCALE) - low,
                             "deltaHigh": round(h * SPOT_SCALE) - low, "deltaClose": round(c * SPOT_SCALE) - low,
                             "utcTimestampInMinutes": t // 60})
        return res(2138, {"period": p["period"], "symbolId": p["symbolId"], "trendbar": rows, "hasMore": False})

    def view(self, pos: dict) -> dict:
        return {"positionId": str(pos["positionId"]), "positionStatus": "POSITION_STATUS_OPEN", "swap": 0,
                "price": pos["price"], "stopLoss": pos.get("stopLoss"), "takeProfit": pos.get("takeProfit"),
                "tradeData": {"symbolId": pos["symbolId"], "volume": str(pos["volume"]),
                              "tradeSide": "BUY" if pos["side"] == 1 else "SELL", "label": pos.get("label"),
                              "comment": pos.get("comment")}}

    def exec_event(self, cid, kind, pos: dict | None = None, deal: dict | None = None, order: dict | None = None):
        payload = {"executionType": kind}
        if pos is not None:
            payload["position"] = self.view(pos)
        if deal is not None:
            payload["deal"] = deal
        if order is not None:
            payload["order"] = order
        return {"clientMsgId": cid if self.exec_with_cid else None, "payloadType": 2126, "payload": payload}

    def new_order(self, p: dict, cid, err) -> list[dict]:
        if self.silent_orders:
            return []
        if self.reject_orders:
            return [{"clientMsgId": cid, "payloadType": 2132,
                     "payload": {"errorCode": self.reject_orders, "description": "rejected by fake"}}]
        bid, ask = self.quotes[p["symbolId"]]
        buy = p["tradeSide"] == 1
        price = ask if buy else bid
        sign = 1 if buy else -1
        pid, oid = next(self.ids), next(self.ids)
        pos = {"positionId": pid, "symbolId": p["symbolId"], "side": p["tradeSide"], "volume": p["volume"],
               "price": price, "label": p.get("label"), "comment": p.get("comment"),
               "stopLoss": round(price - sign * p["relativeStopLoss"] / SPOT_SCALE, 5)
               if p.get("relativeStopLoss") else None,
               "takeProfit": round(price + sign * p["relativeTakeProfit"] / SPOT_SCALE, 5)
               if p.get("relativeTakeProfit") else None}
        self.positions[pid] = pos
        order = {"orderId": str(oid), "positionId": pid,
                 "tradeData": {"symbolId": p["symbolId"], "volume": p["volume"], "tradeSide": p["tradeSide"],
                               "label": p.get("label"), "comment": p.get("comment")}}
        deal = {"dealId": next(self.ids), "orderId": oid, "positionId": pid, "volume": p["volume"],
                "filledVolume": p["volume"], "symbolId": p["symbolId"], "executionPrice": price,
                "tradeSide": p["tradeSide"], "dealStatus": "FILLED", "executionTimestamp": self.now_ms}
        self.deals.append(deal)
        return [self.exec_event(cid, "ORDER_ACCEPTED", order=order),
                self.exec_event(cid, 3, pos=pos, deal=deal, order=order)]  # numeric enum on purpose

    def close(self, p: dict, cid) -> list[dict]:
        pos = self.positions.get(p["positionId"])
        if pos is None:
            return [{"clientMsgId": cid, "payloadType": 2132, "payload": {"errorCode": "POSITION_NOT_FOUND"}}]
        bid, ask = self.quotes[pos["symbolId"]]
        px = bid if pos["side"] == 1 else ask
        deal = {"dealId": next(self.ids), "orderId": next(self.ids), "positionId": pos["positionId"],
                "volume": p["volume"], "filledVolume": p["volume"], "symbolId": pos["symbolId"],
                "executionPrice": px, "tradeSide": 2 if pos["side"] == 1 else 1, "dealStatus": "FILLED",
                "executionTimestamp": self.now_ms,
                "closePositionDetail": {"entryPrice": pos["price"], "grossProfit": 0, "swap": 0, "commission": 0,
                                        "balance": 0, "closedVolume": p["volume"]}}
        self.deals.append(deal)
        pos["volume"] -= p["volume"]
        events = [self.exec_event(cid, "ORDER_ACCEPTED", pos=pos), self.exec_event(cid, "ORDER_FILLED", pos=pos,
                                                                                  deal=deal)]
        if pos["volume"] <= 0:
            del self.positions[pos["positionId"]]
        return events

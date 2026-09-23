"""Paper venue: the cTrader trading semantics simulated (relative SL/TP points, cents, amend both legs,
close by volume). Fills at ask (buy) / bid (sell); SL/TP tracked against M1 bars, SL first when a bar hits both.
Same TradeManager, same guards, same messages — only this object differs from live (broker-execution §6)."""

from __future__ import annotations

import itertools
import json

from mapex.config import DEFAULT_PIP_DIGITS, Settings


class PaperVenue:
    mode = "paper"

    def __init__(self, store, settings: Settings, quote_of, clock):
        self.store, self.s = store, settings
        self.quote_of = quote_of  # symbol -> Quote | None
        self.clock = clock
        state = store.get_json("paper_state", {"positions": {}, "deals": [], "next": 5000})
        self.pos: dict[str, dict] = state["positions"]
        self.deal_log: list[dict] = state["deals"]
        self.ids = itertools.count(state["next"])

    def _save(self) -> None:
        nxt = next(self.ids)
        self.ids = itertools.count(nxt)
        self.store.put_json("paper_state", {"positions": self.pos, "deals": self.deal_log[-500:], "next": nxt})

    def digits(self, symbol: str) -> int:
        return int(self.s.price_digits.get(symbol, DEFAULT_PIP_DIGITS.get(symbol, 2)))

    def supports_range(self) -> bool:
        return True

    async def create_order(self, symbol: str, args: dict) -> dict:
        q = self.quote_of(symbol)
        if q is None:
            from mapex.ctrader.client import ToolError
            raise ToolError("paper: no live quote")
        buy = args["tradeSide"] == "BUY"
        fill = q.ask if buy else q.bid
        unit = 10 ** -self.digits(symbol)
        sgn = 1 if buy else -1
        pid = str(next(self.ids))
        self.pos[pid] = {"position_id": pid, "symbol": symbol, "side": "buy" if buy else "sell",
                         "volume": int(args["volume"]), "entry": fill,
                         "sl": round(fill - sgn * args["relativeStopLoss"] * unit, 8),
                         "tp": round(fill + sgn * args["relativeTakeProfit"] * unit, 8),
                         "label": args.get("label", ""), "comment": args.get("comment", "")}
        self._save()
        return {"order_id": str(next(self.ids)), "position_id": pid, "fill": fill, "volume": int(args["volume"])}

    async def amend_position(self, position_id: str, stop_loss: float, take_profit: float) -> dict:
        if stop_loss is None or take_profit is None:
            raise ValueError("amend_position requires both stopLoss and takeProfit")
        p = self.pos[str(position_id)]
        p["sl"], p["tp"] = float(stop_loss), float(take_profit)
        self._save()
        return {"position": dict(p)}

    def _close(self, pid: str, volume: int, price: float) -> None:
        p = self.pos[pid]
        vol = min(int(volume), p["volume"])
        self.deal_log.append({"position_id": pid, "price": price, "volume": vol, "closing": True,
                              "symbol": p["symbol"], "t": int(self.clock())})
        p["volume"] -= vol
        if p["volume"] <= 0:
            del self.pos[pid]
        self._save()

    async def close_position(self, position_id: str, volume: int) -> dict:
        pid = str(position_id)
        p = self.pos[pid]
        q = self.quote_of(p["symbol"])
        price = (q.bid if p["side"] == "buy" else q.ask) if q else p["entry"]
        self._close(pid, volume, price)
        return {"closed": True}

    async def positions(self) -> list[dict]:
        return [dict(p) for p in self.pos.values()]

    async def deals_for(self, position_id: str, since: float) -> list[dict]:
        return [dict(d) for d in self.deal_log if d["position_id"] == str(position_id)]

    def on_bar(self, symbol: str, bar) -> list[str]:
        """Simulated SL/TP against a closed M1 bar; same-bar SL and TP => SL first."""
        hit = []
        for pid, p in list(self.pos.items()):
            if p["symbol"] != symbol:
                continue
            buy = p["side"] == "buy"
            sl_hit = p["sl"] is not None and (bar.l <= p["sl"] if buy else bar.h >= p["sl"])
            tp_hit = p["tp"] is not None and (bar.h >= p["tp"] if buy else bar.l <= p["tp"])
            if sl_hit:
                self._close(pid, p["volume"], p["sl"])
                hit.append(pid)
            elif tp_hit:
                self._close(pid, p["volume"], p["tp"])
                hit.append(pid)
        return hit


def paper_state_dump(store) -> str:
    return json.dumps(store.get_json("paper_state", {}), indent=1)

"""Replay backtester: GEM1 + GEM2 over historical candles with simulated fills, through the exact live code path
(pipeline + TradeManager + PaperVenue + guards). Every reader receives `now`; nothing after it is visible."""

from __future__ import annotations

import asyncio
import bisect
from dataclasses import dataclass, field

from mapex import guards
from mapex.config import Settings
from mapex.core.primitives import Bar, closed_bars
from mapex.core.timeutil import TF_SECONDS, market_open, ny
from mapex.ctrader.broker import TradeManager
from mapex.ctrader.client import Quote
from mapex.ctrader.paper import PaperVenue
from mapex.data.candles import HISTORY
from mapex.pipeline import run_executor, run_mapper
from mapex.store import Store

REPLAY_SPREAD = {"XAUUSD": 0.20, "BTCUSD": 15.0}
WARMUP_DAYS = {"M1": 1, "M5": 2, "M15": 16, "H1": 14, "H4": 45, "D1": 370, "W1": 430, "MN1": 760}


@dataclass
class Report:
    symbol: str
    days: int
    start: int
    end: int
    maps: int = 0
    valid_maps: int = 0
    trades: list[dict] = field(default_factory=list)
    decisions: dict[str, int] = field(default_factory=dict)

    @property
    def closed(self) -> list[dict]:
        return [t for t in self.trades if t["result_r"] is not None]

    @property
    def wins(self) -> int:
        return sum(1 for t in self.closed if t["result_r"] > 0)

    @property
    def win_rate(self) -> float:
        return self.wins / len(self.closed) if self.closed else 0.0

    @property
    def total_r(self) -> float:
        return sum(t["result_r"] for t in self.closed)

    def text(self) -> str:
        lines = [f"🔁 <b>REPLAY {self.symbol} · {self.days} ditë</b>",
                 f"Periudha: {ny(self.start):%Y-%m-%d} → {ny(self.end):%Y-%m-%d} (NY)",
                 f"Harta: {self.valid_maps}/{self.maps} të vlefshme",
                 f"Tregti: {len(self.trades)} · Fitime: {self.wins} · Humbje: {len(self.closed) - self.wins}",
                 f"Win rate: {self.win_rate * 100:.0f}% · Rezultati: {self.total_r:+.2f}R"]
        for t in self.trades[-10:]:
            r = "?" if t["result_r"] is None else f"{t['result_r']:+.2f}R"
            lines.append(f"• {ny(t['opened_at']):%m-%d %H:%M} {t['side'].upper()} @ {t['entry_fill']} → {r}")
        if not self.trades:
            lines.append("Asnjë tregti 100/100 në këtë periudhë (normale: MAPEX është shumë selektiv).")
        lines.append("Vetëm raport — asnjë urdhër real.")
        return "\n".join(lines)


def _upto(bars: list[Bar], tf: str, now: float, n: int) -> list[Bar]:
    """Closed bars at `now`, at most n of them (the engines cut again: defence in depth, A1)."""
    if tf == "MN1":
        return closed_bars(bars, tf, now)[-n:]
    cut = bisect.bisect_right(bars, now - TF_SECONDS[tf] - 2, key=lambda b: b.t)
    return bars[max(0, cut - n):cut]


async def run_replay(symbol: str, bars: dict[str, list[Bar]], start: int, end: int, s: Settings) -> Report:
    store = Store(":memory:")
    days = max(1, round((end - start) / 86400))
    rep = Report(symbol, days, start, end)
    clock = [float(start)]
    quote: dict[str, Quote | None] = {symbol: None}
    venue = PaperVenue(store, s, lambda sym: quote.get(sym), lambda: clock[0])

    async def no_sleep(_s):
        return None

    tm = TradeManager(store, s, venue, clock=lambda: clock[0], sleep=no_sleep, account="replay")
    m1 = bars["M1"]
    i0 = bisect.bisect_left(m1, start, key=lambda b: b.t)
    spread = REPLAY_SPREAD.get(symbol, 0.0)
    next_map = 0
    for bar in m1[i0:]:
        if bar.t >= end:
            break
        now = bar.t + 60 + 3
        clock[0] = now
        venue.on_bar(symbol, bar)  # simulated SL/TP on the bar that just closed
        q = Quote(bar.c, round(bar.c + spread, 6), now - 3, now)
        quote[symbol] = q
        if now >= next_map:  # every H1 close + 60 s (and at the start)
            h1_close = (int(now) // 3600) * 3600
            sl = {tf: _upto(bars.get(tf, []), tf, now, HISTORY[tf]) for tf in ("MN1", "W1", "D1", "H4", "H1", "M15")}
            res = run_mapper(store, s, symbol, sl, bar.c, now)
            rep.maps += 1
            rep.valid_maps += int(res.valid)
            next_map = h1_close + 3600 + 60
        sl = {tf: _upto(bars.get(tf, []), tf, now, 300 if tf != "M15" else 400) for tf in ("M1", "M5", "M15")}
        sl["D1"] = _upto(bars.get("D1", []), "D1", now, 60)
        res = run_executor(store, s, symbol, now, sl, q.bid, q.ask, market_open(symbol, now))
        for d in res.decisions:
            rep.decisions[d.output] = rep.decisions.get(d.output, 0) + 1
        if res.rerun_mapper:
            next_map = 0
        if res.plan:
            await tm.execute(res.plan, q)
        await tm.manage({symbol: q})
        guards.daily_roll(store, now)
    # positions still open at the end are marked at the last close
    clock[0] = float(end)
    for p in await venue.positions():
        await venue.close_position(p["position_id"], p["volume"])
    await tm.manage({})
    clock[0] = float(end) + 3600
    await tm.manage({})
    rep.trades = [dict(r) for r in store.all("SELECT * FROM trades WHERE state != 'FAILED' ORDER BY opened_at")]
    return rep


async def fetch_history(client, symbol: str, days: int, end: int) -> dict[str, list[Bar]]:
    """Every timeframe from the broker (never aggregated), chunked by the client into <= 720 h windows."""
    out = {}
    for tf, warm in WARMUP_DAYS.items():
        span = (days if tf in ("M1", "M5") else 0) + warm
        out[tf] = await client.trendbars(symbol, tf, int(end - span * 86400), int(end))
        await asyncio.sleep(0)
    return out

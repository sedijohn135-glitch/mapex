"""The Live Validator's `Tape` fixture (live-validator- tests/synth.py) on MAPEX's validator context."""

from __future__ import annotations

from mapex.core.timeutil import TF_SECONDS
from mapex.validator.context import MarketContext, SymbolView
from mapex.validator.market import Candle, session_levels
from tests.helpers import ny_ts

M1 = 60
SYMBOLS = {"XAUUSD": SymbolView(0.01, 0.80), "BTCUSD": SymbolView(0.01, 60.0)}


def ts(text: str) -> float:
    day, hhmm = text.split(" ")
    y, m, d = (int(x) for x in day.split("-"))
    hh, mm = (int(x) for x in hhmm.split(":"))
    return ny_ts(y, m, d, hh, mm)


class Tape:
    """A tape of M1 candles. Higher timeframes are aggregated from it, never invented."""

    def __init__(self, symbol: str = "XAUUSD", start_ny: str = "2026-09-18 09:00", price: float = 4300.0) -> None:
        self.symbol = symbol
        self.start = ts(start_ny)
        self.price = price
        self.bars: list[Candle] = []

    @property
    def now(self) -> float:
        return self.start + len(self.bars) * M1

    def push(self, o: float, h: float, low: float, c: float) -> Candle:
        bar = Candle(self.now, o, h, low, c)
        self.bars.append(bar)
        self.price = c
        return bar

    def drift(self, count: int, step: float = 0.0, span: float = 0.4) -> None:
        for _ in range(count):
            o = self.price
            c = o + step
            self.push(o, max(o, c) + span, min(o, c) - span, c)

    def sweep(self, low: float, close: float, span: float = 0.2) -> None:
        o = self.price
        self.push(o, max(o, close) + span, min(low, o, close), close)

    def spike(self, high: float, close: float, span: float = 0.2) -> None:
        o = self.price
        self.push(o, max(high, o, close), min(o, close) - span, close)

    def series(self) -> dict[str, list[Candle]]:
        out = {"M1": list(self.bars)}
        for tf in ("M5", "M15", "H1", "H4", "D1", "W1"):
            out[tf] = self._aggregate(TF_SECONDS[tf])
        return out

    def _aggregate(self, seconds: int) -> list[Candle]:
        buckets: dict[float, list[Candle]] = {}
        for bar in self.bars:
            buckets.setdefault(bar.t - (bar.t % seconds), []).append(bar)
        return [Candle(t, g[0].o, max(b.h for b in g), min(b.l for b in g), g[-1].c)
                for t, g in sorted(buckets.items())]

    def context(self, bid: float | None = None, spread: float = 0.2, median: float | None = None, **kwargs
                ) -> MarketContext:
        bid = self.price if bid is None else bid
        bars = self.series()
        ctx = MarketContext(symbol=self.symbol, now_ts=kwargs.pop("now_ts", self.now), sym=SYMBOLS[self.symbol],
                            bars=bars, bid=bid, ask=bid + spread, quote_ts=kwargs.pop("quote_ts", self.now),
                            spread_samples=[spread if median is None else median] * 60, **kwargs)
        ctx.levels = session_levels(bars, ctx.now_ts)
        return ctx

"""The market view the Live Validator's rules read, built from MAPEX's closed candles and live quote."""

from __future__ import annotations

from dataclasses import dataclass, field

from mapex.validator.market import Candle, atr14, median_spread


@dataclass(frozen=True)
class SymbolView:
    tick: float
    max_spread_abs: float


@dataclass
class MarketContext:
    symbol: str
    now_ts: float
    sym: SymbolView
    bars: dict[str, list[Candle]]  # closed candles per timeframe, oldest first
    bid: float | None = None
    ask: float | None = None
    quote_ts: float | None = None
    quote_synthetic: bool = False
    spread_samples: list[float] = field(default_factory=list)
    levels: dict[str, float | None] = field(default_factory=dict)
    data_ok: bool = True

    @property
    def spread(self) -> float | None:
        return None if self.bid is None or self.ask is None else self.ask - self.bid

    @property
    def quote_age(self) -> float:
        return float("inf") if self.quote_ts is None else max(0.0, self.now_ts - self.quote_ts)

    def atr(self, timeframe: str) -> float | None:
        return atr14(self.candles(timeframe))

    def median_spread(self) -> float:
        return median_spread(self.spread_samples, self.sym.max_spread_abs)

    def candles(self, timeframe: str) -> list[Candle]:
        return self.bars.get(timeframe, [])

    def last_closed(self, timeframe: str) -> Candle | None:
        bars = self.candles(timeframe)
        return bars[-1] if bars else None

    def has_candles(self, timeframe: str, minimum: int = 2) -> bool:
        return len(self.candles(timeframe)) >= minimum

    def level(self, name: str) -> float | None:
        return self.levels.get(name)

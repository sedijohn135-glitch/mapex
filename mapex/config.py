"""Environment parsing. The service must start with no secrets at all; problems land in `errors`/`warnings`."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULTS = {
    "CONTRACT_SIZE": {"XAUUSD": 100, "BTCUSD": 1},
    "MAX_SPREAD": {"XAUUSD": 0.80, "BTCUSD": 60},
    "MAX_SLIPPAGE_POINTS": {"XAUUSD": 300, "BTCUSD": 3000},
    "SL_SAFETY_BUFFER": {"XAUUSD": 0.30, "BTCUSD": 15},
    "PRICE_BANDS": {"XAUUSD": [1500, 14000], "BTCUSD": [25000, 240000]},
    "PRICE_DIGITS": {},  # override of pipDigits resolution
    "DISPLAY_DECIMALS": {"XAUUSD": 2, "BTCUSD": 2},
}
# precision-table defaults (spotware/ctrader-skills assets/symbol_precision_table.json)
DEFAULT_PIP_DIGITS = {"XAUUSD": 3, "BTCUSD": 2}
CHAIN_GATES = (65, 50, 40)  # GEM1 Step 7 1C
LIVE_LABEL = "MAPEX"


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    ctrader_config: str = ""
    ctrader_url: str = ""
    ctrader_token: str = ""
    telegram_token: str = ""
    telegram_chat_id: str = ""
    trading_mode: str = "paper"
    confirm_live: bool = False
    symbols: list[str] = field(default_factory=lambda: ["XAUUSD", "BTCUSD"])
    lots: dict[str, float] = field(default_factory=dict)
    max_lot: float = 1.0
    contract_size: dict[str, float] = field(default_factory=lambda: dict(DEFAULTS["CONTRACT_SIZE"]))
    max_open_total: int = 2
    max_open_per_symbol: int = 1
    max_trades_per_day: int = 3
    max_consecutive_losses: int = 3
    daily_loss_limit_r: float = 3.0
    tp1_close_pct: float = 50.0
    max_spread: dict[str, float] = field(default_factory=lambda: dict(DEFAULTS["MAX_SPREAD"]))
    max_slippage_points: dict[str, int] = field(default_factory=lambda: dict(DEFAULTS["MAX_SLIPPAGE_POINTS"]))
    sl_safety_buffer: dict[str, float] = field(default_factory=lambda: dict(DEFAULTS["SL_SAFETY_BUFFER"]))
    price_digits: dict[str, int] = field(default_factory=dict)
    price_bands: dict[str, list[float]] = field(default_factory=lambda: dict(DEFAULTS["PRICE_BANDS"]))
    display_decimals: dict[str, int] = field(default_factory=lambda: dict(DEFAULTS["DISPLAY_DECIMALS"]))
    notify_exits: bool = False
    chain_gates: tuple[int, int, int] = CHAIN_GATES
    map_max_age_h: float = 6.0
    no_entry_before_close_min: float = 30.0
    data_dir: Path = Path("./data")
    volume_mounted: bool = False
    log_level: str = "INFO"
    port: int = 8080
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def tick(self, symbol: str) -> float:
        return 10 ** -self.display_decimals.get(symbol, 2)

    def tradable(self, symbol: str) -> bool:
        """A symbol trades only when the owner funded it with a LOT_<SYMBOL> variable."""
        return symbol in self.lots

    @property
    def db_path(self) -> Path:
        return self.data_dir / "mapex.db"


def _json_map(env: dict, key: str, default: dict, errors: list[str]) -> dict:
    raw = env.get(key)
    if not raw:
        return dict(default)
    try:
        val = json.loads(raw)
        if not isinstance(val, dict):
            raise ValueError("not an object")
        merged = dict(default)
        merged.update({k.upper(): v for k, v in val.items()})
        return merged
    except ValueError as exc:
        errors.append(f"{key}: invalid JSON ({exc}); default used")
        return dict(default)


def _num(env: dict, key: str, default, cast, errors: list[str]):
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return cast(raw.strip())
    except ValueError:
        errors.append(f"{key}: '{raw}' is not a number; default {default} used")
        return default


def load(env: dict | None = None) -> Settings:
    env = dict(os.environ if env is None else env)
    s = Settings()
    errs, warns = s.errors, s.warnings
    s.ctrader_config = env.get("CTRADER_MCP_CONFIG", "").strip()
    s.ctrader_url = env.get("CTRADER_MCP_URL", "").strip()
    s.ctrader_token = env.get("CTRADER_MCP_TOKEN", "").strip()
    s.telegram_token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
    s.telegram_chat_id = env.get("TELEGRAM_CHAT_ID", "").strip()
    mode = env.get("TRADING_MODE", "paper").strip().lower() or "paper"
    if mode not in {"paper", "live"}:
        errs.append(f"TRADING_MODE '{mode}' unknown; paper used")
        mode = "paper"
    s.trading_mode = mode
    s.confirm_live = env.get("CONFIRM_LIVE_ACCOUNT", "NO").strip().upper() == "YES"
    syms = [x.strip().upper() for x in env.get("SYMBOLS", "XAUUSD,BTCUSD").split(",") if x.strip()]
    s.symbols = syms or ["XAUUSD", "BTCUSD"]
    s.max_lot = _num(env, "MAX_LOT", 1.0, float, errs)
    for sym in s.symbols:
        raw = env.get(f"LOT_{sym}", "").strip()
        if not raw:
            warns.append(f"LOT_{sym} missing: {sym} will never trade")
            continue
        try:
            lot = float(raw)
        except ValueError:
            errs.append(f"LOT_{sym}='{raw}' is not a number: {sym} will never trade")
            continue
        if not 0 < lot <= s.max_lot:
            errs.append(f"LOT_{sym}={lot} outside (0, MAX_LOT={s.max_lot}]: refused, {sym} will never trade")
            continue
        s.lots[sym] = lot
    s.contract_size = _json_map(env, "CONTRACT_SIZE", DEFAULTS["CONTRACT_SIZE"], errs)
    s.max_spread = _json_map(env, "MAX_SPREAD", DEFAULTS["MAX_SPREAD"], errs)
    s.max_slippage_points = _json_map(env, "MAX_SLIPPAGE_POINTS", DEFAULTS["MAX_SLIPPAGE_POINTS"], errs)
    s.sl_safety_buffer = _json_map(env, "SL_SAFETY_BUFFER", DEFAULTS["SL_SAFETY_BUFFER"], errs)
    s.price_digits = _json_map(env, "PRICE_DIGITS", DEFAULTS["PRICE_DIGITS"], errs)
    s.price_bands = _json_map(env, "PRICE_BANDS", DEFAULTS["PRICE_BANDS"], errs)
    s.max_open_total = _num(env, "MAX_OPEN_TOTAL", 2, int, errs)
    s.max_open_per_symbol = _num(env, "MAX_OPEN_PER_SYMBOL", 1, int, errs)
    s.max_trades_per_day = _num(env, "MAX_TRADES_PER_DAY", 3, int, errs)
    s.max_consecutive_losses = _num(env, "MAX_CONSECUTIVE_LOSSES", 3, int, errs)
    s.daily_loss_limit_r = _num(env, "DAILY_LOSS_LIMIT_R", 3.0, float, errs)
    pct = _num(env, "TP1_CLOSE_PCT", 50.0, float, errs)
    if not 50 <= pct <= 80:  # GEM2 Layer 5 Pay-the-Trader: 50-80 %
        errs.append(f"TP1_CLOSE_PCT={pct} outside GEM2 range 50-80; 50 used")
        pct = 50.0
    s.tp1_close_pct = pct
    s.notify_exits = _truthy(env.get("NOTIFY_EXITS"))
    gates = env.get("MAPPER_MIN_CHAIN_LPS", "").strip()
    if gates:
        try:
            a, b, c = (int(x) for x in gates.replace(" ", "").split(","))
            s.chain_gates = (a, b, c)
            warns.append(f"MAPPER_MIN_CHAIN_LPS override active: {a}/{b}/{c} (GEM1 default 65/50/40)")
        except ValueError:
            errs.append("MAPPER_MIN_CHAIN_LPS must be 'A,B,C' integers; GEM1 65,50,40 used")
    s.map_max_age_h = _num(env, "MAP_MAX_AGE_H", 6.0, float, errs)
    s.no_entry_before_close_min = _num(env, "NO_ENTRY_BEFORE_CLOSE_MIN", 30.0, float, errs)
    data_dir = env.get("DATA_DIR") or env.get("RAILWAY_VOLUME_MOUNT_PATH")
    s.volume_mounted = bool(env.get("RAILWAY_VOLUME_MOUNT_PATH") or env.get("DATA_DIR"))
    s.data_dir = Path(data_dir or "./data")
    if not s.volume_mounted:
        warns.append("No Railway volume: trade history and duplicate protection are lost on every redeploy")
    s.log_level = env.get("LOG_LEVEL", "INFO").upper()
    s.port = _num(env, "PORT", 8080, int, errs)
    if not (s.ctrader_config or (s.ctrader_url and s.ctrader_token) or s.ctrader_token):
        warns.append("CTRADER_MCP_CONFIG missing: no market data until it is set")
    if not (s.telegram_token and s.telegram_chat_id):
        warns.append("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing: no notifications")
    for sym in s.symbols:
        if sym not in s.contract_size:
            errs.append(f"CONTRACT_SIZE has no entry for {sym}: {sym} will never trade")
            s.lots.pop(sym, None)
    return s


def volume_cents(lots: float, contract_size: float) -> int:
    """Remote volume = integer cents of base asset (broker-execution §2)."""
    return round(lots * contract_size * 100)


def cents_to_lots(cents: int, contract_size: float) -> float:
    return cents / 100 / contract_size


def lot_mapping_line(sym: str, lots: float, contract_size: float) -> str:
    cents = volume_cents(lots, contract_size)
    return f"{sym} {lots:.2f} lot = {cents} cents = {cents / 100:g} units"

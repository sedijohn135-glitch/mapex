"""Unit conversions for the cTrader Remote MCP — the only place pipettes, points and cents are converted.

Market data (spot, trendbars) = integer pipettes; order DTO prices = display floats (Q-K19);
relative SL/TP = positive integer points; volume = integer cents of base asset (broker-execution §2).
"""

from __future__ import annotations

import base64
import json
import math
import re
from typing import NewType

Pipettes = NewType("Pipettes", int)
Display = NewType("Display", float)
Points = NewType("Points", int)
Cents = NewType("Cents", int)

DEFAULT_URL = "https://mcp.ctrader.com/trading/mcp"


class DecodeError(ValueError):
    pass


def to_display(raw: int | float, digits: int) -> Display:
    return Display(raw / 10**digits)


def to_points(distance: float, digits: int) -> Points:
    pts = round(abs(distance) * 10**digits)
    if pts <= 0:
        raise DecodeError(f"relative distance {distance} rounds to {pts} points")
    return Points(pts)


def price_field(value: float, display_decimals: int, band: list[float]) -> Display:
    """Order DTO price: a display float inside the symbol's sanity band (a pipettes leak never is, Q-K19)."""
    v = round(float(value), display_decimals)
    if not in_band(v, band):
        raise DecodeError(f"price {value} outside sanity band {band} (pipettes leak? Q-K19)")
    return Display(v)


def in_band(price: float, band: list[float] | tuple[float, float]) -> bool:
    return band[0] <= price <= band[1]


def calibrate_digits(raw_bid: int, candidates: list[int | None], band: list[float]) -> int:
    """First resolved digits (metadata -> PRICE_DIGITS -> precision table) that decodes the live bid inside the
    sanity band. Never guesses: if none fits, raise."""
    for d in candidates:
        if d is None:
            continue
        if in_band(raw_bid / 10**d, band):
            return int(d)
    raise DecodeError(f"no digits in {candidates} decode bid {raw_bid} into band {band}")


def decode_position_price(value, digits: int, band: list[float]) -> float | None:
    """Position/deal prices: display floats per Q-K19, pipettes per the Remote doc. Accept whichever lands in the
    sanity band; anything else is None (treated as missing)."""
    if value is None:
        return None
    v = float(value)
    if v == 0:
        return None
    if in_band(v, band):
        return v
    if in_band(v / 10**digits, band):
        return v / 10**digits
    return None


def check_volume(lots: float | None, contract_size: float, max_lot: float) -> Cents:
    """Volume checks before every order (broker-execution §2)."""
    if lots is None:
        raise DecodeError("no LOT_<SYMBOL> for this symbol")
    if not 0 < lots <= max_lot:
        raise DecodeError(f"lot {lots} outside (0, {max_lot}]")
    cents = round(lots * contract_size * 100)
    if cents < 1:
        raise DecodeError(f"lot {lots} x contract {contract_size} gives {cents} cents")
    if abs(cents / 100 / contract_size - lots) > 1e-9:
        raise DecodeError(f"lot {lots} is not representable in whole cents")
    return Cents(cents)


def round_volume_step(cents: int, step: int = 1) -> int:
    step = max(1, int(step))
    return max(step, (cents // step) * step)


# ---------------------------------------------------------------- credentials

_TOKEN_RE = re.compile(r"(eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-.]+|[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-.]+)")


def parse_mcp_config(raw: str) -> tuple[str, str]:
    """Accept the cTrader Web fragment (no outer braces), a full JSON object (any nesting), a bare token or
    'Bearer <token>'. Returns (url, token)."""
    text = (raw or "").strip().strip("`").strip()
    if not text:
        raise DecodeError("empty configuration")
    obj = None
    for candidate in (text, "{" + text.rstrip(",") + "}"):
        try:
            obj = json.loads(candidate)
            break
        except ValueError:
            continue
    if isinstance(obj, dict):
        url, token = _walk(obj)
        if token:
            return url or DEFAULT_URL, token
    if isinstance(obj, str):
        text = obj
    m = re.match(r"^(?:Bearer\s+)?(\S+)$", text, re.I)
    if m and "." in m.group(1) and not m.group(1).startswith("{"):
        return DEFAULT_URL, m.group(1)
    urls = re.findall(r"https://[^\s\"',}]+", text)
    tok = _TOKEN_RE.search(text.split("Bearer", 1)[-1])
    if tok:
        return (urls[0] if urls else DEFAULT_URL), tok.group(1)
    raise DecodeError("no token found in configuration")


def _walk(obj) -> tuple[str | None, str | None]:
    url = token = None
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() == "url" and isinstance(v, str):
                url = v
            elif k.lower() == "authorization" and isinstance(v, str):
                token = re.sub(r"^Bearer\s+", "", v.strip(), flags=re.I)
            elif k.lower() in {"token", "access_token"} and isinstance(v, str):
                token = v.strip()
            else:
                u, t = _walk(v)
                url, token = url or u, token or t
    elif isinstance(obj, list):
        for v in obj:
            u, t = _walk(v)
            url, token = url or u, token or t
    return url, token


def token_claims(token: str) -> dict:
    """Decode (WITHOUT verifying) the first dot-separated segment, only to display demo/live."""
    try:
        seg = token.split(".")[0]
        seg += "=" * (-len(seg) % 4)
        data = json.loads(base64.urlsafe_b64decode(seg))
        return data if isinstance(data, dict) else {}
    except (ValueError, UnicodeDecodeError):
        return {}


def token_environment(token: str) -> str:
    """'demo' | 'live' | 'unknown' from the token claims (environment / plant)."""
    claims = token_claims(token)
    for key in ("environment", "env", "plant"):
        val = str(claims.get(key, "")).lower()
        if "demo" in val:
            return "demo"
        if "live" in val or val == "real":
            return "live"
    return "unknown"


def mask(token: str | None) -> str:
    if not token:
        return "—"
    return f"{token[:4]}…{token[-4:]}" if len(token) > 12 else "…"


def ceil_ticks(x: float, tick: float) -> float:
    return math.ceil(round(x / tick, 9)) * tick

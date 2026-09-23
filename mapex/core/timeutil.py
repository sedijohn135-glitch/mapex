"""New York time, sessions, killzones, macro windows and market hours (shared-primitives §2)."""

from __future__ import annotations

import calendar
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
CET = ZoneInfo("Europe/Tirane")

TF_SECONDS = {
    "M1": 60,
    "M5": 300,
    "M15": 900,
    "M30": 1800,
    "H1": 3600,
    "H4": 14400,
    "D1": 86400,
    "W1": 604800,
    "MN1": 2_678_400,  # upper bound; real month length via bar_close()
}
# The only periods the Remote server accepts (Q-R1).
PERIODS = {"M1": "M_1", "M5": "M_5", "M15": "M_15", "M30": "M_30", "H1": "H_1", "H4": "H_4", "D1": "D_1",
           "W1": "W_1", "MN1": "MN_1"}
CLOSED_GRACE_S = 2

# GEM1 Step 1 session ranges (NY local, half-open [start, end) on bar close time).
SESSION_RANGES = {
    "Asia": (time(19, 0), time(21, 0)),
    "London": (time(2, 0), time(5, 0)),
    "NY AM": (time(7, 0), time(10, 0)),
    "NY Lunch": (time(12, 0), time(13, 30)),
    "NY PM": (time(13, 30), time(16, 0)),
}
# GEM1 current_session labels.
SESSION_LABELS = (("Asia", time(19, 0), time(2, 0)), ("London", time(2, 0), time(7, 0)),
                  ("New York", time(7, 0), time(19, 0)))
# GEM2 P2 killzones used as the entry gate.
KILLZONES = {
    "London": (time(2, 0), time(5, 0)),
    "New York": (time(8, 30), time(11, 0)),
    "PM Silver Bullet": (time(14, 0), time(15, 0)),
}
SILVER_BULLET = ((time(10, 0), time(11, 0)), (time(14, 0), time(15, 0)))
CLASSIC_FX_SB = (time(3, 0), time(4, 0))
NY_LUNCH = (time(12, 0), time(13, 30))
LAST_ENTRY = time(15, 50)
LAST_HOUR_MACRO = (time(15, 0), time(16, 0))


def utc(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, UTC)


def ny(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, NY)


def ts_of(dt: datetime) -> int:
    return int(dt.timestamp())


def in_window(t: time, start: time, end: time) -> bool:
    """Half-open [start, end); windows may wrap midnight."""
    if start <= end:
        return start <= t < end
    return t >= start or t < end


def ny_time(ts: float) -> time:
    return ny(ts).time()


def bar_close(t: int, tf: str) -> int:
    """Close time of the bar opened at `t`."""
    if tf == "MN1":
        d = utc(t)
        y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
        day = min(d.day, calendar.monthrange(y, m)[1])
        return ts_of(d.replace(year=y, month=m, day=day))
    return t + TF_SECONDS[tf]


def is_closed(t: int, tf: str, now: float, grace: int = CLOSED_GRACE_S) -> bool:
    return now >= bar_close(t, tf) + grace


def current_session(ts: float) -> str:
    t = ny_time(ts)
    for name, start, end in SESSION_LABELS:
        if in_window(t, start, end):
            return name
    return "New York"


def session_label_window(name: str) -> tuple[time, time]:
    for label, start, end in SESSION_LABELS:
        if label == name:
            return start, end
    raise KeyError(name)


def killzone(ts: float) -> str | None:
    """Name of the GEM2 P2 killzone containing `ts`, never NY Lunch, never after 15:50 NY."""
    t = ny_time(ts)
    if in_window(t, *NY_LUNCH) or t >= LAST_ENTRY and t < time(19, 0):
        return None
    for name, (start, end) in KILLZONES.items():
        if in_window(t, start, end):
            return name
    return None


def in_silver_bullet(ts: float) -> bool:
    t = ny_time(ts)
    return any(in_window(t, s, e) for s, e in SILVER_BULLET)


def in_macro_window(ts: float) -> bool:
    """GEM2 Layer 5 macro windows [HH:50, HH+1:10] and the last hour 15:00-16:00."""
    t = ny_time(ts)
    return t.minute >= 50 or t.minute < 10 or in_window(t, *LAST_HOUR_MACRO)


def ny_midnight(ts: float) -> int:
    d = ny(ts)
    return ts_of(d.replace(hour=0, minute=0, second=0, microsecond=0))


def ny_at(ts: float, hh: int, mm: int = 0, day_offset: int = 0) -> int:
    """Timestamp of HH:MM NY on the NY calendar day of `ts` (+ offset days), DST-safe."""
    d = ny(ts).date() + timedelta(days=day_offset)
    return ts_of(datetime(d.year, d.month, d.day, hh, mm, tzinfo=NY))


def trading_day(ts: float) -> str:
    """NY trading day key; the day rolls at 17:00 NY."""
    return (ny(ts) + timedelta(hours=7)).date().isoformat()


def week_start(ts: float) -> int:
    """Sunday 18:00 NY opening the current trading week."""
    d = ny(ts)
    days_back = (d.weekday() + 1) % 7  # Sunday -> 0
    start = ny_at(ts, 18, 0, -days_back)
    if start > ts:
        start = ny_at(ts, 18, 0, -days_back - 7)
    return start


# Market hours (shared-primitives §2). XAUUSD: closed Fri 17:00 -> Sun 18:00 NY and daily 17:00-18:00.
def market_open(symbol: str, ts: float) -> bool:
    if symbol.upper().startswith("BTC"):
        return True
    d = ny(ts)
    t, wd = d.time(), d.weekday()  # Mon=0 .. Sun=6
    if wd == 5:
        return False
    if wd == 4 and t >= time(17, 0):
        return False
    if wd == 6 and t < time(18, 0):
        return False
    return not in_window(t, time(17, 0), time(18, 0))


def minutes_to_close(symbol: str, ts: float) -> float:
    """Minutes until the next daily close (17:00 NY); infinite for 24/7 symbols."""
    if symbol.upper().startswith("BTC"):
        return float("inf")
    close = ny_at(ts, 17, 0)
    if close <= ts:
        close = ny_at(ts, 17, 0, 1)
    return (close - ts) / 60


def fmt_ny(ts: float) -> str:
    return ny(ts).strftime("%H:%M")


def fmt_cet(ts: float) -> str:
    return datetime.fromtimestamp(ts, CET).strftime("%H:%M CET (Tirana)")

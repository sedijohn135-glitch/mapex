"""Risk guards and circuit breakers. They count trades and R — never money (the lot is the owner's)."""

from __future__ import annotations

from mapex.config import Settings
from mapex.core.timeutil import killzone, market_open, minutes_to_close, ny, trading_day
from mapex.store import Store

OPEN_STATES = ("SENDING", "OPEN", "PARTIAL", "BE", "CLOSING")
KILL_KEY = "kill_switch"
AUTH_KILL_AFTER_S = 15 * 60


def kill_switch(store: Store) -> str | None:
    return store.get(KILL_KEY) or None


def trip(store: Store, reason: str, now: float, alert: bool = True) -> bool:
    """Automatic kill switch (requires /resume). Returns True when it was not already on."""
    from mapex.telegram import msg_auto_stop

    if kill_switch(store):
        return False
    with store.tx():
        store.put(KILL_KEY, reason)
        if alert:
            store.outbox_add(f"kill:{int(now)}:{reason}", msg_auto_stop(reason), critical=True, now=now)
    return True


def resume(store: Store) -> None:
    store.put(KILL_KEY, None)


def day_stats(store: Store, now: float) -> dict:
    day = trading_day(now)
    rows = store.all("SELECT state, result_r, opened_at FROM trades WHERE state != 'FAILED' AND opened_at IS NOT NULL")
    today = [r for r in rows if trading_day(r["opened_at"]) == day]
    loss_r = sum(r["result_r"] for r in today if r["result_r"] is not None and r["result_r"] < 0)
    return {"day": day, "trades": len(today), "loss_r": loss_r,
            "sum_r": sum(r["result_r"] or 0 for r in today),
            "consecutive_losses": int(store.get("consecutive_losses", "0"))}


def open_trades(store: Store, symbol: str | None = None) -> list:
    q = f"SELECT * FROM trades WHERE state IN ({','.join('?' * len(OPEN_STATES))})"
    args: tuple = OPEN_STATES
    if symbol:
        q += " AND symbol=?"
        args += (symbol,)
    return store.all(q, args)


def check_entry(s: Settings, store: Store, plan, quote, now: float, client_auth_ok: bool = True,
                skew_s: float = 0.0) -> list[str]:
    """Every guard, evaluated immediately before sending. Empty list = all pass."""
    why: list[str] = []
    sym = plan.symbol
    buy = plan.side == "buy"
    if kill_switch(store):
        why.append(f"kill_switch: {kill_switch(store)}")
    if not s.tradable(sym):
        why.append(f"no LOT_{sym}")
    if not market_open(sym, now) or minutes_to_close(sym, now) < s.no_entry_before_close_min:
        why.append("market_closed_or_near_close")
    if killzone(now) is None:
        why.append("outside_killzone")
    if quote is None:
        why.append("quote_stale")
    else:
        if quote.spread > s.max_spread.get(sym, float("inf")):
            why.append(f"spread {quote.spread:.2f} > {s.max_spread.get(sym)}")
        entry = quote.ask if buy else quote.bid
        if not ((plan.sl < entry < plan.tp_server) if buy else (plan.tp_server < entry < plan.sl)):
            why.append("sl_tp_not_sane")
    if abs(skew_s) > 30:
        why.append("clock_skew")
    if not client_auth_ok:
        why.append("ctrader_auth")
    opened = open_trades(store)
    if len(opened) >= s.max_open_total:
        why.append("max_open_total")
    mine = [t for t in opened if t["symbol"] == sym]
    if len(mine) >= s.max_open_per_symbol:
        why.append("max_open_per_symbol")
    if any(t["side"] != plan.side for t in mine):
        why.append("opposite_position_open")
    st = day_stats(store, now)
    if st["trades"] >= s.max_trades_per_day:
        why.append("max_trades_per_day")
    if st["consecutive_losses"] >= s.max_consecutive_losses:
        why.append("max_consecutive_losses")
    if st["loss_r"] <= -s.daily_loss_limit_r:
        why.append("daily_loss_limit_r")
    if store.one("SELECT 1 FROM trades WHERE setup_key=?", (plan.setup_key,)):
        why.append("duplicate_setup_key")
    return why


def record_result(s: Settings, store: Store, result_r: float | None, now: float) -> None:
    """Update the breakers after a trade closes; trip the kill switch when a limit is reached."""
    if result_r is None:
        return
    n = int(store.get("consecutive_losses", "0"))
    n = n + 1 if result_r < 0 else (0 if result_r > 0 else n)
    store.put("consecutive_losses", str(n))
    if n >= s.max_consecutive_losses:
        trip(store, f"{n} humbje radhazi", now)
    if day_stats(store, now)["loss_r"] <= -s.daily_loss_limit_r:
        trip(store, f"limiti ditor −{s.daily_loss_limit_r:g}R", now)


def daily_roll(store: Store, now: float) -> bool:
    """17:05 NY: reset the per-day counters (consecutive losses), write daily stats. Idempotent per day."""
    day = trading_day(now)
    t = ny(now)
    if store.get("last_roll") == day or (t.hour == 17 and t.minute < 5):
        return False
    prev = store.get("last_roll")
    if prev:
        rows = store.all("SELECT result_r, opened_at FROM trades WHERE opened_at IS NOT NULL AND state != 'FAILED'")
        mine = [r for r in rows if trading_day(r["opened_at"]) == prev]
        rs = [r["result_r"] for r in mine if r["result_r"] is not None]
        store.execute("INSERT OR REPLACE INTO daily_stats(day, trades, wins, losses, sum_r, loss_r) "
                      "VALUES(?,?,?,?,?,?)", (prev, len(mine), sum(1 for x in rs if x > 0),
                                              sum(1 for x in rs if x < 0), sum(rs), sum(x for x in rs if x < 0)))
    store.put("consecutive_losses", "0")
    store.put("last_roll", day)
    return True

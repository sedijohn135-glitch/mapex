"""D-77: V13 setups confirmed by the Live Validator's evidence, executed by MAPEX.

The watch follows live-validator- app/engine.py: WATCHING → AT_ZONE → (evidence) → ENTER now, or LIMIT at a better
price; only the stop or TP1 reached before the entry cancel a setup; a zone that breaks resets the evidence. What
changes is the end: instead of telling the owner to enter, the confirmed plan becomes a MAPEX order and goes through
the same guards, stop-loss mandate, lot and duplicate protection as every other entry.
"""

from __future__ import annotations

import json
import random
import string

from mapex.core.timeutil import current_session, fmt_ny
from mapex.executor.engine import Plan as Order
from mapex.telegram import e
from mapex.validator import plan as planning
from mapex.validator.context import MarketContext
from mapex.validator.evidence import evaluate, window, zone_failed
from mapex.validator.setup_model import Setup, UnusableSetup, normalise

WATCHING, AT_ZONE, LIMIT, SENT, DONE = "WATCHING", "AT_ZONE", "LIMIT", "SENT", "DONE"
OPEN = (WATCHING, AT_ZONE, LIMIT)
CHAIN = "V13"  # MAPEX's guards know a validator entry by this chain (no GEM2 kill-zone gate, D-77)


def new_id(symbol: str, now: float, rng: random.Random | None = None) -> str:
    tag = "".join((rng or random.Random()).choice(string.ascii_uppercase + string.digits) for _ in range(4))
    return f"V13-{symbol[:3].upper()}-{fmt_ny(now).replace(':', '')}-{tag}"


class Validator:
    def __init__(self, store, decimals: dict[str, int], rng: random.Random | None = None):
        self.store, self.decimals, self.rng = store, decimals, rng
        self.last_seen: dict[str, tuple[float, float]] = {}

    # ------------------------------------------------------------------ intake
    def submit(self, raw: dict, ctx: MarketContext, now: float) -> dict:
        """Accept anything the Live Validator accepts; only a payload with no usable number fails."""
        try:
            setup = normalise(raw, zone_pad=planning.zone_pad(ctx))
        except UnusableSetup as exc:
            return {"status": "unusable", "reason": str(exc)}
        sid = new_id(setup.symbol, now, self.rng)
        computed = {"touch_ts": None, "plan": None}
        self.store.execute(
            "INSERT INTO vsetups(id, symbol, direction, state, payload, computed, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (sid, setup.symbol, setup.direction, WATCHING, json.dumps(setup.as_dict()), json.dumps(computed),
             int(now), int(now)))
        self.store.outbox_add(f"v13-reg:{sid}", self._registered(sid, setup, ctx.bid), now=now)
        return {"status": "registered", "setup_id": sid, "setup": setup.as_dict(), "notes": setup.notes}

    def cancel(self, sid: str, now: float) -> bool:
        cur = self.store.execute("UPDATE vsetups SET state=?, reason='CANCELLED_BY_OWNER', updated_at=? "
                                 "WHERE id=? AND state IN (?,?,?)", (DONE, int(now), sid, *OPEN))
        return cur.rowcount == 1

    def rows(self, symbol: str | None = None, sid: str | None = None, open_only: bool = False) -> list:
        sql, args = "SELECT * FROM vsetups WHERE 1=1", []
        if symbol:
            sql, args = sql + " AND symbol=?", [*args, symbol]
        if sid:
            sql, args = sql + " AND id=?", [*args, sid]
        if open_only:
            sql, args = sql + " AND state IN (?,?,?)", [*args, *OPEN]
        return self.store.all(sql + " ORDER BY created_at DESC LIMIT 20", tuple(args))

    def has_open(self) -> bool:
        return bool(self.store.one("SELECT 1 FROM vsetups WHERE state IN (?,?,?) LIMIT 1", OPEN))

    # ------------------------------------------------------------------ the watch
    def step(self, symbol: str, ctx: MarketContext, now: float) -> list[tuple[str, Order]]:
        """One pass over the symbol's open setups; returns the orders to send now."""
        orders = []
        if ctx.bid is None:
            return orders
        price = ctx.bid
        for row in self.rows(symbol, open_only=True):
            setup, comp, sid = Setup.from_dict(json.loads(row["payload"])), json.loads(row["computed"]), row["id"]
            low, high = self._travelled(sid, ctx, price, row["created_at"], now)
            hit_stop = low <= setup.stop_loss if setup.is_long else high >= setup.stop_loss
            tp1 = setup.tp1
            hit_tp1 = tp1 is not None and (high >= tp1 if setup.is_long else low <= tp1)
            if hit_stop or hit_tp1:
                why = "CANCELLED_SL_FIRST" if hit_stop else "CANCELLED_TP1_FIRST"
                self._set(sid, DONE, comp, now, reason=why)
                self.store.outbox_add(f"v13-cancel:{sid}", self._cancelled(sid, setup, why), now=now)
                continue
            if row["state"] == WATCHING:
                if low <= setup.zone_high and high >= setup.zone_low:
                    comp["touch_ts"] = now
                    self._set(sid, AT_ZONE, comp, now)
            elif row["state"] == AT_ZONE:
                touch = float(comp.get("touch_ts") or now)
                if zone_failed(setup, ctx, touch):
                    comp["touch_ts"] = None
                    self._set(sid, WATCHING, comp, now)
                    continue
                verdict = evaluate(setup, ctx, touch)
                if verdict.ready:
                    order = self._decide(sid, setup, comp, ctx, verdict, price, touch, now)
                    if order:
                        orders.append((sid, order))
            elif row["state"] == LIMIT:
                built = planning.Plan.from_dict(comp["plan"])
                if price <= built.entry if setup.is_long else price >= built.entry:
                    self._set(sid, SENT, comp, now)
                    orders.append((sid, self._order(sid, setup, built, comp, price, ctx, now)))
        return orders

    def sent(self, sid: str, result: str, now: float) -> None:
        """MAPEX's answer to the order: open → the trade manager owns it; anything else ends the setup."""
        row = self.store.one("SELECT * FROM vsetups WHERE id=?", (sid,))
        if row is None:
            return
        opened = result.startswith("open")
        self.store.execute("UPDATE vsetups SET state=?, reason=?, updated_at=? WHERE id=?",
                           (DONE, "ENTERED" if opened else result[:200], int(now), sid))
        if not opened:
            setup = Setup.from_dict(json.loads(row["payload"]))
            self.store.outbox_add(f"v13-noentry:{sid}", f"⛔ <b>{e(setup.symbol)} V13: MAPEX nuk hyri</b> — "
                                  f"{e(result[:160])}\nID: {e(sid)}", critical=True, now=now)

    # ------------------------------------------------------------------ helpers
    def _decide(self, sid, setup: Setup, comp: dict, ctx: MarketContext, verdict, price: float, touch: float,
                now: float) -> Order | None:
        """ENTER NOW while price is still in a fair place, otherwise a recomputed LIMIT (live-validator _decide)."""
        bars = window(ctx, touch)
        if verdict.late:
            entry, why = planning.limit_price(setup, bars, price, ctx)
            mode = "LIMIT"
        elif planning.in_premium_half(setup, price):
            entry, why = planning.discount_entry(setup, bars, price, ctx)
            mode = "LIMIT"
        else:
            entry, why, mode = price, "çmimi live", "MARKET"
        built = planning.build_plan(setup, mode, entry, verdict.extreme, ctx, notes=list(setup.notes))
        comp.update(plan=built.as_dict(), entry_why=why, signals=verdict.codes, score=verdict.score)
        if mode == "LIMIT":
            self._set(sid, LIMIT, comp, now)
            self.store.outbox_add(f"v13-limit:{sid}", self._limit(sid, setup, built, comp), now=now)
            return None
        self._set(sid, SENT, comp, now)
        return self._order(sid, setup, built, comp, price, ctx, now)

    def _order(self, sid, setup: Setup, built, comp: dict, price: float, ctx: MarketContext, now: float) -> Order:
        targets = list(built.targets) or [price + built.risk if setup.is_long else price - built.risk]
        rr = list(built.rr) + [None] * 3
        side = "buy" if setup.is_long else "sell"
        return Order(
            setup_key=sid, symbol=setup.symbol, side=side, chain=CHAIN, zone_key=f"v13|{sid}",
            decision_price=price, sl=built.stop, tp1=targets[0], tp2=targets[1] if len(targets) > 1 else None,
            tp3=targets[2] if len(targets) > 2 else None, tp_server=targets[-1], risk=built.risk,
            r_tp1=rr[0] or 0.0, r_tp2=rr[1], management_level=built.secure_at, anchor_type="V13 + validator",
            spread=ctx.spread or 0.0, root_type="V13 stop", root_price=setup.stop_loss,
            root_lps=int(comp.get("score") or 0), sweep_count=0, sweep_type="EVIDENCE",
            zone_label=f"V13 {setup.label or 'sniper'} {setup.zone_low:g}–{setup.zone_high:g}",
            session=f"{current_session(now)} {fmt_ny(now)}", model=setup.label or "V13",
            strategy_used=["V13", *comp.get("signals", [])], confirmed_at=int(now),
            format_b={"setup_id": sid, "plan": comp.get("plan"), "entry_why": comp.get("entry_why")})

    def _travelled(self, sid: str, ctx: MarketContext, price: float, created: float, now: float):
        """The range price covered since the last pass: a level crossed between two polls is still crossed."""
        low = high = price
        prev = self.last_seen.get(sid)
        since = max(created, prev[0] if prev else now - 60.0)
        if prev:
            low, high = min(low, prev[1]), max(high, prev[1])
        for c in ctx.candles("M1"):
            if c.t >= since:
                low, high = min(low, c.l), max(high, c.h)
        self.last_seen[sid] = (now, price)
        return low, high

    def _set(self, sid: str, state: str, comp: dict, now: float, reason: str | None = None) -> None:
        self.store.execute("UPDATE vsetups SET state=?, computed=?, reason=COALESCE(?, reason), updated_at=? "
                           "WHERE id=?", (state, json.dumps(comp, default=str), reason, int(now), sid))

    def _f(self, symbol: str, x) -> str:
        return "—" if x is None else f"{x:.{self.decimals.get(symbol, 2)}f}"

    def _side(self, setup: Setup) -> str:
        return "BLERJE" if setup.is_long else "SHITJE"

    def _registered(self, sid: str, setup: Setup, price) -> str:
        f = self._f
        tps = " · ".join(f"TP{i} {f(setup.symbol, t)}" for i, t in enumerate(setup.targets, 1))
        return (f"📝 <b>V13 U REGJISTRUA — {self._side(setup)} {e(setup.symbol)}</b>\n"
                f"Zona: {f(setup.symbol, setup.zone_low)} – {f(setup.symbol, setup.zone_high)} · "
                f"SL: {f(setup.symbol, setup.stop_loss)}\n{tps}\nÇmimi tani: {f(setup.symbol, price)}\n"
                f"MAPEX hyn vetë kur evidenca konfirmon (Live Validator).\nID: {e(sid)}")

    def _limit(self, sid: str, setup: Setup, built, comp: dict) -> str:
        f = self._f
        return (f"⏳ <b>V13 LIMIT — {self._side(setup)} {e(setup.symbol)}</b>\n"
                f"Evidenca: {e(' + '.join(comp.get('signals', [])))} ({comp.get('score')} pikë)\n"
                f"MAPEX hyn kur çmimi arrin {f(setup.symbol, built.entry)} ({e(comp.get('entry_why', ''))}) · "
                f"SL {f(setup.symbol, built.stop)}\nID: {e(sid)}")

    def _cancelled(self, sid: str, setup: Setup, why: str) -> str:
        what = "SL-ja u prek" if why == "CANCELLED_SL_FIRST" else "TP1 u arrit"
        return (f"✖️ <b>V13 U ANULUA — {self._side(setup)} {e(setup.symbol)}</b>\n"
                f"{what} para se të preket zona: pa hyrje.\nID: {e(sid)}")

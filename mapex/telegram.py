"""Telegram: entry notifications + critical alerts only (Albanian, parse_mode=HTML, every dynamic value escaped),
an outbox with dedupe, 4096 split, 429 retry_after, and owner-only commands."""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time

from mapex.core.timeutil import ny

log = logging.getLogger("mapex.telegram")
LIMIT = 4096
API = "https://api.telegram.org"


def e(x) -> str:
    return html.escape(str(x), quote=False)


def fp(x: float | None, d: int = 2) -> str:
    return "—" if x is None else f"{x:.{d}f}"


KZ_LABEL = {"London": "London Killzone", "New York": "NY Killzone", "PM Silver Bullet": "PM Silver Bullet"}


def short_model(labels: list[str], fallback: str) -> str:
    for label in labels:
        m = re.search(r"\(([A-Z]{2,5})\)", label)
        if m and m.group(1) != "Turtle":
            return m.group(1)
        if label.startswith("CRT"):
            return "CRT"
    return fallback


def display_id(symbol: str, opened_at: float, n: int) -> str:
    return f"MPX-{ny(opened_at).strftime('%m%d')}-{symbol[:3]}-{n:02d}"


def msg_entry(t: dict, d: int = 2) -> str:
    """t: side, symbol, entry, lots, sl, tp1, tp1_r, tp2, tp2_r, zone_label, root_type, root_price, root_lps,
    sweep_count, sweep_type, scores, session, model, account, paper, id."""
    buy = t["side"] == "buy"
    head = f"{'🟢' if buy else '🔴'} <b>{'📝 PAPER — ' if t['paper'] else ''}MAPEX HYRI — {'BUY' if buy else 'SELL'} " \
           f"{e(t['symbol'])}</b>"
    risk = t["entry"] - t["sl"] if buy else t["sl"] - t["entry"]
    tp = f"TP1: {fp(t['tp1'], d)} ({t['tp1_r']:.1f}R)"
    if t.get("tp2") is not None:
        tp += f" · TP2: {fp(t['tp2'], d)} ({t['tp2_r']:.1f}R)"
    p = t["scores"]
    kz, _, hhmm = t["session"].rpartition(" ")
    lines = [
        head,
        f"Hyrja: <b>{fp(t['entry'], d)}</b> · Lot: <b>{t['lots']:.2f}</b>",
        f"SL: <b>{fp(t['sl'], d)}</b> (−{risk:.{d}f})",
        tp,
        f"Zona: {e(t['zone_label'])}",
        f"Root: {'SSL' if buy else 'BSL'} {e(t['root_type'])} @ {fp(t['root_price'], d)} (LPS {t['root_lps']}) · "
        f"Sweep #{t['sweep_count']} ({'BODY' if t['sweep_type'] == 'BODY_CLOSE' else 'WICK'})",
        f"P1 {p[0]} · P2 {p[1]} · P3 {p[2]} · P4 {p[3]} = {sum(p)}",
        f"Sesioni: {e(KZ_LABEL.get(kz, kz))} {e(hhmm)} · Model: {e(t['model'])}",
        f"Llogaria: {'PAPER' if t['paper'] else e(t['account'].upper())} · ID: {e(t['id'])}",
    ]
    if t["paper"]:
        lines.append("PAPER")
    return "\n".join(lines)


def msg_token_expired(detail: str | None = None) -> str:
    text = ("🔑 <b>TOKENI I CTRADER SKADOI</b> — MAPEX nuk hap tregti.\n"
            "cTrader Web → Settings → Remote MCP → kopjo konfigurimin → dërgoje këtu: /ctrader KONFIGURIMI")
    if detail:
        text += f"\nDetaj: <code>{e(detail[:200])}</code>"
    return text


def msg_auto_stop(reason: str) -> str:
    return (f"🛑 <b>MAPEX U NDAL AUTOMATIKISHT</b>\nArsyeja: {e(reason)}\n"
            "Pozicionet ekzistuese mbeten me SL/TP. Rifillo me /resume.")


def msg_no_sl(symbol: str, position_id) -> str:
    return f"⚠️ <b>POZICION PA SL</b> — {e(symbol)} #{e(position_id)}. Kontrollo manualisht."


def msg_startup(mode: str, account: str, lots: dict[str, float], s, forced_paper: bool, notes: list[str],
                status: dict[str, str] | None = None) -> str:
    syms = " · ".join(f"{e(k)} {v:.2f}" for k, v in lots.items()) or "asnjë"
    text = (f"🚀 MAPEX u ndez · Modaliteti: {mode.upper()} · Llogaria: {e(account)} · Simbolet: {syms}\n"
            f"Mbrojtjet: max {s.max_trades_per_day} tregti/ditë · max {s.max_open_per_symbol} pozicion/simbol · "
            f"ndalim pas {s.max_consecutive_losses} humbjeve")
    if forced_paper:
        text += ("\n⚠️ TRADING_MODE=live, por llogaria është LIVE dhe CONFIRM_LIVE_ACCOUNT≠YES → "
                 "MAPEX po punon në PAPER.")
    for sym, why in (status or {}).items():
        text += f"\n⛔ {e(sym)}: {e(why)}"
    for n in notes:
        text += f"\n⚠️ {e(n)}"
    return text


def msg_tp1(tid: str, pct: float) -> str:
    return f"✅ TP1 — mbyllje {pct:g}% dhe SL në breakeven · {e(tid)}"


def msg_exit(kind: str, r: float | None) -> str:
    rtxt = "R e panjohur" if r is None else f"{r:+.1f}R"
    return f"{'🛑' if kind == 'SL' else '🏁'} Dalje: {e(kind)} ({rtxt})"


def split(text: str, limit: int = LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]
    out, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if cur:
                out.append(cur)
                cur = ""
            out.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) + 1 > limit:
            out.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out


class TelegramBot:
    def __init__(self, token: str, chat_id: str, store, http=None, clock=time.time):
        self.token, self.chat_id = token, str(chat_id)
        self.store = store
        self.http = http
        self.clock = clock
        self.offset = int(store.get("tg_offset", "0") or 0)

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    async def _client(self):
        if self.http is None:
            import httpx2
            self.http = httpx2.AsyncClient(timeout=40.0)
        return self.http

    async def api(self, method: str, payload: dict) -> dict:
        http = await self._client()
        for _ in range(3):
            r = await http.post(f"{API}/bot{self.token}/{method}", json=payload)
            data = r.json() if r.content else {}
            if r.status_code == 429:
                wait = float(data.get("parameters", {}).get("retry_after", 1))
                await asyncio.sleep(min(wait, 30))
                continue
            return data
        return {"ok": False, "description": "rate limited"}

    async def send(self, text: str, chat_id: str | None = None) -> bool:
        ok = True
        for part in split(text):
            data = await self.api("sendMessage", {"chat_id": chat_id or self.chat_id, "text": part,
                                                  "parse_mode": "HTML", "disable_web_page_preview": True})
            if not data.get("ok"):
                log.warning("telegram send failed: %s", str(data.get("description"))[:120])
                ok = False
        return ok

    async def flush_outbox(self) -> int:
        if not (self.enabled and self.chat_id):
            return 0
        sent = 0
        for row in self.store.outbox_pending():
            ok = await self.send(row["text"])
            if ok:
                self.store.execute("UPDATE outbox SET sent_at=?, attempts=attempts+1 WHERE id=?",
                                   (int(self.clock()), row["id"]))
                sent += 1
            else:
                self.store.execute("UPDATE outbox SET attempts=attempts+1 WHERE id=?", (row["id"],))
                break
        return sent

    async def poll_once(self, handler, timeout: int = 25) -> None:
        data = await self.api("getUpdates", {"offset": self.offset, "timeout": timeout,
                                             "allowed_updates": ["message"]})
        for upd in data.get("result", []) or []:
            self.offset = max(self.offset, int(upd["update_id"]) + 1)
            self.store.put("tg_offset", str(self.offset))
            msg = upd.get("message") or {}
            text = (msg.get("text") or "").strip()
            chat = str((msg.get("chat") or {}).get("id", ""))
            if not text.startswith("/"):
                continue
            cmd, _, args = text.partition(" ")
            cmd = cmd.split("@")[0].lower()
            if cmd == "/start":
                await self.send(f"Chat ID yt: <code>{e(chat)}</code>\nVendose te Railway si TELEGRAM_CHAT_ID.", chat)
                continue
            if not self.chat_id or chat != self.chat_id:
                continue  # commands only from the owner's chat
            if cmd == "/ctrader":  # the message carries the account token: delete it immediately
                await self.api("deleteMessage", {"chat_id": chat, "message_id": msg.get("message_id")})
            try:
                reply = await handler(cmd, args.strip())
            except Exception as exc:  # noqa: BLE001 — a command must never kill the bot
                log.exception("command %s failed", cmd)
                reply = f"Gabim: {e(type(exc).__name__)}"
            if reply:
                await self.send(reply)

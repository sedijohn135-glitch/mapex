"""Fake Bot API tests (P10): HTML escaping, 4096 split, 429 retry_after, outbox dedupe, owner-only commands."""

import asyncio
import json

import httpx2

from mapex.store import Store
from mapex.telegram import TelegramBot, msg_entry, msg_startup, split


class FakeBotAPI:
    def __init__(self, updates=None, first_429=False):
        self.sent, self.deleted, self.updates = [], [], list(updates or [])
        self.first_429 = first_429

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content or b"{}")
        if method == "sendMessage":
            if self.first_429:
                self.first_429 = False
                return httpx2.Response(429, json={"ok": False, "parameters": {"retry_after": 0}})
            self.sent.append(body)
            return httpx2.Response(200, json={"ok": True, "result": {}})
        if method == "getUpdates":
            ups, self.updates = self.updates, []
            return httpx2.Response(200, json={"ok": True, "result": ups})
        if method == "deleteMessage":
            self.deleted.append(body)
            return httpx2.Response(200, json={"ok": True})
        return httpx2.Response(404, json={"ok": False})


def bot(api, chat="42"):
    http = httpx2.AsyncClient(transport=httpx2.MockTransport(api.handler))
    return TelegramBot("123:ABC", chat, Store(":memory:"), http=http)


def upd(uid, chat, text, mid=7):
    return {"update_id": uid, "message": {"message_id": mid, "chat": {"id": chat}, "text": text}}


ENTRY = {"side": "buy", "symbol": "XAUUSD", "entry": 5654.77, "lots": 0.10, "sl": 5651.20, "tp1": 5665.90,
         "tp1_r": 3.12, "tp2": 5678.40, "tp2_r": 6.4, "zone_label": "CHAIN_A H4 FVG @ 5653.90 <x>",
         "root_type": "PML", "root_price": 5640.10, "root_lps": 82, "sweep_count": 2, "sweep_type": "BODY_CLOSE",
         "scores": (25, 25, 25, 25), "session": "New York 08:47", "model": "DRO", "account": "live",
         "paper": False, "id": "MPX-0917-XAU-03"}


def test_entry_message_format_and_escaping():
    text = msg_entry(ENTRY)
    assert text.splitlines()[0] == "🟢 <b>MAPEX HYRI — BUY XAUUSD</b>"
    assert "Hyrja: <b>5654.77</b> · Lot: <b>0.10</b>" in text
    assert "SL: <b>5651.20</b> (−3.57)" in text
    assert "TP1: 5665.90 (3.1R) · TP2: 5678.40 (6.4R)" in text
    assert "&lt;x&gt;" in text and "<x>" not in text
    assert "P1 25 · P2 25 · P3 25 · P4 25 = 100" in text
    assert "Sesioni: NY Killzone 08:47 · Model: DRO" in text
    assert text.endswith("Llogaria: LIVE · ID: MPX-0917-XAU-03")
    paper = msg_entry({**ENTRY, "paper": True, "side": "sell"})
    assert paper.startswith("🔴 <b>📝 PAPER — MAPEX HYRI — SELL") and paper.endswith("PAPER")


def test_split_4096():
    parts = split("\n".join("x" * 100 for _ in range(100)))
    assert all(len(p) <= 4096 for p in parts) and len(parts) == 3
    assert split("y" * 9000)[0] == "y" * 4096


def test_send_retries_after_429_and_outbox_dedupe():
    api = FakeBotAPI(first_429=True)
    b = bot(api)
    assert b.store.outbox_add("entry:k1", "hello")
    assert not b.store.outbox_add("entry:k1", "hello again")  # dedupe
    n = asyncio.run(b.flush_outbox())
    assert n == 1 and api.sent[0]["text"] == "hello" and api.sent[0]["parse_mode"] == "HTML"
    assert asyncio.run(b.flush_outbox()) == 0


def test_commands_only_from_owner_and_start_replies_chat_id():
    api = FakeBotAPI([upd(1, 999, "/status"), upd(2, 999, "/start"), upd(3, 42, "/status"),
                      upd(4, 42, "/ctrader SECRET", mid=55)])
    b = bot(api)
    seen = []

    async def handler(cmd, args):
        seen.append((cmd, args))
        return "ok"

    asyncio.run(b.poll_once(handler, timeout=0))
    assert seen == [("/status", ""), ("/ctrader", "SECRET")]  # the stranger's /status was ignored
    assert any("999" in m["text"] and m["chat_id"] == "999" for m in api.sent)  # /start -> chat id
    assert api.deleted == [{"chat_id": "42", "message_id": 55}]  # token message deleted
    assert b.store.get("tg_offset") == "5"


def test_startup_message_forced_paper():
    from mapex import config
    s = config.load({"LOT_XAUUSD": "0.10"})
    text = msg_startup("paper", "live", {"XAUUSD": 0.1}, s, True, ["x"])
    assert text.startswith("🚀 MAPEX u ndez · Modaliteti: PAPER · Llogaria: live · Simbolet: XAUUSD 0.10")
    assert "PAPER" in text.splitlines()[2] and "max 3 tregti/ditë" in text

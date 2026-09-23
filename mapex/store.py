"""SQLite persistence (WAL). A state transition and its outbox row are written in one transaction;
`trades.setup_key` is UNIQUE — the insert itself is the lock against a second order for the same setup."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS maps(
  id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, created_at INTEGER NOT NULL, json TEXT NOT NULL,
  bias TEXT, valid INTEGER NOT NULL, hash TEXT, struct_hash TEXT, reason TEXT);
CREATE INDEX IF NOT EXISTS maps_symbol ON maps(symbol, id);
CREATE TABLE IF NOT EXISTS zones(
  zone_key TEXT PRIMARY KEY, symbol TEXT NOT NULL, map_id INTEGER, chain TEXT, direction TEXT, data TEXT,
  state TEXT NOT NULL DEFAULT 'WATCH', sweep_count INTEGER NOT NULL DEFAULT 0, last_sweep_type TEXT,
  last_sweep_level REAL, displacement_detected INTEGER NOT NULL DEFAULT 0, last_event_at INTEGER,
  monitor_reason TEXT, monitor_hash TEXT, thesis_status TEXT, first_seen INTEGER, archived INTEGER NOT NULL DEFAULT 0,
  ctx TEXT);
CREATE TABLE IF NOT EXISTS trades(
  id INTEGER PRIMARY KEY, setup_key TEXT UNIQUE NOT NULL, symbol TEXT, side TEXT, lots REAL, volume_cents INTEGER,
  entry_planned REAL, entry_fill REAL, sl REAL, tp1 REAL, tp2 REAL, tp3 REAL, tp_server REAL, risk REAL,
  position_id TEXT, order_id TEXT, state TEXT NOT NULL, partial_done INTEGER NOT NULL DEFAULT 0,
  be_done INTEGER NOT NULL DEFAULT 0, opened_at INTEGER, closed_at INTEGER, result_r REAL, mode TEXT,
  zone_key TEXT, chain TEXT, info TEXT, manual INTEGER NOT NULL DEFAULT 0, last_amend_at INTEGER,
  partial_r REAL, volume_open INTEGER);
CREATE TABLE IF NOT EXISTS order_log(
  id INTEGER PRIMARY KEY, ts INTEGER, setup_key TEXT, tool TEXT, request TEXT, response TEXT, ms INTEGER, ok INTEGER);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY, ts INTEGER, symbol TEXT, zone_key TEXT, chain TEXT, state TEXT, output TEXT,
  p1 INTEGER, p2 INTEGER, p3 INTEGER, p4 INTEGER, reason TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS outbox(
  id INTEGER PRIMARY KEY, dedupe TEXT UNIQUE NOT NULL, created_at INTEGER, text TEXT NOT NULL, sent_at INTEGER,
  attempts INTEGER NOT NULL DEFAULT 0, critical INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS daily_stats(
  day TEXT PRIMARY KEY, trades INTEGER NOT NULL DEFAULT 0, wins INTEGER NOT NULL DEFAULT 0,
  losses INTEGER NOT NULL DEFAULT 0, sum_r REAL NOT NULL DEFAULT 0, loss_r REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS lease(id INTEGER PRIMARY KEY CHECK(id = 1), owner TEXT, heartbeat INTEGER);
"""

LEASE_TTL_S = 60


class Store:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self._depth = 0
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)

    # ------------------------------------------------------------- basics
    @contextmanager
    def tx(self):
        with self.lock:
            outer = self._depth == 0
            if outer:
                self.conn.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield self
                self._depth -= 1
                if outer:
                    self.conn.execute("COMMIT")
            except BaseException:
                self._depth -= 1
                if outer:
                    self.conn.execute("ROLLBACK")
                raise

    def execute(self, sql: str, args: tuple | dict = ()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, args)

    def one(self, sql: str, args: tuple | dict = ()) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(sql, args).fetchone()

    def all(self, sql: str, args: tuple | dict = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, args).fetchall()

    # ------------------------------------------------------------- kv
    def get(self, key: str, default: str | None = None) -> str | None:
        row = self.one("SELECT v FROM kv WHERE k=?", (key,))
        return row["v"] if row else default

    def put(self, key: str, value: str | None) -> None:
        if value is None:
            self.execute("DELETE FROM kv WHERE k=?", (key,))
        else:
            self.execute("INSERT INTO kv(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, value))

    def get_json(self, key: str, default=None):
        raw = self.get(key)
        return json.loads(raw) if raw else default

    def put_json(self, key: str, value) -> None:
        self.put(key, json.dumps(value, sort_keys=True))

    # ------------------------------------------------------------- lease (P5)
    def acquire_lease(self, owner: str, now: float | None = None) -> bool:
        now = int(now if now is not None else time.time())
        with self.tx():
            row = self.one("SELECT owner, heartbeat FROM lease WHERE id=1")
            if row is None:
                self.execute("INSERT INTO lease(id, owner, heartbeat) VALUES(1, ?, ?)", (owner, now))
                return True
            if row["owner"] == owner or now - row["heartbeat"] > LEASE_TTL_S:
                self.execute("UPDATE lease SET owner=?, heartbeat=? WHERE id=1", (owner, now))
                return True
            return False

    def lease_info(self) -> dict | None:
        row = self.one("SELECT owner, heartbeat FROM lease WHERE id=1")
        return dict(row) if row else None

    # ------------------------------------------------------------- outbox / events
    def outbox_add(self, dedupe: str, text: str, critical: bool = False, now: float | None = None) -> bool:
        cur = self.execute(
            "INSERT OR IGNORE INTO outbox(dedupe, created_at, text, critical) VALUES(?, ?, ?, ?)",
            (dedupe, int(now or time.time()), text, int(critical)))
        return cur.rowcount == 1

    def outbox_pending(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.all("SELECT * FROM outbox WHERE sent_at IS NULL AND attempts < 20 ORDER BY id LIMIT ?", (limit,))

    def event(self, ts: float, symbol: str, zone_key: str | None, chain: str | None, state: str, output: str,
              scores: tuple[int, int, int, int] = (0, 0, 0, 0), reason: str = "", data: dict | None = None) -> None:
        self.execute(
            "INSERT INTO events(ts, symbol, zone_key, chain, state, output, p1, p2, p3, p4, reason, data) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (int(ts), symbol, zone_key, chain, state, output, *scores, reason,
             json.dumps(data, sort_keys=True, default=str) if data else None))

    def order_log(self, setup_key: str, tool: str, request: dict, response, ms: int, ok: bool,
                  now: float | None = None) -> None:
        self.execute(
            "INSERT INTO order_log(ts, setup_key, tool, request, response, ms, ok) VALUES(?,?,?,?,?,?,?)",
            (int(now or time.time()), setup_key, tool, json.dumps(request, sort_keys=True, default=str),
             json.dumps(response, sort_keys=True, default=str)[:4000], ms, int(ok)))

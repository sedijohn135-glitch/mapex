"""Glue shared by the live service and the replay backtester: one code path for decisions (paper = live = replay)."""

from __future__ import annotations

import json

from mapex.config import Settings
from mapex.executor.engine import ExecInput, ExecResult, evaluate
from mapex.executor.state import archive_missing, load_states, save_state
from mapex.mapper.engine import MapperInput, MapResult, build_map
from mapex.store import Store

MAPS_KEPT = 72


def run_mapper(store: Store, s: Settings, symbol: str, bars: dict, price: float, now: float) -> MapResult:
    """GEM1 run; a map is *published* only when Step 8 passes (invalid maps are kept for audit only)."""
    res = build_map(MapperInput(symbol, now, price, bars, s.tick(symbol), s.display_decimals.get(symbol, 2),
                                s.chain_gates, s.chain_mode))
    with store.tx():
        cur = store.execute(
            "INSERT INTO maps(symbol, created_at, json, bias, valid, hash, struct_hash, reason) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (symbol, int(now), res.dumps(), (res.json or {}).get("strategic_bias"), int(res.valid),
             res.meta.get("hash"), res.meta.get("struct_hash"), res.reason))
        if res.valid:
            map_id = cur.lastrowid
            keys = set(res.meta["zone_keys"].values())
            archive_missing(store, symbol, keys)
            if keys:
                marks = ",".join("?" * len(keys))
                store.execute(f"UPDATE zones SET map_id=? WHERE zone_key IN ({marks})", (map_id, *keys))
        # retention: the last MAPS_KEPT maps per symbol (audit), always including the published one
        store.execute("DELETE FROM maps WHERE symbol=? AND id < (SELECT MAX(id) FROM maps WHERE symbol=?) - ? "
                      "AND id != COALESCE((SELECT MAX(id) FROM maps WHERE symbol=? AND valid=1), -1)",
                      (symbol, symbol, MAPS_KEPT, symbol))
    return res


def current_map(store: Store, symbol: str) -> tuple[dict | None, dict]:
    """Latest published (valid) map; the JSON is parsed once per map id."""
    row = store.one("SELECT id FROM maps WHERE symbol=? AND valid=1 ORDER BY id DESC LIMIT 1", (symbol,))
    if row is None:
        return None, {}
    cache = store.__dict__.setdefault("map_cache", {})
    hit = cache.get(symbol)
    if hit and hit[0] == row["id"]:
        return hit[1], hit[2]
    data = json.loads(store.one("SELECT json FROM maps WHERE id=?", (row["id"],))["json"])
    meta = data["meta"]
    meta["map_id"] = row["id"]
    cache[symbol] = (row["id"], data["map"], meta)
    return data["map"], meta


def run_executor(store: Store, s: Settings, symbol: str, now: float, bars: dict, bid: float | None,
                 ask: float | None, market_open: bool, data_ok: bool = True) -> ExecResult:
    m, meta = current_map(store, symbol)
    states = load_states(store, symbol)
    inp = ExecInput(symbol, now, m, meta, m is not None, bars, bid, ask, market_open, data_ok, s.tick(symbol),
                    s.display_decimals.get(symbol, 2), float(s.sl_safety_buffer.get(symbol, 0.0)), s.map_max_age_h)
    res = evaluate(inp, states)
    with store.tx():
        zones = {kz["id"]: kz for kz in (m or {}).get("key_zones", [])}
        for key, st in res.states.items():
            if states.get(key) is None or st != states[key]:
                st.map_id = meta.get("map_id")
                save_state(store, st, zones.get(st.chain))
        last = store.get_json(f"last_out:{symbol}", {})
        for d in res.decisions:
            if d.output == "WATCH" and not d.reason.startswith("raid"):
                continue
            sig = f"{d.output}|{d.reason}"
            k = d.zone_key or "_"
            if d.output in ("WAIT", "NO-SETUP") and last.get(k) == sig:
                continue  # repeated WAIT / preflight NO-SETUP: one event, not one per minute
            last[k] = sig
            store.event(d.ts or now, symbol, d.zone_key, d.chain, d.state or d.output, d.output, d.scores, d.reason,
                        d.data)
        store.put_json(f"last_out:{symbol}", last)
    return res

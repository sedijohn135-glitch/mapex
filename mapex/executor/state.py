"""Per-zone state machine persistence (zones table), keyed by the stable zone_key (A7)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

STATES = ("WATCH", "RAID", "SHIFT", "GAP", "RETURN", "CONFIRMED", "MONITOR", "DEAD")


@dataclass
class ZoneState:
    zone_key: str
    symbol: str
    chain: str
    direction: str
    state: str = "WATCH"
    sweep_count: int = 0
    last_sweep_type: str | None = None
    last_sweep_level: float | None = None
    displacement_detected: bool = False
    last_event_at: int | None = None
    monitor_reason: str | None = None
    monitor_hash: str | None = None
    thesis_status: str = "ACTIVE"
    first_seen: int = 0
    map_id: int | None = None
    ctx: dict = field(default_factory=dict)

    def clear_sequence(self) -> None:
        """Back to WATCH after a Level-1 RESET: sweep_count and the thesis survive."""
        seq = ("raid", "type7", "disp", "mss_level", "mss", "wick_break", "fvg", "retest_pending", "footprint")
        for k in seq:
            self.ctx.pop(k, None)
        self.state = "WATCH"
        self.displacement_detected = False

    def copy(self) -> ZoneState:
        return ZoneState(**{**asdict(self), "ctx": json.loads(json.dumps(self.ctx))})


def load_states(store, symbol: str) -> dict[str, ZoneState]:
    out = {}
    for r in store.all("SELECT * FROM zones WHERE symbol=? AND archived=0", (symbol,)):
        out[r["zone_key"]] = ZoneState(
            zone_key=r["zone_key"], symbol=r["symbol"], chain=r["chain"], direction=r["direction"], state=r["state"],
            sweep_count=r["sweep_count"], last_sweep_type=r["last_sweep_type"], last_sweep_level=r["last_sweep_level"],
            displacement_detected=bool(r["displacement_detected"]), last_event_at=r["last_event_at"],
            monitor_reason=r["monitor_reason"], monitor_hash=r["monitor_hash"], thesis_status=r["thesis_status"],
            first_seen=r["first_seen"] or 0, map_id=r["map_id"], ctx=json.loads(r["ctx"] or "{}"))
    return out


def save_state(store, st: ZoneState, zone_json: dict | None = None) -> None:
    store.execute(
        "INSERT INTO zones(zone_key, symbol, map_id, chain, direction, data, state, sweep_count, last_sweep_type, "
        "last_sweep_level, displacement_detected, last_event_at, monitor_reason, monitor_hash, thesis_status, "
        "first_seen, archived, ctx) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?) "
        "ON CONFLICT(zone_key) DO UPDATE SET map_id=excluded.map_id, chain=excluded.chain, "
        "data=COALESCE(excluded.data, zones.data), state=excluded.state, sweep_count=excluded.sweep_count, "
        "last_sweep_type=excluded.last_sweep_type, last_sweep_level=excluded.last_sweep_level, "
        "displacement_detected=excluded.displacement_detected, last_event_at=excluded.last_event_at, "
        "monitor_reason=excluded.monitor_reason, monitor_hash=excluded.monitor_hash, "
        "thesis_status=excluded.thesis_status, archived=0, ctx=excluded.ctx",
        (st.zone_key, st.symbol, st.map_id, st.chain, st.direction,
         json.dumps(zone_json) if zone_json is not None else None, st.state, st.sweep_count, st.last_sweep_type,
         st.last_sweep_level, int(st.displacement_detected), st.last_event_at, st.monitor_reason, st.monitor_hash,
         st.thesis_status, st.first_seen, json.dumps(st.ctx, sort_keys=True)))


def archive_missing(store, symbol: str, keep: set[str]) -> None:
    """Zones that vanished from the map are archived (their state is kept for audit, A7)."""
    rows = store.all("SELECT zone_key FROM zones WHERE symbol=? AND archived=0", (symbol,))
    for r in rows:
        if r["zone_key"] not in keep:
            store.execute("UPDATE zones SET archived=1 WHERE zone_key=?", (r["zone_key"],))

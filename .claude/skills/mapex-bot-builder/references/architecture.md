# Architecture — MAPEX bot

## 1. Shape of the system

One Python process on Railway. No public MCP server, no OAuth, no Gemini: MAPEX is a **client** of the cTrader
Remote MCP and a **worker** that runs on its own schedule.

```
                    ┌──────────────── MAPEX (one Railway service) ─────────────────┐
 cTrader Remote MCP │  data: get_symbols · get_spot_prices · get_trendbars         │
 (IC Markets)  ◄────┤  trade: create_order · amend_position · close_position       │
                    │        get_positions · get_position_details · get_deals      │
                    │                                                              │
                    │  mapper (GEM1) ──► Strategic Map JSON ──► executor (GEM2)    │
                    │        ▲ every H1 close                      │ every M1 close │
                    │        │                                      ▼               │
                    │   candle store (SQLite + memory)        decision WAIT/RESET/  │
                    │                                          MONITOR/CONFIRMED    │
                    │                                               │ CONFIRMED     │
                    │                              risk guards ─────┤               │
                    │                                               ▼               │
                    │                                    broker (paper | live)      │
                    │                                               │               │
                    └───────────────────────────────────────────────┼──────────────┘
                                                                    ▼
                                                        Telegram: "MAPEX HYRI …"
```

## 2. Modules

```
mapex/
  main.py           # asyncio entrypoint: lifespan, scheduler, /health ASGI app
  config.py         # env parsing, per-symbol settings, constants from the spec files
  store.py          # SQLite schema + queries + lease
  ctrader/
    client.py       # MCP client session, retries, rate limit, auth errors, tool discovery
    decode.py       # pipettes ↔ display, points, volume cents, band calibration
    broker.py       # order send / verify / amend / partial close / reconcile (live)
    paper.py        # same interface, simulated fills
  data/
    candles.py      # fetch + cache + closed-bar rules + higher-TF integrity
    quotes.py       # spot polling, spread stats
  core/
    timeutil.py     # NY time, sessions, killzones, macro windows, market hours
    primitives.py   # swings, structure, displacement, FVG/IFVG/BPR/VI/VOID/OB/BB/RB, ATR, session ATR
  mapper/
    engine.py       # GEM1 Step 0 → Step 8
    liquidity.py    # discovery + LPS scoring
    chains.py       # PDA candidates, linkage, confidence, chain assignment
    schema.py       # Strategic Map dataclasses ↔ GEM1 JSON
  executor/
    engine.py       # Module 5, P1..P4, Modules 6/7/8, scoring, alpha pick
    stoploss.py     # Layer 6 fiduciary SL mandate
    targets.py      # Layer 5 TP ladder, 3R policy, reachability, guardrails
    state.py        # per-zone state machine persistence
  guards.py         # kill switch, trade counters, consecutive losses, daily R, spread, hours, lot sanity
  telegram.py       # entry notifications, critical alerts, commands
  replay.py         # historical replay of mapper+executor with simulated fills
tests/  docs/  Dockerfile  pyproject.toml  uv.lock  .env.example  CLAUDE.md
```

Stack: Python 3.12, `uv`, `mcp==2.2.0` (client side; it pulls `httpx2`, `anyio`, `starlette`, `uvicorn`),
`tzdata` (mandatory for `America/New_York`), stdlib `sqlite3` (WAL). Dev: `pytest`, `ruff`, `httpx` (TestClient).
Run as root in Docker (Railway volumes). Shell-form CMD so `$PORT` expands. No `railway.json` (deprecated).

## 3. Scheduler (single asyncio loop, lease-protected)

| Task | Cadence |
|---|---|
| quote poll | 1 s when a position is open or price is within 0.5 × session_ATR of a chain zone, else 10 s |
| M1 close handler → executor tick | every minute, ~3 s after the bar closes |
| M5 / M15 close handlers | at their closes (executor uses them for raid/MSS logic) |
| mapper run | at every H1 close + 60 s, and at start-up; forced re-run after a Level 3 invalidation |
| position manager | every 5 s while any MAPEX position is open (TP1 partial, breakeven, protection check) |
| reconciliation | every 30 s: compare DB trades ↔ `get_positions` (label filter) |
| daily roll | 17:05 NY: reset counters, write daily stats |
| credential heartbeat | `get_version` every 5 min when idle |

Only one instance may run: `lease` row with heartbeat (Railway can briefly overlap deploys).

## 4. Persistence (SQLite at `DATA_DIR/mapex.db`)

- `maps` — id, symbol, created_at, json (full GEM1 Strategic Map), bias, valid, hash
- `zones` — zone_key (stable: symbol+tf+zone_type+formed_at+direction), map_id, chain, thesis fields,
  state (WATCH/RAID/SHIFT/GAP/RETURN/CONFIRMED/DEAD), sweep_count, last_sweep_type, displacement_detected,
  last_event_at, monitor_reason
- `trades` — setup_key (unique), symbol, side, lots, volume_cents, entry_planned, entry_fill, sl, tp1, tp2, tp3,
  position_id, order_id, state (SENDING/OPEN/PARTIAL/BE/CLOSED/FAILED), partial_done, be_done, opened_at,
  closed_at, result_r, mode (paper/live)
- `order_log` — every request/response pair (redacted), for audit and post-mortems
- `events` — decision trail: one row per executor tick that changes state, with the phase scores and reason
- `outbox` — Telegram messages (dedupe key unique)
- `daily_stats`, `kv` (credentials override, digits calibration, symbol map), `lease`

A state transition and its outbox row are written in one transaction. `trades.setup_key` is UNIQUE — the insert
itself is the lock that prevents a second order for the same setup.

`DATA_DIR` = `DATA_DIR` env → `RAILWAY_VOLUME_MOUNT_PATH` → `./data`. Without a volume: loud warning in `/health`
and one Telegram alert (trade history and dedupe keys would be lost on redeploy).

## 5. Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `CTRADER_MCP_CONFIG` | — | paste from cTrader Web → Settings → Remote MCP (fragment, full JSON or bare token all accepted) |
| `CTRADER_MCP_URL` / `CTRADER_MCP_TOKEN` | — | alternative to the above |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | — | notifications |
| `TRADING_MODE` | `paper` | `paper` = decisions + simulated fills; `live` = real orders |
| `CONFIRM_LIVE_ACCOUNT` | `NO` | must be `YES` when the token's environment decodes as live |
| `SYMBOLS` | `XAUUSD,BTCUSD` | traded instruments |
| `LOT_XAUUSD`, `LOT_BTCUSD` | — | **owner's lot size**; a symbol with no lot never trades |
| `MAX_LOT` | `1.0` | hard ceiling; a bigger `LOT_*` is refused at start-up with an alert |
| `CONTRACT_SIZE` | `{"XAUUSD":100,"BTCUSD":1}` | units per lot, used for volume-in-cents |
| `MAX_OPEN_TOTAL` / `MAX_OPEN_PER_SYMBOL` | `2` / `1` | concurrent MAPEX positions |
| `MAX_TRADES_PER_DAY` | `3` | per NY day, all symbols |
| `MAX_CONSECUTIVE_LOSSES` | `3` | halt new entries until the next NY day |
| `DAILY_LOSS_LIMIT_R` | `3.0` | sum of realised −R in a NY day → halt |
| `TP1_CLOSE_PCT` | `50` | Pay-the-Trader partial close (GEM2 Layer 5: 50–80) |
| `MAX_SPREAD` | `{"XAUUSD":0.80,"BTCUSD":60}` | no entry above this |
| `MAX_SLIPPAGE_POINTS` | `{"XAUUSD":300,"BTCUSD":3000}` | market-range bound |
| `SL_SAFETY_BUFFER` | `{"XAUUSD":0.30,"BTCUSD":15}` | GEM2 Layer 6 safety part of the spread buffer |
| `PRICE_DIGITS`, `PRICE_BANDS` | see broker doc | decoding overrides / sanity bands |
| `NOTIFY_EXITS` | `false` | owner asked for entry notifications only |
| `MAPPER_MIN_CHAIN_LPS` | — | optional override of GEM1 chain gates (default = GEM1 values 65/50/40) |
| `DATA_DIR`, `LOG_LEVEL` | — | |

Start-up self-check (one Telegram line, then `/health`): mode, environment (demo/live from the token), symbols and
lots, guards, volume the lots translate to, data freshness. If `TRADING_MODE=live` and the token decodes as a live
environment while `CONFIRM_LIVE_ACCOUNT≠YES` → the bot runs in paper and says so.

## 6. Health endpoint

`GET /health` → always 200 while the process lives: mode, environment, lease, last quote age per symbol, active map
ids and bias, zone states, open trades, guard status, last error. Never includes tokens. Railway healthcheck path
`/health` — it must not depend on cTrader being reachable, otherwise a dead token blocks deploys.

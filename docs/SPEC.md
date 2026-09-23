# SPEC — MAPEX (index)

MAPEX = GEM1 (HTF Structure Mapper) + GEM2 (LTF Execution Engine) as deterministic Python, one Railway service,
reading IC Markets data and trading through the cTrader Remote MCP (client side, `mcp==2.2.0`).

| Topic | Source of truth | Code |
|---|---|---|
| Strategy law | `docs/source/GEM1.md`, `docs/source/GEM2.md` | `mapex/mapper/`, `mapex/executor/` |
| Build spec | `.claude/skills/mapex-bot-builder/SKILL.md` + `references/` | whole repo |
| Primitives (numbers for the prose) | `references/shared-primitives.md` | `mapex/core/` |
| Orders, units, quirks | `references/broker-execution.md`, spotware/ctrader-skills | `mapex/ctrader/` |
| Guards | `references/broker-execution.md` §3, §7 | `mapex/guards.py` |
| Telegram (Albanian) | `references/telegram-messages-sq.md` | `mapex/telegram.py` |
| Deploy (Albanian) | `references/deploy-railway-sq.md` | `docs/SETUP_SQ.md`, `Dockerfile` |
| Rule ↔ code ↔ test | — | `docs/TRACEABILITY.md` (checked by `tests/test_traceability.py`) |
| Decisions / deviations | — | `docs/DECISIONS.md` |

## Runtime (one asyncio process, lease-protected)
quotes 1 s near action / 10 s idle · M1 tick 3 s after each close (executor) · mapper at every H1 close + 60 s and on
Level-3 invalidation · management every 5 s with open positions · reconciliation every 30 s · daily roll 17:05 NY ·
heartbeat 5 min · Telegram polling + outbox · `/health` (never depends on cTrader).

## Data flow
`ctrader/client` → `data/candles` (closed bars only) → `pipeline.run_mapper` (GEM1 JSON in `maps`) →
`pipeline.run_executor` (zone states in `zones`, decisions in `events`) → CONFIRMED plan → `guards` →
`ctrader/broker.TradeManager` with `LiveVenue` or `PaperVenue` → `trades`, `order_log`, `outbox` → Telegram.

## Plan followed (build order, each step gated by green tests, one commit per step)
1. scaffold · 2. primitives · 3. cTrader read path · 4. GEM1 mapper · 5. GEM2 executor · 6. broker + guards ·
7. Telegram · 8. replay · 9. docs — then verification (pytest, ruff, clean-venv run, failure-modes walk,
traceability) and ship to `main`.

## Failure modes
All rows of `references/failure-modes.md` are covered; see the checklist in `README.md`.

## Example
`docs/example_map.json` is a real mapper output on synthetic BTCUSD candles (valid map with CHAIN_A); the
`liquidity_registry` is trimmed to the pools referenced by the chains and alerts to keep the file readable.

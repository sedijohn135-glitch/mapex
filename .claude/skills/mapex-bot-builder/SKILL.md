---
name: mapex-bot-builder
description: Complete build spec for MAPEX bot - a single Python service on Railway that converts the GEM1 HTF Structure Mapper and GEM2 LTF Execution Engine prompts into deterministic code, reads live IC Markets data through the cTrader Remote MCP, and opens, protects and manages trades automatically with lot size taken from Railway variables, notifying Telegram on entry. Use this skill whenever working in the MAPEX repo - building it from scratch, continuing, fixing, changing mapper or executor rules, order placement, risk guards, the replay backtester or the Railway deployment - even if the user only says continue, fix, deploy or add a rule.
---

# MAPEX bot — build skill

## Mission

Two prompts become one program:

- **GEM 1 — HTF Structure Mapper** → `mapex/mapper/` : D1/H4/H1 candles in, a Strategic Map JSON out
  (liquidity registry with LPS scores, strategic bias, DOL, causal chains CHAIN_A/B/C).
- **GEM 2 — LTF Execution Engine** → `mapex/executor/` : Strategic Map + M15/M5/M1 candles + live quote in,
  one decision out (WAIT / CONFIRMED / RESET / MONITOR / NO-SETUP) through P1–P4 and Modules 5–8.
- **CONFIRMED (100/100) → the bot places the trade itself** through the cTrader Remote MCP: market entry with
  stop loss and take profit attached, partial close at TP1, stop to breakeven after TP1.

No screenshots, no Gemini, no LLM at runtime. Everything is deterministic code over candles from the one data
source available: the cTrader Remote MCP of the owner's IC Markets account.

The owner sets lot size in Railway variables. He receives one Telegram message when MAPEX enters a trade.

## Hard constraints

- **Faithfulness first.** `references/source/GEM1.md` and `GEM2.md` are the source of truth. Every step, module,
  gate, score and threshold in them must exist in code, with the same names, and be traceable: build
  `docs/TRACEABILITY.md` mapping each GEM section → module.function → test. Where a GEM rule is ambiguous, use the
  exact formula given in `references/shared-primitives.md` / the two spec files — never invent your own
  interpretation silently; anything you still must decide goes into `docs/DECISIONS.md`.
- **A+ only.** A trade is placed only when P1+P2+P3+P4 = 100/100 and every guard passes. WAIT, RESET, MONITOR and
  NO-SETUP never place orders.
- **Money logic is the owner's.** Lot size comes from `LOT_<SYMBOL>` variables. The code never computes lots from
  balance, never asks for risk %, never reads equity to size anything. Circuit breakers count trades and R, not money.
- **Never naked.** A position must never exist without a stop loss: orders are sent with relative SL/TP attached,
  then amended to the exact structural levels, then verified. If verification fails → close the position and halt.
- **Never double-fire.** Order sending is idempotent through `label`/`comment` keys plus reconciliation. A timeout is
  never retried blind.
- **Default is paper.** `TRADING_MODE=paper` until the owner flips it; a live-environment token additionally requires
  `CONFIRM_LIVE_ACCOUNT=YES`.
- **Only MAPEX positions.** The bot may only amend or close positions carrying its own label; anything the owner
  opens or edits by hand is untouchable.
- **Phone-only owner, no coding.** Never ask him technical questions; decide, log the decision, continue. Telegram
  messages in Albanian; code, comments and tests in English.
- **Ponytail**: simplest correct implementation, stdlib first — but never drop a GEM rule, a guard or a test.

## References — read progressively

| When | Read |
|---|---|
| Phase 0 | `references/claude-md.md` |
| Phase 1 | `references/architecture.md`, `references/failure-modes.md`, then skim both files in `references/source/` |
| Mapper work | `references/gem1-mapper-spec.md` + `references/shared-primitives.md` + `source/GEM1.md` |
| Executor work | `references/gem2-executor-spec.md` + `references/shared-primitives.md` + `source/GEM2.md` |
| Orders | `references/broker-execution.md` |
| Telegram / deploy | `references/telegram-messages-sq.md`, `references/deploy-railway-sq.md` |

## Phase 0 — bootstrap

1. If this skill arrived as `mapex-bot-builder.zip` in the repo root: extract so that
   `.claude/skills/mapex-bot-builder/SKILL.md` exists, delete the zip, commit.
2. Copy `references/source/GEM1.md` and `GEM2.md` into `docs/source/` in the repo (they are the spec of record).
3. Create `CLAUDE.md` from `references/claude-md.md`; commit and push immediately.
4. **Turn the owner's plugins on for this repo** (agent-skills, ponytail, graphify, ruflo — they are the owner's
   token budget, so this is not optional):
   a. Check what is actually loaded in this session: list the available skills / marketplaces with the plugin
      command of the current Claude Code version. Write the result into `docs/DECISIONS.md` as "plugins available".
   b. For every one of the four that exists in this environment, persist it in the repo so **every future session
      loads it automatically**: read the current official Claude Code documentation for project plugin settings
      (`.claude/settings.json`, keys for known marketplaces and enabled plugins) and write the file with the exact
      current schema — take the marketplace source values from this environment, never from memory. Commit it as
      `chore: enable project plugins`.
   c. Verify by naming one skill from each plugin and confirming it resolves (e.g. `agent-skills:plan`,
      `ponytail:ponytail-review`, `graphify:graphify`, a `ruflo-*` skill).
   d. Anything that does not resolve: note it in `docs/DECISIONS.md` and continue without it. Never spend more than
      a few minutes on plugin infrastructure — the build matters more than the tooling.

## Phase 1 — plan

`agent-skills:spec` → `docs/SPEC.md` (short index + decisions log), `agent-skills:plan` → task list following the
build order below. `agent-skills:source-driven-development` before any external API work: read the installed
`mcp` SDK source (v2 renamed the server class to `MCPServer`, the HTTP client library is `httpx2`; for MAPEX only
the **client** side is used) and the Spotware docs listed in `references/broker-execution.md`.

## Phase 2 — build order (each step gated by green tests, commit per step)

Skills to drive the work, chosen automatically — the owner never asks for them:
`agent-skills:build` + `agent-skills:incremental-implementation` throughout ·
`agent-skills:test-driven-development` for steps 2, 4, 5 (write the fixtures and the expected decisions first) ·
`agent-skills:doubt-driven-development` for steps 5 and 6 (a wrong order costs real money) ·
`agent-skills:source-driven-development` for step 3 and 6 (cTrader, MCP SDK) ·
`agent-skills:debugging-and-error-recovery` on any red test ·
`ponytail:ponytail` before writing each module and `ponytail:ponytail-review` before each commit ·
`graphify:graphify` at the start of any later session on the existing codebase ·
`ruflo-adr:adr-create` (only if ruflo tools are live) for the three architectural decisions:
order protocol, thesis-state persistence, paper/live parity.

1. **Scaffold** — `pyproject.toml` + `uv.lock` (Python 3.12, pinned), package layout, `config.py`, `store.py`
   (SQLite), `/health`, Dockerfile, CI, `.env.example`. Gate: service starts with no secrets, `/health` 200.
2. **Primitives** (`references/shared-primitives.md`) — candles, closed-bar logic, sessions/NY time, ATR,
   session ATR, swings, structure (BOS/MSS/CISD), FVG/IFVG/BPR/VI/VOID/OB/BB/RB detection, displacement,
   price-unit normalisation. Gate: unit tests with hand-built candle fixtures for every detector.
3. **cTrader read path** — MCP client, symbol map, pipettes decoding + band calibration, trendbar fetching with
   720 h chunking, quote polling, auth-error handling, credential hot-swap. Gate: tests against a fake MCP server.
4. **GEM1 mapper** — full pipeline Step 0 → Step 8, emitting the GEM1 JSON schema. Gate: golden-map tests
   (`gem1-mapper-spec.md` §9) including LPS arithmetic, bias derivation, chain gates, verification failures.
5. **GEM2 executor** — Module 5, P1, P2, P3 + Module 6, P4 + Module 7, Module 8, scoring, SL mandate, TP ladder,
   alpha pick. Gate: golden-scenario tests (`gem2-executor-spec.md` §10), no-lookahead test.
6. **Broker + risk guards** (`references/broker-execution.md`) — volume encoding, order send, verification,
   amend, partial close, breakeven, reconciliation, kill switch, circuit breakers, paper engine. Gate: fake-broker
   tests for every failure branch listed there.
7. **Telegram** — entry notifications, critical alerts, commands. Gate: fake Bot API tests.
8. **Replay backtester** — run mapper+executor over historical candles with simulated fills; `/replay SYMBOL DAYS`.
   Gate: no-lookahead test + deterministic repeat run.
9. **Docs** — `docs/SETUP_SQ.md`, `docs/RULES_SQ.md` (plain Albanian: what MAPEX does and when it trades),
   `docs/TRACEABILITY.md`, `README.md`.

## Phase 3 — verification

- `uv run pytest -q` green, `uv run ruff check` clean, Dockerfile build (or clean-venv equivalent).
- `agent-skills:security-and-hardening` over credentials, Telegram guard, order path.
- `agent-skills:review` + `ponytail:ponytail-review`.
- Walk `references/failure-modes.md` and tick every row.
- Traceability check: a test asserts every GEM section id listed in `docs/TRACEABILITY.md` has a real function and test.

## Phase 4 — ship

Everything on `main`. If only a side branch is possible, open a PR and give the owner Albanian merge steps for the
GitHub phone app. Final Albanian message: what was built, the Railway variables to fill, and the three-step
go-live path (paper → demo token → live with `CONFIRM_LIVE_ACCOUNT=YES`).

## Definition of done

- [ ] GEM1 and GEM2 fully implemented as code; `docs/TRACEABILITY.md` complete; deviations in `docs/DECISIONS.md`.
- [ ] Trade fires only at 100/100 plus guards; WAIT/RESET/MONITOR never order.
- [ ] Every order path tested: success, timeout, rejection, partial fill, missing SL, duplicate prevention,
      restart mid-trade, manual interference, market closed, spread spike.
- [ ] Paper mode is the default and produces identical decisions to live mode (same code path, simulated fills).
- [ ] Replay runs over ≥ 30 days of history and reports trades/win rate/R without lookahead.
- [ ] Telegram: entry messages + critical alerts only; lot size read from variables and echoed in the message.
- [ ] No `railway.json`; deployment documented for phone-only setup.

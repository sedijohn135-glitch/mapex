# MAPEX

GEM1 (HTF Structure Mapper) and GEM2 (LTF Execution Engine) turned into deterministic Python. One service on Railway
reads IC Markets candles through the cTrader Open API (or the Remote MCP), maps liquidity every hour, runs the P1–P4 execution machine
every minute and — only at **100/100** with every guard passing — opens the trade itself with stop loss and take profit
attached, takes a partial at TP1 and moves the stop to breakeven. Lot size comes from Railway variables. Telegram
receives one message per entry plus critical alerts, in Albanian.

> **Pronari (shqip):** instalimi me telefon → [`docs/SETUP_SQ.md`](docs/SETUP_SQ.md) ·
> çfarë bën dhe kur tregton → [`docs/RULES_SQ.md`](docs/RULES_SQ.md)

## Layout
```
mapex/
  main.py        asyncio entrypoint: lease, scheduler, /health, Telegram commands, secret redaction
  config.py      Railway variables (starts with no secrets; lots only from LOT_<SYMBOL>)
  store.py       SQLite (WAL): maps, zones, trades, order_log, events, outbox, kv, lease
  pipeline.py    the single decision path used by live, paper and replay
  core/          NY time/sessions/killzones, candle primitives, PD arrays, liquidity levels
  ctrader/       Open API + MCP clients, unit decoding, broker order flow (live), paper venue
  data/          candle cache (closed bars, contiguity), quotes (freshness, skew)
  mapper/        GEM1 Step 0 → 8 (liquidity registry + LPS, bias, chains, verification, brief)
  executor/      GEM2 Module 5, P1–P4, Modules 6–8, stop-loss mandate, TP ladder, alpha pick
  guards.py      kill switch and circuit breakers (count trades and R, never money)
  telegram.py    Albanian messages, outbox, commands
  replay.py      historical replay with simulated fills (/replay SYMBOL DAYS)
docs/            SETUP_SQ, RULES_SQ, SPEC, DECISIONS, TRACEABILITY, source/GEM1.md, source/GEM2.md
```

## Run
```bash
uv sync
uv run pytest -q          # 150+ tests: golden GEM fixtures, fake cTrader MCP, fake Bot API, replay
uv run ruff check
uv run python -m mapex.main   # starts in paper mode with no secrets; GET :$PORT/health
```
Docker: `docker build -t mapex . && docker run -p 8080:8080 -e PORT=8080 mapex` (no `railway.json`).

## Safety model
- `TRADING_MODE=paper` by default; a live-account token also needs `CONFIRM_LIVE_ACCOUNT=YES`, otherwise MAPEX runs
  in paper and says so.
- An order is sent once (`setup_key` unique in SQLite, label `MAPEX`, comment = setup key). A timeout is reconciled by
  comment, never resent. SL/TP travel with the market order as relative points, are amended to the exact levels with
  **both** legs, and read back; any mismatch closes the position and trips the kill switch.
- Only positions labelled `MAPEX` are ever amended or closed; owner changes are adopted, never fought.

## Failure-modes checklist (`references/failure-modes.md`)
| # | Proof |
|---|---|
| M1 duplicate order | `test_timeout_never_resends_and_adopts_by_comment`, `test_restart_rebuilds_state_without_duplicate`, `test_duplicate_setup_key_blocked_by_db` |
| M2 position without SL | `test_happy_path_one_order_relative_points_and_exact_amend`, `test_missing_sl_closes_and_trips` |
| M3 volume 100× wrong | `test_volume_mismatch_closes_immediately`, `test_partial_fill_is_a_volume_mismatch`, `test_volume_cents_mapping`, `test_lot_above_max_refused` |
| M4 amend drops a leg | `test_amend_position_both_fields_everywhere` |
| M5 pipettes in a price field | `test_units` |
| M6 symbol without lot | `test_defaults_start_without_secrets`, `test_market_guards_spread_killzone_quote_lot` |
| M7 live instead of paper | `test_effective_mode_live_confirmation`, `test_live_app_with_fake_broker_calibrates_and_commands` |
| M8 runaway losses | `test_each_breaker_blocks_independently`, `test_breakers_trip_kill_switch_on_results` |
| M9 fighting the owner | `test_manual_modify_and_close_are_respected`, `test_foreign_positions_are_untouchable` |
| M10 stale / foreign price | `test_quote_freshness_and_skew`, `test_market_guards_spread_killzone_quote_lot` |
| M11 entry outside killzone | `test_killzone_gates`, `test_market_guards_spread_killzone_quote_lot` |
| M12 weekend / rollover | `test_market_hours_gold_and_btc`, `test_market_closed_blocks_before_any_call` |
| A1 lookahead | `test_mapper_no_lookahead`, `test_determinism_and_no_lookahead`, `test_replay_30_days_deterministic_and_no_lookahead` |
| A2 forming bar | `test_closed_bars_no_lookahead` |
| A3 aggregated HTF | `test_every_timeframe_fetched_from_broker_never_aggregated` |
| A4 dropped GEM rule | `tests/test_traceability.py` over `docs/TRACEABILITY.md` |
| A5 gold-era constants | `test_constants_scale_with_price_for_both_symbols` |
| A6 100 with a partial phase | every executor test asserts CONFIRMED ⇔ 100 (`assert_all_confirmed_are_100`), `test_judas_threshold_not_exceeded` |
| A7 zone identity drift | `test_state_roundtrip_in_sqlite`, `test_monitor_clears_only_with_structurally_new_map` |
| A8 stale map | `test_preflight_no_setup_without_touching_state` |
| A9 session ATR fallback | `test_session_atr_fallback_and_normal` |
| P1 token expiry | `test_auth_error_detected_and_hot_swap`, `test_expired_token_alerts_once_and_trips_after_15_minutes` |
| P2 poisoned batch | `test_unknown_symbol_never_sent_in_batch` |
| P3 period / 720 h | `test_trendbars_chunked_720h_and_paginated`, `test_tool_rejection_surfaces` |
| P4 rate limits | `test_rate_limiter_spacing` |
| P5 two instances | `test_lease_only_one_leader` |
| P6 no volume | `test_defaults_start_without_secrets` (warning + start-up alert) |
| P7 /health vs broker | `test_health_never_depends_on_broker_and_never_leaks_token` |
| P8 DST | `test_dst_march_and_november` |
| P9 secrets in logs | `test_log_redaction_masks_tokens` |
| P10 Telegram flood / parse | `tests/test_telegram.py` |
| P11 clock skew | `test_quote_freshness_and_skew` (+ `clock_skew` guard) |

Decisions and every interpretation of the GEM prose: [`docs/DECISIONS.md`](docs/DECISIONS.md).

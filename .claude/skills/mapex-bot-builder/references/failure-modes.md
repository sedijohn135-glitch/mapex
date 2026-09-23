# Failure modes — MAPEX (tick every row before shipping)

## Money-critical

| # | Risk | Mitigation | Proof |
|---|---|---|---|
| M1 | Duplicate order after timeout/restart | unique `setup_key` row + label/comment reconciliation, never blind retry | broker tests 2, 9 |
| M2 | Position without stop loss | relative SL/TP on the market order, amend to absolute, read-back verify, else close + kill switch | broker tests 1, 4 |
| M3 | Volume 100× wrong (`CONTRACT_SIZE`, cents) | typed conversions, start-up echo, first-fill volume check, `MAX_LOT` cap | broker test 5 |
| M4 | `amend_position` removes TP/SL because a field was omitted | helper that always sends both; grep test | broker test 7 |
| M5 | Pipettes written into an order price field (Q-K19) | `Display` vs `Pipettes` types, band check before send | decode tests |
| M6 | Trading a symbol the owner did not fund with a lot | no `LOT_<SYMBOL>` ⇒ symbol never trades | config test |
| M7 | Live account traded while the owner expected paper | `TRADING_MODE=paper` default + `CONFIRM_LIVE_ACCOUNT` for live-environment tokens + start-up message | config tests |
| M8 | Runaway loop of losing trades | max trades/day, consecutive losses, daily −R limit, kill switch | guard tests |
| M9 | Bot fights the owner's manual changes | only MAPEX-labelled positions, adopt manual SL/TP after 60 s | broker tests 10, 11 |
| M10 | Order placed on a stale/foreign price | quote freshness ≤ 5 s, spread cap, `MARKET_RANGE` slippage bound | guard tests |
| M11 | Entry outside the killzone / during lunch / after 15:50 NY | P2 gate re-checked at send time | executor test 9 |
| M12 | Weekend or rollover gap | market-hours table, no entries near the daily/weekly close | timeutil tests |

## Analysis correctness

| # | Risk | Mitigation | Proof |
|---|---|---|---|
| A1 | Lookahead in mapper/executor/replay | every reader takes `now`; bars with `t + tf > now` are invisible; explicit test | mapper 9, executor 13 |
| A2 | Forming bar treated as closed | closed-bar rule + 2 s grace | primitives tests |
| A3 | Higher-TF bars aggregated from M1 (wrong D1 boundary) | always fetch each timeframe from the broker | data tests |
| A4 | GEM rule silently dropped or "improved" | `docs/TRACEABILITY.md` + test per GEM section; deviations only in `docs/DECISIONS.md` | traceability test |
| A5 | Absolute price constants from the 3390-gold era applied to BTC | percentage-based constants in `shared-primitives.md` §1 | unit tests both symbols |
| A6 | Score reaching 100 with a partial phase | phases are gates; 100 required; property test that no branch emits CONFIRMED below 100 | executor tests |
| A7 | Zone identity drifting between mapper runs (state lost) | stable `zone_key`; states keyed by it, archived when the zone disappears | state tests |
| A8 | Stale map used for hours | map TTL: no entries on a map older than `MAP_MAX_AGE_H` (default 6) unless re-published | mapper test |
| A9 | Session ATR wrong at session start (no data) | fallback `4 × ATR14(H1)`, logged | unit test |

## Platform / data

| # | Risk | Mitigation | Proof |
|---|---|---|---|
| P1 | cTrader session token expires mid-day | AuthError detection, halt entries, alert with steps, `/ctrader` hot swap | client tests |
| P2 | Batch quote poisoned by an unknown symbolId | validate ids against the cached symbol map | client test |
| P3 | Unsupported period or >720 h window | only the 9 valid periods, chunked history | client tests |
| P4 | Rate limits (50/s general, 5/s historical) | limiter + backoff | client test |
| P5 | Two instances during a Railway deploy | lease row; the loser runs read-only | lease test |
| P6 | No volume attached ⇒ trade history and dedupe lost on redeploy | warning in `/health` + one alert | config test |
| P7 | `/health` failing because cTrader is down blocks deploys | health never depends on the broker | health test |
| P8 | Timezone/DST errors in killzones | `zoneinfo`, `tzdata` dependency, DST tests (March and November) | timeutil tests |
| P9 | Secrets in logs | redaction filter for token/Authorization; token only ever masked | log test |
| P10 | Telegram flood / parse errors | HTML escaping, 4096 split, 429 `retry_after`, outbox dedupe | telegram tests |
| P11 | Clock skew between Railway and the broker | compare quote timestamps, warn > 5 s, block entries > 30 s | client test |

# Broker execution — cTrader Remote MCP (trading profile)

This is the file where a mistake costs real money. Read the official sources first, implement exactly, test every branch.

Official sources (read, do not vendor — Spotware EULA):
`https://help.ctrader.com/ctrader-ai-agent-connect/remote-mcp/` (setup / trading / analysis / faq) and
`https://github.com/spotware/ctrader-skills` → `skills/ctrader-mcp-servers/references/remote-http-server.md`,
`known-quirks.md`, `trader-workflows.md`, `self-healing-playbook.md`, `assets/symbol_precision_table.json`.
Clone to /tmp in the build sandbox and read. If the live `tools/list` disagrees with any fact below, the live
server wins and the difference goes into `docs/DECISIONS.md`.

## 1. Connection and credentials

- The owner copies the configuration from cTrader Web → Settings → **Remote MCP**. It looks like
  `"url": "https://mcp.ctrader.com/trading/mcp", "headers": { "Authorization": "Bearer eyJ…" }` and is generated
  **per trading account** — switching accounts requires copying it again. The `/trading/` path is the trading
  profile; MAPEX needs it (a data-only token cannot place orders).
- The parser must accept: the fragment above **without outer braces**, a full JSON object, a bare token, or
  `Bearer <token>`. Precedence: Telegram `/ctrader …` override in `kv` → `CTRADER_MCP_CONFIG` → `CTRADER_MCP_URL` + `CTRADER_MCP_TOKEN`.
- The token's first dot-separated segment is base64 JSON containing `plant` and `environment`. Decode it **without
  verifying** only to display `demo` / `live` in `/health`, start-up message and `/status`. Never log or send the token;
  mask it as `eyJw…AB12`. Delete the Telegram message that carried it.
- Session/token expiry: 401/403 or an error text matching `unauthori|forbidden|expired|invalid token|session` ⇒
  `AuthError` ⇒ halt new entries, keep managing open positions when possible, one critical Telegram alert with the
  renewal steps, retry every 60 s.

## 2. Units (the classic foot-gun)

| Quantity | Encoding |
|---|---|
| Market data (`get_spot_prices`, `get_trendbars`) | **integer pipettes**: `display = raw / 10^pipDigits` |
| Order DTO prices (`limitPrice`, `stopPrice`, `stopLoss`, `takeProfit`) | **display floats** — never pipettes |
| Relative SL/TP on market orders | **positive integer points** = `distance / 10^-pipDigits` |
| Volume | **integer cents of base asset**: `cents = round(lots × CONTRACT_SIZE[symbol] × 100)` |

Use distinct Python types (`Pipettes = NewType(int)`, `Display = NewType(float)`, `Points = NewType(int)`,
`Cents = NewType(int)`) and convert only in `ctrader/decode.py`. Digits resolution: symbol metadata if present →
`PRICE_DIGITS` override → precision-table default (XAUUSD 3, BTCUSD 2), always validated by decoding a live bid and
checking it against `PRICE_BANDS` (XAUUSD 1500–14000, BTCUSD 25000–240000; bands narrower than 10× so calibration is
unambiguous). A failed calibration disables the symbol and alerts — it never guesses.

Volume checks before every order: `lots` present for the symbol, `0 < lots ≤ MAX_LOT`, `cents ≥ 1`, cents integer,
and `cents` re-derived back to lots within 1e-9. Log the mapping at start-up: "XAUUSD 0.10 lot = 1000 cents = 10 oz".
`CONTRACT_SIZE` is configurable because it is broker-defined; the first live fill verifies it (§5).

## 3. Order flow (CONFIRMED → position)

1. **Guards** (`guards.py`) — all must pass, checked immediately before sending:
   mode (`live`), live-account confirmation, kill switch off, market open, killzone still valid, quote fresh (≤ 5 s),
   spread ≤ `MAX_SPREAD[symbol]`, `MAX_OPEN_TOTAL` / `MAX_OPEN_PER_SYMBOL`, `MAX_TRADES_PER_DAY`,
   `MAX_CONSECUTIVE_LOSSES`, `DAILY_LOSS_LIMIT_R`, no existing trade row with this `setup_key`, no MAPEX position
   already open on this symbol in the opposite direction, SL/TP sane (§Layer 6 of the executor spec).
2. **Claim the setup**: `INSERT INTO trades(setup_key, state='SENDING', …)` — the UNIQUE constraint is the lock.
   If the insert fails, another tick already owns it: do nothing.
3. **Send once**: `create_order` with
   `orderType = MARKET_RANGE` (fallback `MARKET` if the schema or the server rejects it),
   `symbolId`, `tradeSide = BUY|SELL`, `volume = cents`,
   `baseSlippagePrice = decision price`, `slippageInPoints = MAX_SLIPPAGE_POINTS[symbol]`,
   `relativeStopLoss = points(|decision − SL|)`, `relativeTakeProfit = points(|TP_server − decision|)`,
   `label = "MAPEX"`, `comment = setup_key`.
   Relative offsets are mandatory for market orders — absolute SL/TP are rejected there. The position therefore is
   never naked, even for one tick.
4. **On response**: store `order_id`, `position_id`, `executionPrice`, filled volume; state `OPEN`.
5. **On timeout / transport error: never resend.** Poll `get_positions` (and `get_order_history` if needed) filtered
   by `label`/`comment == setup_key` with backoff (1, 2, 5, 10, 20 s, up to 2 min). Found → adopt it. Not found after
   the window → state `FAILED`, kill switch ON, critical alert. A duplicate order is worse than a missed trade.
6. **Exactify protection**: once the fill price is known, `amend_position(positionId, stopLoss=<display SL>,
   takeProfit=<display TP_server>)` — **always send both fields; omitting one removes it**. Then read back with
   `get_position_details` and assert SL/TP within one tick of the intended values and side/volume as expected.
7. **Verification failure** (no SL, wrong side, volume mismatch beyond 1 cent): try one amend; still wrong →
   `close_position(positionId, volume=<full>)`, kill switch ON, critical alert.
8. Everything (request, redacted response, timings) goes to `order_log`.

## 4. Position management (only positions with `label == "MAPEX"`)

- Loop every 5 s while a MAPEX position is open, driven by the live quote and M1 closes:
  - **TP1**: when bid ≥ TP1 (buy) / ask ≤ TP1 (sell): `close_position(positionId, volume = round_to_step(cents × TP1_CLOSE_PCT%))`,
    then `amend_position(stopLoss = entry_fill, takeProfit = TP_server)` (breakeven, both fields). Mark `partial_done`,
    `be_done`. Never move the stop before TP1 (GEM2 Layer 5).
  - **Level 3 thesis invalidation** does not close positions; SL/TP stand.
  - **Manual interference**: if the position's SL/TP differ from MAPEX's values and MAPEX did not change them
    (after the first 60 s), adopt the owner's values and stop amending; log it. If a MAPEX position has **no** SL at all,
    send one critical alert — do not fight the owner.
  - **Position gone** (closed by SL/TP/owner): read the closing deal (`get_deals` / `get_position_details`), compute
    `result_r = (exit − entry)/risk` signed by side, update counters (consecutive losses, daily R), close the trade row.
    `get_deals` propagation lags — retry for up to 60 s before falling back to "closed, result unknown".
- **Start-up reconciliation**: load MAPEX positions from the broker, match them to `trades` by `comment`; adopt
  orphans (position without a row) as `OPEN` with unknown setup and only manage their protection; rows without a
  position become `CLOSED`.

## 5. First-fill sanity (contract-size and encoding proof)

On the **first live fill of each symbol**: compare the broker's reported position volume with the intended cents.
Mismatch by more than one step ⇒ `close_position` immediately, kill switch ON, critical alert naming the expected and
actual volume (this is the protection against a wrong `CONTRACT_SIZE` turning 0.10 lot into 10 lots).
The same check runs on every fill, cheaply.

## 6. Paper mode (`ctrader/paper.py`, default)

Same interface as the live broker, same guards, same state machine, same Telegram messages prefixed `📝 PAPER`:
fills at the current ask (buy) / bid (sell) with `MAX_SLIPPAGE_POINTS` ignored, SL/TP tracked against M1 bars
(same-bar SL and TP ⇒ SL first), partial close and breakeven simulated. Paper and live must share every line of
decision code — the only difference is which broker object is injected.

## 7. Kill switch and circuit breakers

- `/stop` → no new entries (management continues) · `/resume` → back on · `/flat` → close every MAPEX position now
  (requires a confirmation reply `/flat yes`).
- Automatic kill switch ON: order verification failure, volume mismatch, repeated `AuthError` (> 15 min),
  `MAX_CONSECUTIVE_LOSSES`, `DAILY_LOSS_LIMIT_R`, price-decoding calibration failure, DB write failure.
  Every automatic trip sends a critical Telegram alert and requires `/resume`.
- Daily counters reset at 17:05 NY.

## 8. Tests (fake broker that records every call)

1. Happy path: one `create_order` with correct volume cents, relative SL/TP points, label/comment; one `amend_position`
   with both absolute levels; verification read-back.
2. Timeout after send → no second `create_order`; reconciliation adopts the position found by `comment`.
3. Rejection (invalid volume / market closed) → state FAILED, no retry loop, alert.
4. Verification shows no SL → one amend attempt → still missing → `close_position` + kill switch.
5. Volume mismatch (contract-size error simulated ×100) → immediate close + kill switch.
6. TP1 partial: correct volume rounding, breakeven amend sends both fields.
7. `amend_position` called with only one field anywhere in the codebase → static test fails (grep + unit).
8. Guards: each breaker blocks entry independently; duplicate `setup_key` insert blocked by the DB.
9. Restart mid-trade: reconciliation rebuilds state, no duplicate order, management resumes.
10. Manual close/modify by the owner is respected (no re-amend, no reopen).
11. Only `label == "MAPEX"` positions are ever amended or closed (fixture includes a foreign position).
12. Paper vs live parity: the same fixture produces identical decisions and identical intended orders.

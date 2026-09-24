# Shared primitives — exact definitions (used by both GEM specs)

The GEM prompts were written for a human reading charts. Code needs numbers. Everything ambiguous is pinned here
once; both specs reference these names. All constants live in `config.py` and are overridable per symbol.

## 1. Price-unit normalisation

GEM1 uses absolute "price units" from an XAUUSD ≈ 3390 era ("within 1 price unit", "5-price-unit zone").
Convert to percentages of current price so the same code works for BTCUSD:

| Name | Value | Origin |
|---|---|---|
| `EQ_TOL_PCT` | 0.10 % of price | GEM1 §0B "tolerance: 0.1% of price" |
| `TOUCH_TOL_PCT` | 0.03 % of price | GEM1 §0D "wick within 1 price unit" (1 / 3390) |
| `CLUSTER_WIDTH_PCT` | 0.15 % of price | GEM1 §0B "5 price units" (5 / 3390) |
| `tick` | 10^−`display_decimals` | XAUUSD 0.01, BTCUSD 0.01 |
| `level_tol(tf)` | `max(TOUCH_TOL_PCT × price, 0.15 × ATR14(tf), 3 × tick)` | matching a price to a level |

## 2. Candles and time

- Candle `{t (open, UTC epoch s), o, h, l, c}`; **closed** iff `now ≥ t + tf + 2 s` and fetched after that moment.
  Never evaluate on a forming bar (in replay: never read a bar with `t + tf > simulated_now` — the no-lookahead rule).
- Timeframes from the broker: M1, M5, M15, M30, H1, H4, D1, W1, MN1 (no others exist on the Remote server).
- `body_top=max(o,c)`, `body_bot=min(o,c)`, `body=body_top−body_bot`, `range=h−l` (range 0 ⇒ treat ratios as 0).
- New York time via `zoneinfo("America/New_York")`; all windows are half-open `[start, end)` on the bar's **close** time.
- Sessions (GEM1 §1, as written): Asia range 19:00–21:00, London 02:00–05:00, NY AM 07:00–10:00,
  NY Lunch 12:00–13:30, NY PM 13:30–16:00. Session label for `current_session`: 19:00–02:00 Asia,
  02:00–07:00 London, 07:00–19:00 New York.
- GEM2 killzones (§P2): London 02:00–05:00, New York 08:30–11:00, Silver Bullet 10:00–11:00 and 14:00–15:00,
  Classic FX SB 03:00–04:00. Macro windows: `[HH:50, HH+1:10]`, last hour 15:00–16:00.
- The owner's full ICT clock (ICT Sniper V13, D-72), for Gemini's mapping context only (entries stay in the GEM2
  killzones above). The code copy is `mapex/mcp_api.py::ICT_TIMES`; `market_snapshot` returns `ict_now` / `ict_next`.
  The same table is in `docs/gemini/GEM1_SPARK_SKILL.md` §5, so change all three together:
  - Asian Range 19:00–00:00 (M5 only) · London Opening Range 01:30–02:00 · London Open KZ 02:00–05:00 · London
    Silver Bullet 03:00–04:00 · European Open ref 06:00 · 6:30 ref · NY Opening Range 07:00–07:30 · NY Open KZ
    07:00–10:00 (Judas 09:30–10:00) · Equities OR 09:30–10:00 · London Close 10:00–12:00 · AM Silver Bullet
    10:00–11:00 · NY Lunch 12:00–13:00 (no trade) · PM OR 13:30–14:00 · PM Session 13:30–16:00 · PM Silver Bullet
    14:00–15:00 · Last Hour 15:00–16:00.
  - Macros are the stated time ±10 min: 02:33, 04:03, 08:00, 09:00, 10:00, 11:00, 12:00, 13:20, 15:00, and
    15:15 / 15:40 / 15:50 / 16:00.
- Market hours: XAUUSD closed Fri 17:00 → Sun 18:00 NY and daily 17:00–18:00; BTCUSD 24/7 (entries still only in
  the killzones above). No entries within `NO_ENTRY_BEFORE_CLOSE_MIN` (default 30) of a session close for XAUUSD.

## 3. ATR and session ATR

- `ATR14(tf)` = mean of the last 14 true ranges of closed bars, `TR = max(h−l, |h−prev_c|, |l−prev_c|)`.
- `session_ATR` = mean of `(high − low)` of the **current named session** over its last 10 occurrences, computed
  from M15 bars. Fallback when fewer than 5 occurrences: `4 × ATR14(H1)`. Used for GEM1 `BEING_TARGETED`
  (0.5 × session_ATR), `intraday_reachability_filter` (0.4 × session_ATR) and GEM2 TP reachability.

## 4. Swings and structure

- **Swing high** (strict 3-bar, GEM2 "swing-nesting"): `h[i] > h[i−1] and h[i] > h[i+1]`; swing low mirrored.
  Requires bar `i+1` closed.
- **Major swing** (GEM1 "old high/low", "multi-swing extreme"): a swing high that is higher than the previous two
  and the next two swing highs on that timeframe (mirrored for lows). Used for dealing ranges and old highs/lows.
- **BOS / MSS** = a bar whose **body closes** beyond the most recent confirmed swing in the relevant direction.
  A wick-only break is never a BOS/MSS (GEM2 P3 State 2: wick-only = fake MSS).
- **CISD anchor** = the **open** of the first candle of the last consecutive opposite-close run immediately before
  the displacement (GEM1 "Open of decisive candle"; GEM2 Layer 2 anchoring rule).
- **Market structure direction** on a timeframe: bullish if the last two confirmed major swings are
  higher-high + higher-low, bearish if lower-high + lower-low, otherwise the direction of the most recent BOS.

## 5. Displacement

`is_displacement(candle, tf)` = `body ≥ DISP_BODY_ATR(1.0) × ATR14(tf)` **and** `body/range ≥ 0.60`
("large body, shallow wicks") **and** the candle is the middle bar of a new FVG in its own direction.

Displacement **quality** (GEM1 Step 7 scoring):
- `LARGE` (13 pts): FVG created and the displacement leg (the run of same-direction bars starting at this candle,
  max 3) covers `≥ 3 × ATR14(tf)`.
- `MODERATE` (9 pts): FVG created and `body ≥ 1.0 × ATR14(tf)`.
- `SMALL` (4 pts): anything else.

## 6. PD array detectors (one function per type, all return zone low/high, anchor, formed_at, direction)

- **FVG** (3 bars, strict, no body overlap): bullish `l[m+1] > h[m−1]` ⇒ zone `[h[m−1], l[m+1]]`; bearish
  `h[m+1] < l[m−1]` ⇒ zone `[h[m+1], l[m−1]]`. Anchor = `CE = (low+high)/2`. `formed_at = t[m]`.
- **IFVG** (GEM1 definition): an FVG that price has re-entered and **respected** — no body close beyond CE against
  its direction since it was tapped. Anchor = CE.
- **BPR**: an FVG overlapped by an opposite-direction FVG; zone = the overlap; invalidated when a body traverses
  the full range (body close beyond both edges in sequence).
- **VI** (volume imbalance): adjacent bars whose bodies do not touch while wicks overlap —
  bullish `body_bot[i] > body_top[i−1]` and `l[i] ≤ h[i−1]`; zone between the bodies.
- **VOID / VACUUM**: `VACUUM` = a true price gap between adjacent bars (`l[i] > h[i−1]` or `h[i] < l[i−1]`);
  `VOID` = an FVG whose height `≥ 1.0 × ATR14(tf)` and which is still unfilled.
- **OB**: the run of opposite-close candles immediately before a displacement that also produces a BOS/MSS.
  Zone = high→low of that run; anchor = CISD open. Bullish OB additionally requires an FVG within the 3 bars after it
  (GEM1 §PD "Imbalance must exist nearby").
- **BB (breaker)**: an OB that was invalidated by a body close through it and then reclaimed by displacement in the
  opposite direction; the failed OB zone becomes the breaker, anchor = CISD open of the reclaiming displacement.
  (Interpretation of GEM1's "High→Low→Higher High" pattern — record it in `docs/DECISIONS.md`.)
- **RB (rejection block)**: a down-close candle whose high is broken and closed above by the next candle (bullish;
  mirrored bearish). Zone = the candle's wick on the rejection side, anchor = CISD open.
- **PB (propulsion)**: a single body ≥ 1.5 × ATR14(tf) inside a displacement leg; mean threshold 50 % of its body.
  Detected for logging/trailing only.
- **Mitigation state**: a zone is `unmitigated` while no later bar has traded into it (bullish zone: no `l ≤ zone_high`).
  `mitigated_at` recorded otherwise.

## 7. Liquidity levels (shared by GEM1 registry and GEM2 raid detection)

- `EQH/EQL`: 2+ swing highs (lows) within `EQ_TOL_PCT` of each other → level = their mean, `touches = count`.
- `PDH/PDL`, `PWH/PWL`, `PMH/PML`: extremes of the previous closed D1 / W1 / MN1 bar.
- Session highs/lows: extremes of each named session window (today and the previous 3 days), from M15 bars.
- Swing H/L: every strict swing on the timeframe; "old" if age > 5 trading days.
- Algo magnet: a level (cluster of swing extremes within `EQ_TOL_PCT`) touched ≥ 3 separate times, where a touch is
  a wick within `level_tol` and touches are separated by at least 3 bars.
- Resting liquidity: an untouched pool older than 3 sessions.
- Engineered liquidity: EQH/EQL formed within 10 bars after an opposite-direction sweep.
- Range liquidity: swing extremes strictly inside the active D1 dealing range that are not major swings.
- **Sweep classification** for a level L: `SWEPT_BODY` if any later bar closes beyond L; else `SWEPT_WICK` if any
  later wick reaches within `TOUCH_TOL_PCT × price` beyond or at L; else `UNTOUCHED`. `BEING_TARGETED` (secondary)
  when `|price − L| ≤ 0.5 × session_ATR`.

## 8. Data integrity rules

- Every timeframe series must be contiguous; a missing bar is refetched once, then the tick is skipped and logged.
- Higher-timeframe bars are taken from the broker, never aggregated from M1 (broker day/week boundaries differ).
- All decisions use **bid** candles; entries use ask (buy) / bid (sell); spread = ask − bid from the live quote.
- A quote older than 5 s is stale: no new entries, no state transitions that depend on live price.

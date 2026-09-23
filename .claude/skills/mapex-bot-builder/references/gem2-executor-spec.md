# GEM 2 — LTF Execution Engine as code (`mapex/executor/`)

Source of truth: `references/source/GEM2.md`. The executor never re-derives bias, never re-scores liquidity, never
reassigns chains (GEM2 LAYER 0). It consumes the Strategic Map and decides: **WAIT / CONFIRMED / RESET / MONITOR /
NO-SETUP**. Only CONFIRMED reaches the broker.

Tick: on every closed M1 bar (plus M5 and M15 closes) for each symbol that has a valid map, for CHAIN_A and CHAIN_B
in parallel (each zone carries its own state row). Live quote must be fresh (≤ 5 s).

## Preflight

Map exists and `valid`, `active_causal_chain.root_liquidity_id` not null, candles contiguous, quote fresh,
market open. Otherwise `NO-SETUP` (`error_code 1001`) without touching state.

## Module 5 — HTF thesis anchor (per zone, computed once per map)

`THESIS_ROOT` = registry[generating_liquidity_id].price_level · `THESIS_ANCHOR` = the zone (low/high/anchor) ·
`THESIS_PDA_LPS` = inherited confidence · `thesis_invalidation_level` = THESIS_ROOT.
`thesis_status` ∈ ACTIVE | PRESERVED | MONITOR | INVALIDATED | STALE, evaluated at every D1 close and every tick:

- **P1 preservation**: no D1 **body close** beyond THESIS_ROOT against the bias.
- **P2**: no D1 structural break against the bias (D1 body close beyond the previous D1 major swing on the opposite side).
- **P3**: fewer than 3 full sessions since the map was published without price reaching THESIS_ANCHOR (else `STALE`).

## P1 — Macro map confirmation [25]

25 pts when: map bias is set, `thesis_status` ∈ {ACTIVE, PRESERVED}, and M15 structure does not contradict the thesis —
i.e. there is no M15 BOS **against** the bias with displacement after the current raid began. A contradiction is a
Level-2 event (MONITOR), not an invalidation (GEM2 P1 "THESIS LINKAGE"). SMT is confluence only and is **not
implemented** (always `NO SMT VISIBLE`; logged in `docs/DECISIONS.md`) because it cannot gate anything.

## P2 — Bellwether M15 [25]

All three must hold (each 25/3 for logging; the phase is a gate):
1. **Killzone**: tick time inside London 02:00–05:00, NY 08:30–11:00, or PM Silver Bullet 14:00–15:00 NY.
   Never inside NY Lunch 12:00–13:30. No new entries after 15:50 NY.
2. **Judas / PO3**: `judas_threshold` = NY-midnight open before 08:30, else the lower (buy) / higher (sell) of the
   NY-midnight open and the 08:30 open. Satisfied when the raid extreme (Module 6 `last_sweep_level`) is beyond that
   threshold against the bias — i.e. the day's manipulation leg has already happened and the entry originates after it.
   Alternative accepted form during the NY killzone: the raid took out the London range extreme on the against-bias side.
3. **DOL alignment**: `primary_dol` / `final_lrlr_objective` lies in the trade direction beyond the zone.

## P3 — Sniper M5/M1 [25] with Module 6 (sweep counter)

State machine per zone: `WATCH → RAID → SHIFT → GAP → RETURN`.

- **State 1 RAID**: on a closed M15 or M5 bar, a sweep of an LTF pool (M5/M15 swing STH/STL, EQH/EQL, session H/L,
  NY-Lunch H/L, PDH/PDL) located at or near the zone (`|level − zone edge| ≤ 0.1 × session_ATR` or inside the zone),
  on the against-bias side. Record `last_sweep_level`, `last_sweep_type` = `WICK_ONLY` (wick beyond, close back inside)
  or `BODY_CLOSE`, and `sweep_count += 1`.
  **Module 6**: evaluate displacement within 3 bars of the sweep on the sweep timeframe.
  - displacement found → `DISPLACEMENT_CONFIRMED` → State 2.
  - not found and `sweep_count ≤ 2` → RESET (thesis preserved), stay in WATCH awaiting the next sweep.
  - not found and `sweep_count ≥ 3` → Module 8 Level 2 → **MONITOR**: no further WAIT for this zone until a new map
    is published.
- **State 2 SHIFT (MSS)**: an M5 (or M1) displacement candle whose **body closes** beyond the relevant swing —
  for a buy, the last M5 swing high of the down-leg into the raid. A wick-only break = fake MSS → Module 8 Level 1 → RESET.
- **State 3 GAP**: the displacement leaves a real FVG (strict, primitives §6). An FVG located inside an existing BPR
  is not a valid anchor → RESET.
- **State 4 RETURN**: price returns to the FVG edge or its CE (or to the OB CISD anchor for OB setups). No chasing:
  if price never returns within P4.3's window the setup dies (see below).

Type 11 vs Type 7 (P4.1) governs which raids can ever produce an entry: **only a `BODY_CLOSE` sweep whose next bar
closes back inside in the bias direction (Type 7) plus displacement is tradable**. A `WICK_ONLY` sweep always routes
to RESET and waits for the next contact (Multi-Sweep Protocol conditions A–D).

## P4 — The Iron Gate [25]

- **P4.1** Type 7 + displacement (above).
- **P4.2 Displacement footprint**: within the 3 M1 bars starting at the first M1 bar that closes beyond the MSS
  swing level: at least one M1 FVG in bias direction **and** an M1 body ≥ 1.5 × the median M1 body of the last 20 bars.
  Missing → `no_displacement_print` → RESET (sweep_count logic applies).
- **P4.3 Retest-to-anchor**: price returns to the FVG CE or the CISD anchor within 15 M1 bars of the displacement.
  Missing → `no_retest_window` → RESET if `sweep_count ≤ 2`, else MONITOR.
- **P4.4 Defense on retest**: the retest bar (or the next M1 bar) has `body ≥ 0.60 × range`,
  `wick_against_bias ≤ 0.35 × range`, and closes in bias direction beyond the anchor / FVG 50 %.
  A body close beyond the anchor against the trade → `anchor_not_defended` → RESET.

**Module 7 output**: `WAIT` (P1–P3 full, P4 pending — one single condition, stored and shown by `/status`),
`CONFIRMED` (all four phases full → order), `RESET` (Level-1 noise, thesis preserved, state returns to WATCH with
`sweep_count` kept). MAPEX does not message the owner on WAIT/RESET (he asked for entry notifications only) but
every transition is written to `events`.

## Module 8 — Invalidation engine (sole authority on thesis status)

- **Level 1 NOISE → RESET**: fake MSS, wick-only CISD probe, sweep without displacement, `anchor_not_defended`,
  `no_retest_window` with `sweep_count ≤ 2`, FVG fully closed after displacement (`NOT_BREAKAWAY_RETRY`).
- **Level 2 WARNING → MONITOR**: `sweep_count ≥ 3` without displacement · a new map contains an opposing chain whose
  generating LPS ≥ 80 against this thesis · 3 sessions without reaching THESIS_ANCHOR (STALE).
  Effect for MAPEX: this zone stops producing entries until a fresh map re-creates it.
- **Level 3 BREAK → INVALIDATED**: D1 body close beyond THESIS_ROOT against bias (`error_code 3002`) or D1 structural
  break against bias (`3001`). Effect: zone dead, all pending states cleared, mapper re-run forced.
  Open positions are **not** force-closed by Level 3 — their stop loss and take profit stand
  (documented decision; the bot does not improvise exits).

## Scoring

`p1 + p2 + p3 + p4`, each 0–25, partial credit for logging only. **CONFIRMED requires exactly 100.** Anything else is
WAIT / RESET / MONITOR / NO-SETUP. The score and every sub-check land in `events` so any decision can be replayed.

## Layer 6 — Fiduciary stop loss (`stoploss.py`)

1. `anchor`: if the raid wick exceeded the tactical swing → `anchor_type = SWEEP`, anchor = the sweep extreme
   (lowest low for buy). Otherwise structural: OB CISD open or FVG CE.
   Optional refinements (M1 wick imbalance edge, extreme micro-OB) only when they are closer to price than the sweep
   extreme **and** still beyond the retest low: `EXTREME_REFINEMENT` / `EXTREME_MICRO_OB`.
   Long-wick alternative: when the sweep wick is longer than `2 × ATR14(M5)`, anchor = wick CE (GEM2 3B).
2. `spread_buffer = live_spread + safety_buffer`, `safety_buffer = max(ceil(spread/tick × 0.05) × tick, SL_SAFETY_BUFFER[symbol])`.
3. `SL = anchor − spread_buffer` (buy) / `anchor + spread_buffer` (sell).
4. **Dangerous-trap rejection**: if any registry or LTF pool level (EQH/EQL, session H/L, PDH/PDL) lies within
   `level_tol(M5)` of the SL → `NO-SETUP` (do not move the stop to "fix" it).
5. Sanity: `risk = |entry − SL|` must satisfy `risk ≥ 4 × spread` and `risk ≤ 0.5 × session_ATR`, otherwise NO-SETUP.

## Layer 5 — Targets (`targets.py`)

- `R = risk`. Candidate objectives in trade direction, nearest first: session H/L, PDH/PDL, EQH/EQL, opposing
  FVG/OB edges, registry pools.
- `TP1` = the first candidate with `r_multiple ≥ 3.0` **and** distance ≤ `0.4 × session_ATR`. Nearer candidates with
  `r < 3` become `management_level` (FTA), never TP1.
- If no candidate qualifies, use the SD projection of the manipulation leg (raid extreme → displacement extreme):
  TP1 ≈ −2.0 SD, TP2 ≈ −2.5 SD, TP3 ≈ −4.0 SD. Re-apply the ≥ 3R and reachability test.
- Still nothing → `tp_constraints_not_met` → NO-SETUP (GEM2 guardrail).
- `TP2` = next objective with `r ≥ 3.5`; `TP3` = `final_lrlr_objective` when `r ≥ 4.5`.
- Guardrail: every TP distance ≥ `10 × live_spread`.
- Order placement: server-side TP = TP2 when it exists, else TP1. Pay-the-Trader is executed by the bot:
  at TP1 close `TP1_CLOSE_PCT` of the volume and move the stop to entry (breakeven) — never before TP1.

## Alpha pick (Layer 10)

When CHAIN_A and CHAIN_B both reach CONFIRMED in the same tick, take: (1) higher TP1 R-multiple, (2) higher
`root_lps`, (3) CHAIN_A. One trade per tick, and the guards in `broker-execution.md` still apply.

## Strategy labels

`strategy_used[]` and `suggested_pearl` are derived deterministically for logging/Telegram only, from the sequence
that fired: sweep of an obvious pool + reversal + BOS → `CRT (Turtle Soup) Liquidity Sweep`; entry at OB retest →
`Order Block Mitigation (OBM)`; entry at FVG CE after displacement → `Displacement + Return to Origin (DRO)`;
EQH/EQL cluster raid → `Magnetized Liquidity Cluster (MLC)`; inside a Silver Bullet window → `SILVER_BULLET`.
Labels never influence the decision.

## 10. Required tests (synthetic M1/M5/M15 fixtures + a fake map)

1. Clean buy: Type 7 raid at CHAIN_A, displacement, M5 MSS body close, FVG, retest to CE, defense candle →
   score 100 → exactly one order request with the expected SL/TP.
2. Wick-only raid → RESET, `sweep_count = 1`, no order; second Type 7 raid + displacement → entry allowed.
3. Fake MSS (wick break) → RESET, no order.
4. Third sweep without displacement → MONITOR; further ticks produce no WAIT and no order until a new map.
5. `anchor_not_defended` (body closes back through CE) → RESET, no order.
6. No retest within 15 M1 bars → RESET (sweep_count ≤ 2) / MONITOR (≥ 3).
7. TP: nearest pool at 2.4R → becomes management_level; next pool at 3.2R within reachability → TP1; nothing
   reachable → NO-SETUP `tp_constraints_not_met`.
8. Dangerous trap: SL sitting on PDL → NO-SETUP.
9. Killzone gates: identical setup at 11:30 NY (outside) → no entry; at 14:20 NY (PM SB) → entry.
10. Judas: raid that does not exceed the NY-midnight/08:30 threshold → P2 fails → no entry.
11. Thesis: D1 body close beyond THESIS_ROOT → INVALIDATED, zone cleared, mapper re-run requested.
12. Alpha pick with two confirmed chains → one order, the better R.
13. Determinism + no-lookahead: replaying the same fixture twice gives identical events; the engine never reads a
    bar whose close time is after the simulated tick.

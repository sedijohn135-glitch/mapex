# GEM 1 — HTF Structure Mapper as code (`mapex/mapper/`)

Source of truth: `references/source/GEM1.md`. Read it alongside this file. Every step below keeps GEM1's names so
`docs/TRACEABILITY.md` can map them 1:1. Primitives come from `shared-primitives.md`.

Input per symbol: closed candles D1 (250), H4 (250), H1 (300), M15 (400, for session ranges), W1 (60), MN1 (24),
live quote, `session_ATR`. Output: one **Strategic Map** object serialised exactly in GEM1's LAYER 4 JSON schema,
stored in `maps`. Run at every H1 close +60 s and at start-up.

## Step 0 — Liquidity discovery engine (`liquidity.py`)

0A/0B/0C: build `liquidity_registry[]` from D1 → H4 → H1 (in that order; `id = LIQ_001…` in discovery order) using
the level detectors of `shared-primitives.md` §7: old highs/lows, PDH/PDL, PWH/PWL, PMH/PML, session H/L, swing H/L,
algo magnets, EQH/EQL, range liquidity, clusters, engineered, resting, voids and vacuums. Deduplicate levels that are
within `EQ_TOL_PCT` **within the same timeframe** (keep the highest-priority type, keep both type labels).

0D — status per pool: `UNTOUCHED | SWEPT_BODY | SWEPT_WICK | BEING_TARGETED | PDA_GENERATING` (§7 rules;
`PDA_GENERATING` is set later by Step 7 when the pool is a chain root).

0E — **LPS** (integer 0–100), exactly GEM1's arithmetic:

```
base   = 60 (D1) | 40 (H4) | 20 (H1)
mult   = PMH/PML 1.00 · PWH/PWL 0.95 · old swing (age>5 trading days) 0.92 · EQH/EQL 3+ touches 0.88 ·
         algo magnet 0.85 · PDH/PDL 0.83 · session H/L 0.78 · resting >3 sessions 0.75 · EQH/EQL 2 touches 0.72 ·
         engineered 0.65 · range liquidity 0.62 · void/vacuum 0.70
bonus  = +12 multi-TF confluence (same level within EQ_TOL_PCT on 2+ timeframes)
         +10 status UNTOUCHED
         +8  cluster member (3+ pools within CLUSTER_WIDTH_PCT)
         +5  level inside an active HTF PDA zone
         capped at +35
lps    = min(100, round(base * mult + bonus))
importance = CRITICAL ≥80 | HIGH 65–79 | MEDIUM 50–64 | LOW <50
```

When a pool matches several types, use the **highest** multiplier and keep every label in `label`.
LOW pools are excluded from chain assignment (they stay in the registry).

## Step 1 — Session context

`current_session`, `current_local_time` (report NY time and the owner's CET for the brief), `session_atr`,
`intraday_reachability_filter = 0.4 × session_ATR`.

## Step 2 — Structural context

- **2A Dealing range**: latest confirmed D1 major swing high and major swing low → BSL/SSL boundaries,
  equilibrium = midpoint, `pd_status` of any price = Premium above / Discount below / Equilibrium within `level_tol(D1)`.
- **2B Strategic bias** (liquidity-first, no independent derivation):
  1. `top_up` = highest-LPS pool above price with status UNTOUCHED or BEING_TARGETED; `top_down` = same below.
  2. `bias_candidate` = direction of the higher LPS (`buy` if above wins, `sell` if below wins).
  3. Tie (|Δ| ≤ 10): prefer the pool from the dominant timeframe (D1 > H4 > H1); still tied → `d1_narrative`;
     still tied → **no map** (`valid=false`, reason `bias_tie`).
  4. `d1_narrative`: bullish if the last closed D1 closes up and above the previous D1 midpoint; bearish mirrored;
     else neutral. If it contradicts `bias_candidate`, record `bias_conflict=true` (GEM1: the conflict is a finding).
  5. Conflict resolution through 2C: if **both** LTH and ITH oppose `bias_candidate` → **no map**
     (`valid=false`, reason `bias_conflict_htf`). Otherwise `strategic_bias = bias_candidate`.
- **2C Bias hierarchy**: `LTH` = D1 market-structure direction (over the last 60 D1 bars, primitives §4);
  `ITH` = direction of the last H4 BOS/MSS that had displacement (else `corrective`);
  `STH` = direction of the last H1 BOS (marked `corrective` when opposite to LTH/ITH).
- **2D PO3 phase**: `Accumulation` while in the Asia label window; `Manipulation` in London/NY while the
  against-bias sweep of the NY-midnight anchor has not happened yet (buy bias: day low still above NY midnight open);
  `Distribution` once it has and price has returned through the anchor in the bias direction.
- **2E Weekly profile**: `Weekly Expansion` if the week's extreme in bias direction is being extended and the
  week open is beyond the opposite extreme; `Weekly Reversal` if the week made its extreme on Tue/Wed and price has
  since closed back through the week open; else `Range`. Informational only.
- **2F Special days**: `inside_day`, `big_event_range` (D1 range ≥ 1.5 × ATR14(D1)), `daily_discount_wick`
  (D1 open within `level_tol(D1)` of the previous close and lower wick ≥ 60 % of range → wick CE into `static_anchors`).

## Step 3 / Step 4 — H4 and H1 structure

H4: dealing range, MSS/BOS with displacement, unmitigated OB/FVG/IFVG/BB, premium/discount, ITH/ITL, session pools.
H1: OB/FVG/IFVG/BPR/RB, STH/STL, EQH/EQL clusters, session ranges (prev PM, London, NY AM), premium/discount inside
the H4 range. Any H1 level not already in the registry is appended (GEM1 Step 4 first line).

## Step 5 — DOL resolution

`primary_dol` = highest-LPS pool with status UNTOUCHED/BEING_TARGETED in the bias direction; `secondary_dol` = next.
`final_lrlr_objective` = the highest-LPS D1/W1 **ERL** pool in the bias direction (fallback: `primary_dol`).
NDOG (previous session close 16:59 NY vs re-open 18:00) and NWOG (Friday close vs Sunday open) become
`static_anchors.opening_gaps[]` with `anchor_type = "STRUCTURAL (FVG)"`. ORG/AOR are RTH-index concepts →
`null` for XAUUSD/BTCUSD (documented deviation). PDH/PDL of the last 3 days are always tagged as named ERL candidates.

## Step 6 — HTF sweep alerts

`htf_liquidity_alerts[]` from registry pools with status UNTOUCHED/BEING_TARGETED and `lps ≥ 50`, sorted by LPS desc
(BEING_TARGETED first at equal LPS), max 8 but all CRITICAL always included. Fields exactly as in GEM1 Step 6.
`alert_trigger` = CROSS_UP for pools above price, CROSS_DOWN below. `reason` is built from a fixed template
(no free text): type, timeframe, age, status, expected displacement, bias alignment.

## Step 7 — Causal chain builder + PDA scoring (`chains.py`)

Candidates: every unmitigated PDA detected on D1/H4/H1 in the `strategic_bias` direction (bullish arrays for buy).

- **Filter 1**: the PDA must be created by a displacement candle (primitives §5). Wick-only breaks are rejected.
- **Filter 2 anchoring**: OB/BB/RB → CISD open; FVG/IFVG/BPR/NDOG/NWOG → CE; liquidity pool → the level itself.
- **1A linkage**: scan the 2 bars immediately before the displacement bar on the same timeframe; find registry pools
  swept there (buy setups sweep sell-side pools, sell setups sweep buy-side). Take the highest-LPS such pool →
  `generating_liquidity_id`, `generating_lps`, `causal_sweep_type` (SWEPT_BODY / SWEPT_WICK). None → `UNLINKED`,
  the PDA is excluded from chains and may appear only in `htf_swing_keylevels[]`.
- **1B confidence** (total 100, exactly GEM1): LPS contribution 42/35/28/15 by LPS tier (tier <50 also blocks CHAIN_A);
  sweep type 15 (body) / 8 (wick); displacement quality 13/9/4 (primitives §5); unmitigated 15; PDA type
  OB|FVG 12, BB|IFVG 10, RB 10 (deviation: GEM1 omits RB — log it), VI|BPR 7; HTF alignment 3 when the PDA direction
  equals LTH or ITH.
- **1C chains**: gates `A ≥ 65`, `B ≥ 50`, `C ≥ 40` on the **generating pool's LPS**; ordering by
  (1) generating LPS, (2) displacement quality, (3) generating timeframe D1>H4>H1, (4) PDA type quality,
  (5) unmitigated. Assign CHAIN_A, then CHAIN_B, then CHAIN_C (C only if a third zone is structurally justified:
  different timeframe or different generating pool).
- `time_horizon` = `INTRADAY` when the zone anchor is within `0.4 × session_ATR` of price, else `SWING_HTF`
  (also `SWING_HTF` when `validation_score ≥ 80`).
- `active_causal_chain` = the chain-A record (root id, root LPS, root price, sweep flags, displacement description,
  `pda_confidence_inherited`, `chain_assigned`). The root pool's status becomes `PDA_GENERATING`.
- `tp1` / `tp2` per zone are **structural references only**: tp1 = nearest opposite-side pool beyond the zone in bias
  direction, tp2 = `primary_dol`. GEM2 recomputes real targets.

## Step 8 — Verification (hard gate)

Run every check in GEM1 Step 8 as assertions. Any failure ⇒ `map.valid = false` with the failing check recorded, and
**no map is published** (the executor keeps the previous valid map only if its zones still exist; otherwise it idles).
Never "fix" a failing map by relaxing a rule.

## Output

The Strategic Map is serialised in GEM1's exact JSON schema (LAYER 4 Part 1) — same keys, same enums, prices as
strings with the symbol's display decimals. Store it in `maps.json`; `docs/` shows one example. Part 2 (the
Liquidity Intelligence Brief) is generated only for `/map` on Telegram and the replay report, from a fixed template.

## 9. Required tests (golden, synthetic candles)

1. LPS arithmetic: table-driven cases for each type multiplier, bonus cap at +35, cap at 100, importance tiers.
2. Registry: EQH detection at 0.1 %, cluster grouping, multi-TF confluence bonus applied on both entries.
3. Sweep status: wick within tolerance → SWEPT_WICK; body close → SWEPT_BODY; untouched stays UNTOUCHED.
4. Bias: candidate above vs below; 10-point tie → dominant TF; full tie → no map; LTH+ITH both opposing → no map.
5. Chains: gate at LPS 65/50/40; UNLINKED PDA excluded; ordering tie-breaks in the documented sequence.
6. Confidence: a fixture that must produce exactly 87/100 with the documented breakdown.
7. Step 8: a deliberately inconsistent map (zone against bias) must publish nothing.
8. Determinism: the same candle fixture run twice produces byte-identical JSON.
9. No-lookahead: the mapper given candles up to T never references a bar with `t ≥ T`.

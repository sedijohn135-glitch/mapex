# TRACEABILITY — GEM section → code → test

Every GEM1/GEM2 section of `docs/source/` maps to real code and a real test. `tests/test_traceability.py` imports
every `code` reference, finds every `test` function, and checks that each required GEM section id is listed.
Deviations and interpretations: `docs/DECISIONS.md`.

## GEM 1 — HTF Structure Mapper

> D-71: with `MAP_SOURCE=gemini` (the default) Gemini runs GEM1 from `docs/gemini/GEM1_SPARK_SKILL.md` and
> `mapex/gemini_map.py::accept` checks its JSON (`tests/test_gemini.py`). The rows below are the built-in mapper,
> used with `MAP_SOURCE=mapex`.

| GEM section | Rule | Code | Test |
|---|---|---|---|
| GEM1 · LAYER 2 | FVG strict, CE anchor | `mapex/core/primitives.py::fvgs` | `tests/test_primitives.py::test_fvg_strict_and_displacement` |
| GEM1 · LAYER 2 | IFVG respected at CE | `mapex/core/primitives.py::ifvgs` | `tests/test_primitives.py::test_mitigation_and_ifvg` |
| GEM1 · LAYER 2 | BPR overlap, body-to-body invalidation | `mapex/core/primitives.py::bprs` | `tests/test_primitives.py::test_bpr_overlap` |
| GEM1 · LAYER 2 | VI, VOID, VACUUM | `mapex/core/primitives.py::volume_imbalances` | `tests/test_primitives.py::test_vi_vacuum_void` |
| GEM1 · LAYER 2 | VOID (unfilled FVG ≥ ATR) | `mapex/core/primitives.py::voids` | `tests/test_primitives.py::test_vi_vacuum_void` |
| GEM1 · LAYER 2 | VACUUM (true gap) | `mapex/core/primitives.py::vacuums` | `tests/test_primitives.py::test_vi_vacuum_void` |
| GEM1 · LAYER 2 | OB: BOS/MSS + FVG, CISD open | `mapex/core/primitives.py::order_blocks` | `tests/test_primitives.py::test_order_block_needs_bos_and_fvg` |
| GEM1 · LAYER 2 | CISD anchor | `mapex/core/primitives.py::cisd_anchor` | `tests/test_primitives.py::test_cisd_anchor_is_open_of_first_opposite_candle` |
| GEM1 · LAYER 2 | BB failed OB | `mapex/core/primitives.py::breaker_blocks` | `tests/test_primitives.py::test_rejection_block_and_breaker` |
| GEM1 · LAYER 2 | RB | `mapex/core/primitives.py::rejection_blocks` | `tests/test_primitives.py::test_rejection_block_and_breaker` |
| GEM1 · LAYER 2 | PB mean threshold | `mapex/core/primitives.py::propulsion_blocks` | `tests/test_primitives.py::test_propulsion_block_inside_displacement_leg` |
| GEM1 · LAYER 2 | IRL ↔ ERL | `mapex/mapper/liquidity.py::Pool.erl` | `tests/test_mapper.py::test_pool_erl_irl` |
| GEM1 · STEP 0A | external scan D1→H4→H1 | `mapex/mapper/liquidity.py::discover_tf` | `tests/test_mapper.py::test_eqh_detection_cluster_and_confluence` |
| GEM1 · STEP 0B | EQH/EQL 0.1 %, clusters | `mapex/core/levels.py::group_equal` | `tests/test_levels.py::test_equal_levels_at_point_one_percent` |
| GEM1 · STEP 0B | algo magnets (3+ touches) | `mapex/core/levels.py::touch_count` | `tests/test_levels.py::test_touch_count_needs_separation` |
| GEM1 · STEP 0B | same-TF de-duplication | `mapex/mapper/liquidity.py::dedupe` | `tests/test_mapper.py::test_eqh_detection_cluster_and_confluence` |
| GEM1 · STEP 0C | voids / vacuums as pools | `mapex/mapper/liquidity.py::discover_tf` | `tests/test_primitives.py::test_vi_vacuum_void` |
| GEM1 · STEP 0D | sweep status | `mapex/core/levels.py::sweep_status` | `tests/test_levels.py::test_sweep_status_rules` |
| GEM1 · STEP 0D | status per pool, BEING_TARGETED | `mapex/mapper/liquidity.py::classify` | `tests/test_mapper.py::test_sweep_status_classification` |
| GEM1 · STEP 0E | LPS formula, cap 35/100 | `mapex/mapper/liquidity.py::lps_score` | `tests/test_mapper.py::test_lps_arithmetic` |
| GEM1 · STEP 0E | bonuses | `mapex/mapper/liquidity.py::score` | `tests/test_mapper.py::test_eqh_detection_cluster_and_confluence` |
| GEM1 · STEP 0E | importance tiers | `mapex/mapper/liquidity.py::importance` | `tests/test_mapper.py::test_lps_capped_at_100_and_importance` |
| GEM1 · STEP 1 | session, session_ATR, reachability | `mapex/core/levels.py::session_atr` | `tests/test_levels.py::test_session_atr_fallback_and_normal` |
| GEM1 · STEP 1 | session label | `mapex/core/timeutil.py::current_session` | `tests/test_timeutil.py::test_sessions_and_trading_day` |
| GEM1 · STEP 2A | D1 dealing range | `mapex/mapper/liquidity.py::dealing_range` | `tests/test_mapper.py::test_dealing_range_and_fallback` |
| GEM1 · STEP 2B | liquidity-first bias | `mapex/mapper/engine.py::derive_bias` | `tests/test_mapper.py::test_bias_tie_dominant_tf_then_no_map` |
| GEM1 · STEP 2B | D1 narrative / conflict | `mapex/mapper/engine.py::d1_narrative` | `tests/test_mapper.py::test_bias_conflict_with_lth_and_ith_gives_no_map` |
| GEM1 · STEP 2C | LTH / ITH / STH | `mapex/mapper/engine.py::bias_hierarchy` | `tests/test_mapper.py::test_bias_hierarchy_and_d1_narrative` |
| GEM1 · STEP 2D | PO3 phase, NY-midnight anchor | `mapex/mapper/engine.py::po3_phase` | `tests/test_mapper.py::test_po3_phase_rules` |
| GEM1 · STEP 2E | weekly profile | `mapex/mapper/engine.py::weekly_profile` | `tests/test_mapper.py::test_weekly_profile_and_special_days` |
| GEM1 · STEP 2F | special days | `mapex/mapper/engine.py::special_days` | `tests/test_mapper.py::test_weekly_profile_and_special_days` |
| GEM1 · STEP 3 | H4 structure + PDAs | `mapex/mapper/chains.py::detect_pdas` | `tests/test_mapper.py::test_confidence_87_breakdown` |
| GEM1 · STEP 4 | H1 structure + PDAs | `mapex/mapper/chains.py::detect_pdas` | `tests/test_mapper.py::test_unlinked_pda_is_excluded_from_chains` |
| GEM1 · STEP 5 | DOL, final LRLR objective | `mapex/mapper/engine.py::build_map` | `tests/test_mapper.py::test_full_map_determinism_and_schema` |
| GEM1 · STEP 5 | NDOG / NWOG | `mapex/mapper/engine.py::opening_gaps` | `tests/test_mapper.py::test_opening_gaps_ndog_nwog` |
| GEM1 · STEP 6 | HTF sweep alerts (max 8, all CRITICAL) | `mapex/mapper/engine.py::build_map` | `tests/test_mapper.py::test_full_map_determinism_and_schema` |
| GEM1 · STEP 7 | Filter 1 displacement | `mapex/core/primitives.py::is_displacement` | `tests/test_primitives.py::test_displacement_rejects_wicky_candle` |
| GEM1 · STEP 7 | Filter 2 anchoring | `mapex/mapper/chains.py::candidates` | `tests/test_mapper.py::test_confidence_87_breakdown` |
| GEM1 · STEP 7 | 1A linkage / UNLINKED | `mapex/mapper/chains.py::link` | `tests/test_mapper.py::test_unlinked_pda_is_excluded_from_chains` |
| GEM1 · STEP 7 | 1B confidence (87/100 fixture) | `mapex/mapper/chains.py::lps_points` | `tests/test_mapper.py::test_confidence_87_breakdown` |
| GEM1 · STEP 7 | displacement quality 13/9/4 | `mapex/core/primitives.py::displacement_quality` | `tests/test_primitives.py::test_fvg_strict_and_displacement` |
| GEM1 · STEP 7 | 1C chain gates 65/50/40 | `mapex/mapper/chains.py::assign_chains` | `tests/test_mapper.py::test_chain_gates` |
| GEM1 · STEP 7 | 1C ordering | `mapex/mapper/chains.py::Candidate.order_key` | `tests/test_mapper.py::test_chain_ordering_tie_breaks` |
| GEM1 · STEP 7 | CHAIN_C structural justification | `mapex/mapper/chains.py::assign_chains` | `tests/test_mapper.py::test_chain_c_needs_structural_distinction` |
| GEM1 · STEP 8 | verification hard gate | `mapex/mapper/engine.py::verify` | `tests/test_mapper.py::test_inconsistent_map_publishes_nothing` |
| GEM1 · LAYER 4 | JSON schema, determinism | `mapex/mapper/engine.py::build_map` | `tests/test_mapper.py::test_full_map_determinism_and_schema` |
| GEM1 · LAYER 4 | Liquidity Intelligence Brief | `mapex/mapper/engine.py::brief` | `tests/test_mapper.py::test_brief_matches_chains` |
| GEM1 · EXECUTION POLICY | no lookahead / same input same output | `mapex/core/primitives.py::closed_bars` | `tests/test_mapper.py::test_mapper_no_lookahead` |

## GEM 2 — LTF Execution Engine

| GEM section | Rule | Code | Test |
|---|---|---|---|
| GEM2 · LAYER 1 | preflight → no-setup 1001 | `mapex/executor/engine.py::preflight` | `tests/test_executor.py::test_preflight_no_setup_without_touching_state` |
| GEM2 · MODULE 5 | THESIS_ROOT / Level-3 break | `mapex/executor/engine.py::thesis_check` | `tests/test_executor.py::test_thesis_invalidated_by_d1_close_past_root` |
| GEM2 · MODULE 5 | P3 expiry (STALE) | `mapex/executor/engine.py::step_zone` | `tests/test_executor.py::test_stale_zone_goes_to_monitor` |
| GEM2 · P1 | macro map confirmation | `mapex/executor/engine.py::score_p1` | `tests/test_executor.py::test_clean_buy_exactly_one_order` |
| GEM2 · P1 | thesis linkage (M15 contradiction = Level 2) | `mapex/executor/engine.py::m15_contradiction` | `tests/test_executor.py::test_m15_contradiction_is_level2` |
| GEM2 · P2 | killzones, lunch, 15:50 | `mapex/core/timeutil.py::killzone` | `tests/test_executor.py::test_killzone_gates` |
| GEM2 · P2 | Judas / 08:30 vs midnight | `mapex/executor/engine.py::judas_threshold` | `tests/test_executor.py::test_judas_threshold_not_exceeded` |
| GEM2 · P2 | bellwether score | `mapex/executor/engine.py::score_p2` | `tests/test_executor.py::test_killzone_gates` |
| GEM2 · MODULE 6 | sweep counter | `mapex/executor/engine.py::watch` | `tests/test_executor.py::test_wick_only_then_type7_entry` |
| GEM2 · MODULE 6 | displacement within 3 bars | `mapex/executor/engine.py::raid` | `tests/test_executor.py::test_third_sweep_monitor_blocks_everything` |
| GEM2 · P3 | State 2 SHIFT, fake MSS | `mapex/executor/engine.py::shift_scan` | `tests/test_executor.py::test_fake_mss_resets` |
| GEM2 · P3 | State 3 GAP, BPR exclusion | `mapex/executor/engine.py::gap_eval` | `tests/test_executor.py::test_no_displacement_print_resets` |
| GEM2 · P3 | State 4 RETURN | `mapex/executor/engine.py::retest_scan` | `tests/test_executor.py::test_clean_buy_exactly_one_order` |
| GEM2 · P4.1 | Type 7 vs Type 11 | `mapex/executor/engine.py::raid` | `tests/test_executor.py::test_wick_only_then_type7_entry` |
| GEM2 · P4.2 | displacement footprint | `mapex/executor/engine.py::gap_eval` | `tests/test_executor.py::test_no_displacement_print_resets` |
| GEM2 · P4.3 | retest within 15 M1 bars | `mapex/executor/engine.py::retest_scan` | `tests/test_executor.py::test_no_retest_window_reset_then_monitor` |
| GEM2 · P4.4 | defense on retest | `mapex/executor/engine.py::defended` | `tests/test_executor.py::test_anchor_not_defended_resets` |
| GEM2 · MODULE 7 | WAIT / CONFIRMED / RESET | `mapex/executor/engine.py::step_zone` | `tests/test_executor.py::test_clean_buy_exactly_one_order` |
| GEM2 · MODULE 8 | Level 1 → RESET, else MONITOR | `mapex/executor/engine.py::noise` | `tests/test_executor.py::test_third_sweep_monitor_blocks_everything` |
| GEM2 · MODULE 8 | Level 2 MONITOR until a new map | `mapex/executor/engine.py::monitor` | `tests/test_executor.py::test_monitor_clears_only_with_structurally_new_map` |
| GEM2 · LAYER 4 | 100/100 only | `mapex/executor/engine.py::confirm` | `tests/test_executor.py::test_clean_buy_exactly_one_order` |
| GEM2 · LAYER 5 | TP ladder, 3R, reach, SD | `mapex/executor/targets.py::ladder` | `tests/test_executor.py::test_tp_ladder_rules` |
| GEM2 · LAYER 6 | fiduciary SL, dangerous trap | `mapex/executor/stoploss.py::fiduciary_sl` | `tests/test_executor.py::test_dangerous_trap_sl_on_pdl` |
| GEM2 · LAYER 6 | spread + safety buffer | `mapex/executor/stoploss.py::safety_buffer` | `tests/test_executor.py::test_clean_buy_exactly_one_order` |
| GEM2 · LAYER 7 | strategy catalog labels | `mapex/executor/engine.py::confirm` | `tests/test_executor.py::test_killzone_gates` |
| GEM2 · LAYER 8 | institutional rationale | `mapex/executor/engine.py::format_b` | `tests/test_executor.py::test_clean_buy_exactly_one_order` |
| GEM2 · LAYER 9 | no-setup reason codes | `mapex/executor/engine.py::format_a` | `tests/test_executor.py::test_dangerous_trap_sl_on_pdl` |
| GEM2 · LAYER 10 | alpha pick | `mapex/executor/engine.py::alpha_pick` | `tests/test_executor.py::test_alpha_pick` |
| GEM2 · LAYER 11 | Format B (A+) | `mapex/executor/engine.py::format_b` | `tests/test_executor.py::test_clean_buy_exactly_one_order` |
| GEM2 · LAYER 11 | Format C (RESET) | `mapex/executor/engine.py::format_c` | `tests/test_executor.py::test_wick_only_then_type7_entry` |
| GEM2 · LAYER 11 | Format A (no-setup / 3001 / 3002) | `mapex/executor/engine.py::format_a` | `tests/test_executor.py::test_thesis_invalidated_by_d1_close_past_root` |
| GEM2 · LAYER 12 | executive verdict → entry message | `mapex/telegram.py::msg_entry` | `tests/test_telegram.py::test_entry_message_format_and_escaping` |
| GEM2 · EXECUTION POLICY | determinism, no lookahead | `mapex/executor/engine.py::evaluate` | `tests/test_executor.py::test_determinism_and_no_lookahead` |

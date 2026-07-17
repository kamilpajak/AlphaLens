# Shadow collections inventory — 2026-07-16

**Status:** LIVING (update when a track is promoted, retired, or added).

Everything the live pipeline collects that **no production decision consumes today**
(selection and brief sorting untouched; "display" means a card chip / drawer / /edge
readout only). Each track carries a poolability key; rows under different tokens
never pool (ADR 0013 R3).

## A. Awaiting a scheduled first look

| Track | What / where | Since | Used today | First look (gate) |
|---|---|---|---|---|
| `options_*` | 16 cols @ score stage (yfinance chain snapshot, post-close freeze) | 2026-07-07 | /edge terminal readout | Cluster **#19**, pre-reg 2026-07-16: full spread-stratified panel (NOT OK-gated), matured N≥30 ≈ mid-Aug–Sep |
| Expert panel | `buffett_*` (14) + `oneil_*` (8) + `expert_spread`, stamped @ score + `experts enrich` | 2026-06-13/14 | Card chip + drawer (tone-neutral) | Cluster **#15**, last budgeted re-look ~2026-09, retire-if-null |
| `proposal_shadow` | Per (theme, date): LLM pre-gate set vs mechanical salience-membership set → `~/.alphalens/thematic_candidates/proposal_shadow/` | 2026-07-12 | Nothing | Cluster **#21**, ~2026-09+: forward head-to-head, kill lines pre-committed (incl. $500M–10B size gate) |
| `novelty_rank/score` | Theme-novelty stamp on candidate parquet | 2026-06-20 | Display | EDGE attribution join at N≥30 (rides the general sweep) |
| `mstate_*` (market state) | SPY regime label + drivers (ATR%, dist200, VIX), broadcast per asof | 2026-06 | Display context label (UNVALIDATED heuristic) | Regime × outcome study, undated; any use = pre-registered look |
| `broker_fills` | Betlejem paper-ledger closed-trade export (`broker-fills-v1`, scale-free ratios only) → `~/.alphalens/broker_fills/broker-fills-<runts>.parquet`, per-run snapshot, rsync/manual-delivered, lexically-latest wins (`broker_fills_export_design_2026_07_17.md`) | 2026-07-17 (contract; Arm-A rows accrue only after betlejem C1612 merges) | Nothing | Cluster **#22**: one Mann-Whitney U on `pnl_pct_of_notional`, HARD floor N≥30 closed POST_C1612 trades PER ARM; exec-cost calibration look charges §4.1 separately |

## B. Counterfactual replays on outcome rows (/edge what-ifs)

| Track | What | Used today | Gate |
|---|---|---|---|
| `breakeven_realized_r_json` | 4 registered lenses (be_0p5r, fill-anchored, be_0p5r_trail0p6 fwd-only from 2026-07-16, atr_bracket_1p5 fwd-only from its deploy date — `bezpazery_lens_design_2026_07_16.md`), cap 5 | /edge what-if panel (in-sample label, helped/harmed counts) | Every registered lens charges the ~2026-09 stop-rule walk-forward multiplicity (§4.1 annex) |
| `ratchet_realized_r` | Stop→BE on TP1-hit replay | Stamped, unsurfaced (barely binds) | Same §4.1 budget |
| `grid_realized_r_json` | Alternate-exit-ladder grid per row | Nothing | Exit-policy analysis, Sep walk-forward |
| `realized_r_full_fill` | Entry-side counterfactual (full-fill blended entry) | Nothing | Entry-drag analysis |

## C. Poolability / provenance keys (no analysis content — they guard pooling)

`catalyst_config_version` (2026-07-16) · `setup_builder_config_version` ·
`ladder_config_version` · `scorer_config_version` · `mapper_config_version`
(doubles as the frozen-candidates idempotency gate) · `insider_signal_version` ·
`options_config_version` · `panel_config_version` · `novelty_config_version` ·
`market_state_config_version` · `fills_source_version` (2026-07-17, stamped
EXPORTER-side per row — canonical sorted-keys JSON
`{"broker":"ibkr-paper","schema":1,"source":"thesis-jsonl+llm-outcomes-jsonl"}`;
the one externally-stamped key in this list).

## D. Intentionally NOT collected

- **`ml_rank_v1`** — shadow ML score deferred (user decision 2026-07-15): a frozen
  deterministic score over already-stamped columns is retroactively reconstructable,
  and `options_*`/`mstate_*` mature ~mid-Aug, which would obsolete v1 immediately.
  First stamped version = the options-era model, post-Aug re-run.

## Excluded from this list (collected AND used)

`insider_score_usd` (feeds `layer4_weighted_score`), O'Neil R grouped-daily store
(feeds the score stage), Form-4 store, population-ladder outcome columns
(`realized_r` etc. — they ARE the EDGE measurement, not a shadow).

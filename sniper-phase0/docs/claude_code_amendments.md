# Claude Code — Amendment Guide

> **Read `docs/feature_contract.md` first.** It is the source of truth. This guide tells you how
> to refactor the existing Phase 0 scaffold onto that contract. Work in dependency order. Run
> `pytest -q` after each numbered step; do not proceed if it regresses. Update
> `docs/decision_log.md` with one line per structural change.

## What changed conceptually (why this refactor exists)

The first scaffold trained a binary skip-classifier on the user's **realized trades**, using a
feature set that mixed raw price levels with normalized features. Three corrections:

1. **Target → directional move detection on a grid**, option-economics-aware, learned from all of
   history — not a filter on trades actually taken. (Removes selection bias and rule-based setups.)
2. **Features → strictly instrument-independent** (ATR units, z-scores, ratios). Raw levels are
   banned from the model row.
3. **Options are read directly** (ATM CE/PE/straddle price structure + flow/OI/IV) for
   move/no-move, IV regime, and lean — alongside underlying structure for direction.

Legend below: **CREATE** new file · **MODIFY** existing · **DELETE/DEMOTE** · **KEEP** (reuse as-is).

---

## Step 1 — Normalization helpers (CREATE)

`src/nomad_sniper/utils/normalize.py`

```python
def atr_reference(bars, as_of_date, window=14) -> float: ...      # prior-close 14-session ATR, points
def atr_normalize(value_points, atr_ref) -> float: ...            # points -> ATR units
def rolling_tod_baseline(series, decision_time, lookback=20):     # same-time-of-day mean+std
    ...                                                           # -> (mu, sigma), leak-free
def zscore(x, mu, sigma) -> float: ...
```

All functions must use only data strictly before `decision_time`. Add `tests/test_normalize.py`
asserting leak-freeness (baseline computed at `t` ignores bars ≥ `t`).

## Step 2 — Decision grid + same-TOD helpers (MODIFY `utils/timeutil.py`)

Add:
```python
def decision_grid(session_date, grid_minutes=5, start="09:30", end="15:00") -> list[datetime]: ...
def tod_bucket_key(ts) -> str: ...   # stable key for same-time-of-day baselines
```
Keep all existing IST functions.

## Step 3 — ATM option data loader (CREATE)

`src/nomad_sniper/data/option_bars.py`

- Loads minute bars for ATM CE and PE per underlying/expiry. Expected file convention:
  `upstox_<underlying>_<expiryYYYYMMDD>_<strike>_<CE|PE>.parquet`.
- `resolve_atm_series(underlying, session_date, bars_underlying)` → returns the CE & PE series for
  the strike nearest spot at the session reference time (09:20), and a `straddle` = CE+PE series.
- Compute ATM IV per bar if greeks/IV column present; else expose hook for `bs_proxy` to estimate.
- Same IST + dedup discipline as `data/bars.py`. Raise clearly if files are absent (this family is
  optional at runtime — pipeline must degrade gracefully to underlying-only when option data is
  missing, emitting nulls for families C/D).

## Step 4 — Feature builders (MODIFY)

For every builder: **remove raw-level outputs; emit only contract-compliant normalized features.**
Names must match `docs/feature_contract.md` §5 exactly (prefixes `u_`, `o_`, `c_`).

- **`features/market_profile.py`** → emit family **A**. Delete `prev_poc`, `prev_vah`, `current_price`,
  `dev_poc`, `ib_high/low`, raw ranges as *features*. Keep computing levels internally; output only
  `u_dist_*_atr`, `u_*_location`, `u_gap_atr`, `u_value_migration_atr`, IB-breakout binaries. Take
  `atr_ref` as an argument.
- **`features/order_flow.py`** → emit family **B**. Replace raw `inferred_delta_30m`,
  `volume_30m_total`, `oi_change_30m` with `u_inferred_delta_z`, `u_volume_z`, `u_oi_change_pct`,
  keeping the existing ratio features. Rename outputs to `u_` prefix.
- **`features/context.py`** → emit family **E**. Replace `atr_14d` (raw) and raw ranges with
  `c_atr_percentile`, `c_range_consumed_pct`; add `c_india_vix_level/_change` (null-safe).
- **`features/option_structure.py`** (CREATE) → emit families **C** and **D** from the ATM CE/PE/
  straddle series. This is where the move/no-move and CE/PE-divergence features live; implement
  `o_balanced_divergence`, `o_straddle_decay_vs_theta`, `o_ce_pe_premium_ratio*`,
  `o_ce_ret_minus_pe_ret`, value-break divergences, PCRs, option-volume z-scores. All null-safe
  when option data is absent.

## Step 5 — Pipeline (MODIFY `features/pipeline.py`)

- `build_all_features(decision_time, bars_u, atm_series, atr_ref)` now stitches A+B+C+D+E.
- Add `build_features_for_grid(session_dates, bars_by_underlying, atm_by_underlying, grid_minutes)`
  that iterates `decision_grid(...)` over every session and every underlying. This replaces the
  trade-driven `build_features_for_trades` as the primary entry point. **Drop `underlying` from the
  emitted row** (contract §3) unless an ablation flag is set.
- Keep the `assert_no_leakage` call.

## Step 6 — Labeler (CREATE) + gate (CREATE); reuse triple-barrier (KEEP)

- **`labels/profitability_gate.py`** (CREATE): `ProfitabilityGate` ABC with `atr_proxy`,
  `bs_proxy`, `actual_option` implementations per contract §4.2. Each maps a candidate
  (direction, m, H, path, option context) → kept-direction or `none`.
- **`labels/directional.py`** (CREATE): for each grid point, run triple-barrier on the **underlying**
  (`labels/triple_barrier.py`, unchanged geometry), apply the gate, emit the five heads from
  contract §4.3 plus `sample_weight` placeholder. Output one label row per grid point keyed by
  `(underlying, decision_time)`.
- **`labels/triple_barrier.py`** (KEEP): geometry is correct and reused; do not rewrite. Its caller
  changes from "synthetic candidates" to "grid points on the underlying."
- **`labels/actual_trades.py`** (DEMOTE): keep, but re-document as validation-overlay only
  (contract §7). Remove it from any training path.

## Step 7 — Model (MODIFY `models/lightgbm_skip.py` → `models/directional.py`)

- Rename to `DirectionalModel`. LightGBM has no native multi-output booster, so hold several:
  one multiclass booster for `direction` (objective `multiclass`, 3 classes), one binary for
  `is_move`, and regression boosters for `magnitude_atr`, `time_to_target`, `mae_atr` (trained on
  `is_move==1` rows only).
- Accept and pass `sample_weight` (uniqueness weights from §6) into every `lgb.Dataset`.
- Keep the categorical handling and the provenance block. `predict(...)` returns a dict of head→array.

## Step 8 — Evaluation (MODIFY)

- **`evaluation/splits.py`**: add `embargo_minutes` (default = label horizon H) so train rows whose
  label window overlaps the test span are dropped; add a `sample_uniqueness_weights(label_windows)`
  helper.
- **`evaluation/metrics.py`**: add directional metrics — per-class precision/recall for
  up/down/none, `is_move` precision (how often "move" calls were real), and **acted-EV**: expected
  net option P&L if you took every up/down call at the model's chosen size. The headline is
  acted-EV, not accuracy.
- **`evaluation/phase0.py`**: replace skip-classifier verdict with the directional gate. New
  pre-committed criteria (put in this file as constants):
  1. `none`-class recall ≥ 0.70 (it reliably keeps you out of chop),
  2. up/down precision ≥ 0.55 after the option-economics gate,
  3. acted-EV positive at 2× slippage,
  4. leakage + instrument-independence tests pass.

## Step 9 — CLI (MODIFY `cli.py`)

Replace the trade-centric commands with grid commands (keep `validate-trades` for the overlay):
`build-grid-features`, `build-labels`, `train-directional`, `evaluate`, plus
`validate-overlay` (realized-trade agreement, §7).

## Step 10 — Configs (MODIFY/CREATE)

- `configs/label.yaml` (CREATE): `grid_minutes`, `label_horizon_minutes`, barrier `m`,
  `m_breakeven`, gate mode (`atr_proxy|bs_proxy|actual_option`).
- `configs/features.yaml` (CREATE): normalization windows, the ablation flag for `underlying`.
- `configs/baseline.yaml` (MODIFY): multiclass + regression head params; `embargo_minutes`.

## Step 11 — Tests (CREATE/MODIFY)

- **`tests/test_instrument_independence.py`** (CREATE, critical): assert no feature column has
  price-scale magnitude; assert feature distributions for two synthetic instruments at different
  price levels (e.g. 22000 vs 48000) are statistically comparable (KS test on each feature). This
  is the guard that the §2 law actually holds.
- **`tests/test_directional_labeler.py`** (CREATE): up/down/none resolution, gate relabeling a slow
  move to `none`, head values on known synthetic paths.
- **`tests/test_no_leakage.py`** (MODIFY): extend to grid + option features; assert normalizers
  ignore `≥ t` data.
- **`tests/test_market_profile.py`** (MODIFY): assert outputs are ATR-normalized, raw levels gone.
- Update `scripts/smoke_test.py` to the grid pipeline end-to-end.

## Step 12 — Governing docs (MODIFY)

- `CLAUDE.md`: update the problem statement and the "what you do NOT do" list to match this guide
  (done in this commit — keep in sync).
- `docs/decision_log.md`: add the pivot entries (done in this commit).

---

## Verification (must all pass before declaring done)

```bash
pytest -q                                   # all tests, incl. instrument-independence + leakage
python scripts/smoke_test.py                # grid pipeline runs end-to-end on synthetic data
sniper build-grid-features && sniper build-labels && sniper train-directional && sniper evaluate
```

The smoke test on random data should produce a NO-GO with sensible-looking per-class numbers — if
it errors or returns degenerate metrics (e.g. everything classed `none`), the wiring is wrong.

## Do NOT

- Do not reintroduce raw price/volume/premium as model features (violates contract §2).
- Do not train on realized trades (contract §7).
- Do not add deep learning — that is the *next* milestone, on this identical feature contract,
  only after the directional gate clears.
- Do not adjust the §8 verdict thresholds after seeing results.

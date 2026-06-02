# Feature Contract — Nomad Curie Sniper

> **Status: authoritative.** This document defines the learning problem, the label, and every
> feature the model is allowed to see. Code is validated *against this document*, not the other
> way around. If code and contract disagree, the code is wrong. Any change to the target or the
> feature set is a change to this file first, recorded in `docs/decision_log.md`.

---

## 1. The learning problem

**Detect tradeable directional moves in the underlying from market structure, so the move can be
expressed through options.** This is a forward-looking supervised problem learned from historical
data on a regular time grid. There are no hand-crafted setups: every eligible point in history is
labeled, and the model learns which structural states precede a move.

Three things follow from this and are non-negotiable:

1. **The structural read for *direction* is on the underlying** (front-month future). Direction
   is cleanest there — no theta drift, no IV contamination, one stable instrument.
2. **The options (ATM CE / PE / straddle) are read for *move-vs-no-move*, IV regime, and
   directional lean** — the information the underlying cannot show. Disproportionate premium
   behaviour while the underlying balances is signal, not noise.
3. **The label is option-economics-aware.** A "move" is not "the underlying went up." It is "the
   underlying moved far enough, fast enough, that the option expression nets positive after theta,
   spread, and cost over the holding horizon." See §4.

---

## 2. Instrument-independence law (the normalization standard)

This is the rule that was being violated by the first scaffold. It is now a hard constraint with
a test (`tests/test_instrument_independence.py`).

> **No feature fed to the model may carry units of price (points) or raw volume / OI / premium.**
> Every model input is one of: ATR-normalized distance, rolling z-score, ratio, percent, bounded
> count fraction, or categorical. Raw levels may exist only as *intermediate computation* and must
> never appear in the feature row.

Normalization bases (all computed from information available *before* the decision time):

| Quantity | Normalize by | Result |
|---|---|---|
| Price distance `(P − level)` | `ATR_ref` (14-session underlying ATR, points, as of prior close) | distance in ATR units |
| Price range / width | `ATR_ref` | range in ATR units |
| Underlying volume / inferred delta | trailing 20-session mean+std for the **same time-of-day bucket** | z-score |
| Option volume | same-TOD trailing baseline for that option series | z-score |
| OI change | percent of prior OI | % |
| Option premium change | theoretical theta rate (BS) OR straddle-implied decay | decay ratio (unitless) |
| Premium level | never used raw; only as ratios (CE/PE, premium/underlying) | ratio |
| IV | used as level (already normalized) and as change | level + Δ |

`ATR_ref` is the single normalizer that makes NIFTY, BANKNIFTY and FINNIFTY comparable and makes
2024-at-22000 comparable to 2026-at-26000. Use the **prior session's** ATR so it is leak-free.

---

## 3. The decision grid

Labels and features are generated at every grid point, not only at realized trades.

- **Cadence:** every `grid_minutes` (default 5) from `09:30` to `15:00` IST. The 09:15–09:30
  warm-up is excluded (IB not yet meaningful); after 15:00 there is not enough forward horizon.
- **Each grid point** produces one feature row (§5) and one label (§4).
- **Pooled across instruments:** NIFTY, BANKNIFTY, FINNIFTY grid points go into one dataset. The
  model learns structure, not instrument identity. `underlying` is **not** a feature (dropping it
  is the test of whether the structure-reading thesis holds; can be re-added behind a config flag
  for ablation only).

---

## 4. The label

Computed on the **underlying** forward path from each grid point `t`.

### 4.1 Mechanics — triple-barrier on the underlying

- **Barriers:** `± m · ATR_ref` (default `m = 1.0`), symmetric, volatility-scaled.
- **Horizon:** `H = label_horizon_minutes` (default 60), matched to the intended option holding
  period. Because of theta this must be short and explicit.
- **Resolution:** whichever barrier the underlying touches first within `H`.
  - upper first → candidate **up**
  - lower first → candidate **down**
  - neither within `H` → **none** (chop / no tradeable move)

This reuses `labels/triple_barrier.py` geometry. The barrier is a *labeling device applied to
every grid point*, NOT a setup filter — it imposes no view about which points are interesting.

### 4.2 The option-economics gate (what turns a candidate into a label)

A candidate `up`/`down` is only kept as `up`/`down` if the move would have been **profitable
through the option expression** over `H`. Otherwise it is relabeled `none`. This is pluggable
(`labels/profitability_gate.py`):

- **`atr_proxy` (v1 default, no option history needed):** require `m ≥ m_breakeven`, where
  `m_breakeven` is calibrated (offline, once) so that a representative ATM weekly option clears
  theta + spread + cost over `H`. Crude but honest and instrument-independent.
- **`bs_proxy` (needs underlying + an IV estimate):** price the ATM option you'd buy with
  Black-Scholes at entry IV, advance along the realized underlying path, decay theta, exit at the
  barrier/timeout, label on the *option's* net P&L after cost.
- **`actual_option` (needs strike-level option history):** label directly on the realized P&L of
  the actual ATM option. Strongest interpretation; use when option data is available.

The gate is why a slow correct call gets labeled `none` — the thing that protects the book.

### 4.3 Target heads (the model predicts all of these)

| Head | Type | Definition | Used for |
|---|---|---|---|
| `direction` | 3-class | up / down / none after the gate | primary signal |
| `is_move` | binary | direction ≠ none | when to be in the market at all (derivable; also trained explicitly for robustness) |
| `magnitude_atr` | regression | MFE along the path, in ATR | option strike/structure choice |
| `time_to_target` | regression | bars until barrier (∞→`H` if timeout) | theta budget / expression choice |
| `mae_atr` | regression | max adverse excursion, in ATR | stop placement (Management layer) |

`magnitude_atr`, `time_to_target`, `mae_atr` are only meaningful on `is_move == 1` rows; train
them on that subset.

---

## 5. The feature set

Every feature carries a `data_available_at` (existing `features/base.py` mechanism) and obeys §2.
Families A–B read the **underlying**; C–D read the **ATM CE/PE/straddle**; E is context.

### A. Underlying Market Profile — directional structure

| Feature | Definition | Norm |
|---|---|---|
| `u_dist_prev_poc_atr` | (price − prev-session POC) | ATR |
| `u_dist_prev_vah_atr` | (price − prev VAH) | ATR |
| `u_dist_prev_val_atr` | (price − prev VAL) | ATR |
| `u_location_vs_prev_value` | above / inside / below prior value | categorical |
| `u_open_location` | open above / in / below prior value | categorical |
| `u_gap_atr` | (today open − prev POC) | ATR |
| `u_dist_dev_poc_atr` | (price − developing-session POC) | ATR |
| `u_dist_ib_high_atr` / `u_dist_ib_low_atr` | (price − IB extreme), after 10:15 | ATR |
| `u_price_above_ib` / `u_price_below_ib` | breakout flags | binary |
| `u_prev_value_width_atr` | prior (VAH − VAL) | ATR |
| `u_prev_range_atr` | prior session range | ATR |
| `u_value_migration_atr` | (developing POC − prev POC), signed | ATR |

### B. Underlying Order Flow (inferred from bars) — momentum / conviction

| Feature | Definition | Norm |
|---|---|---|
| `u_inferred_delta_z` | signed-volume sum over window | z-score |
| `u_inferred_delta_slope_z` | slope of cumulative delta | z-score |
| `u_volume_z` | window volume vs same-TOD baseline | z-score |
| `u_volume_accel_ratio` | recent-half vol / prior-half vol | ratio |
| `u_range_expansion_ratio` | recent bar-range / prior bar-range | ratio |
| `u_oi_change_pct` | futures OI change over window | % |
| `u_up_bar_fraction` | up-bars / total in window | bounded |

### C. Option price structure (ATM CE/PE/straddle) — move/no-move, IV regime, lean

> This is the family that encodes the insight that a balancing underlying with disproportionate
> CE/PE premium behaviour tells you whether a move is coming.

| Feature | Definition | Norm |
|---|---|---|
| `o_straddle_decay_vs_theta` | realized straddle decay rate ÷ theoretical theta rate | ratio |
| `o_iv_level` | ATM implied vol | level |
| `o_iv_change` | ΔIV over window | Δ |
| `o_ce_pe_premium_ratio` | ATM CE premium ÷ ATM PE premium | ratio |
| `o_ce_pe_premium_ratio_drift` | change in that ratio over window | Δ-ratio |
| `o_ce_ret_minus_pe_ret` | CE %Δ − PE %Δ over window | % diff |
| `o_balanced_divergence` | `|CE%Δ − PE%Δ|` gated on `|underlying %Δ| < ε` | bounded score |
| `o_ce_value_break_vs_u_hold` | CE breaks own VA while underlying holds value | binary |
| `o_pe_value_break_vs_u_hold` | PE breaks own VA while underlying holds value | binary |
| `o_straddle_value_width_ratio` | straddle developing VA width ÷ trailing median | ratio |

### D. Option flow / OI (ATM CE/PE) — sentiment / positioning

| Feature | Definition | Norm |
|---|---|---|
| `o_ce_oi_change_pct` / `o_pe_oi_change_pct` | OI change at ATM strike | % |
| `o_ce_volume_z` / `o_pe_volume_z` | option volume vs same-TOD baseline | z-score |
| `o_pcr_volume` | put volume ÷ call volume | ratio |
| `o_pcr_oi` | put OI ÷ call OI | ratio |
| `o_ce_pe_aggressor_imbalance` | inferred (CE aggressive − PE aggressive) flow | bounded |

### E. Context / regime

| Feature | Definition | Norm |
|---|---|---|
| `c_minutes_into_session` | minutes since 09:15 | scaled |
| `c_time_of_day_bucket` | open/mid/lunch/afternoon/close | categorical |
| `c_day_of_week` | 0–4 | categorical |
| `c_days_to_weekly_expiry` / `c_is_expiry_day` / `c_is_pre_expiry_day` | expiry proximity | bounded/binary |
| `c_atr_percentile` | underlying ATR percentile vs trailing year | percentile |
| `c_range_consumed_pct` | today range so far ÷ typical full-day range | % |
| `c_india_vix_level` / `c_india_vix_change` | macro vol regime (if available) | level + Δ |

---

## 6. Leakage and CV rules

1. **`data_available_at ≤ decision_time`** for every feature, enforced by `assert_no_leakage`.
2. **Forward labels overlap** (adjacent grid points share future bars). This MUST be handled or
   every CV score is inflated:
   - **Embargo** a window of `H` around each test fold boundary (no train rows whose label window
     overlaps the test span).
   - **Sample-uniqueness weights** (López de Prado): down-weight a sample by how many other samples
     share its label window. Pass as `sample_weight` to the model.
3. **Walk-forward only**, with the embargo above. No random or vanilla k-fold.
4. **Normalizers are leak-free**: `ATR_ref`, volume baselines, and percentiles use only data
   strictly before `decision_time`.

---

## 7. Realized trades = validation overlay, not training target

The FY25-FY26 Zerodha log is no longer a training target. It is a sanity overlay: for each of your
actual trades, check whether the model (at that timestamp) said `up`/`down` in your direction
(agreement on winners) and `none`/opposite on your losers. Reported as agreement rates, never
mixed into training. Keep `labels/actual_trades.py` for this purpose only.

---

## 8. Calibration knobs (set before trusting any result)

- `grid_minutes` (5), `label_horizon_minutes` H (60), barrier `m` (1.0 ATR).
- `m_breakeven` for `atr_proxy` gate — calibrate from option chain so an ATM weekly clears
  theta+cost over H. **This single number gates every label; calibrate it deliberately.**
- Slippage / cost (existing `ZerodhaFnoCostModel`) — calibrate from your fills.
- Volume-baseline lookback (20 sessions), ATR window (14 sessions).

A result is only meaningful once `m_breakeven`, cost, and the option-economics gate mode are set
honestly. Until then, treat numbers as plumbing checks.

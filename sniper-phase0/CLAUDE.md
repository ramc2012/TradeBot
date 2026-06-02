# CLAUDE.md — Nomad Curie Sniper (Phase 0)

> This file is the persistent context for Claude Code working in this repo.
> Read it fully before any task. Update it whenever architectural decisions change.

---

## What this project is

**Nomad Curie Sniper** detects tradeable **directional moves** in NSE index underlyings from
market structure, so the move can be expressed through options. It complements the existing Nomad
Curie Scanner (Greeks Confluence Engine): the Scanner narrows the universe; the Sniper decides
whether *this moment* precedes a move worth trading.

**Authoritative spec: `docs/feature_contract.md`.** It defines the target, the label, and every
feature the model may see. Code is validated against that file. To refactor the current scaffold
onto it, follow `docs/claude_code_amendments.md` step by step.

## The learning problem (read the contract for detail)

Forward-looking supervised learning on a **time grid over the underlying**, learned from all of
history — not a filter on trades actually taken, and no hand-crafted setups. At each grid point:

- **Direction** is read from the **underlying** (front-month future): clean, no theta/IV drift.
- **Move-vs-no-move, IV regime, and directional lean** are read from the **ATM CE/PE/straddle**:
  a balancing underlying with disproportionate premium behaviour reveals whether a move is coming.
- The **label is option-economics-aware**: a "move" means the underlying moved far/fast enough that
  the option expression nets positive after theta, spread, and cost over the holding horizon.

The model predicts: `direction` (up/down/none), `is_move`, `magnitude_atr`, `time_to_target`,
`mae_atr`. LightGBM (multi-head) validates signal; a neural net comes later on this *same* feature
contract, only after the directional gate clears.

## Why this framing

The FY25-FY26 P&L review found **loss management was the binding constraint** (50% loss-containment
→ ~7.2x P&L). Detecting `none`/chop reliably — knowing when *not* to trade — is therefore as
valuable as picking direction, which is why move/no-move is a first-class target.

## Go/no-go criteria (pre-committed, in `evaluation/phase0.py`)

1. **`none`-class recall ≥ 0.70** — reliably keeps you out of chop.
2. **up/down precision ≥ 0.55** after the option-economics gate.
3. **Acted-EV positive at 2× slippage** — taking every up/down call at chosen size nets positive.
4. **Leakage + instrument-independence tests pass.**

All four must pass. Partial passes mean return to features, not promote.

## Hard rules (do not violate)

1. **No look-ahead in features.** Every feature has a `data_available_at`; used at `t` only if
   `data_available_at <= t`. Enforce via `assert_no_leakage()` in `nomad_sniper.features.base`.
   Normalizers (ATR, baselines, percentiles) must also use only pre-`t` data.
2. **Instrument-independence law.** No feature fed to the model carries units of price, volume, or
   premium. Everything is ATR-normalized, z-scored, ratio, %, or categorical. Guarded by
   `tests/test_instrument_independence.py`. (See contract §2.)
3. **Costs / theta go into labels, not evaluation.** The option-economics gate bakes theta, spread,
   and cost into the label. Do not re-subtract at evaluation.
4. **Walk-forward only, with embargo + sample-uniqueness weights** for overlapping forward labels.
   No random splits. (See contract §6.)
4. **Times are IST.** All timestamps in storage and in code are `Asia/Kolkata` timezone-aware.
   Naive datetimes are a bug; the loaders raise on them.
5. **Money is rupees, not paise.** All prices in float rupees; round only at the display layer.
6. **Reproducibility.** Every model artifact carries a config hash and a git SHA. The
   `nomad_sniper.utils.provenance` module enforces this.
7. **Never commit secrets.** Fyers / Zerodha / Upstox API keys live in `.env` only; `.env` is
   gitignored. The settings loader in `nomad_sniper.utils.settings` reads them.

## What lives where

```
src/nomad_sniper/
  data/          Loaders: Zerodha trade log, Upstox underlying bars, ATM option bars (pending)
  profiles/      Auction primitives: profile.py (MP: POC/VAH/VAL/HVN/LVN/shape/excess/IB),
                 open_type.py (Dalton open types), day_type.py (trend/balance/neutral)
  features/      market_profile (geometry+shape+auction-state), htf (multi-timeframe stack),
                 order_flow (inferred + depth stubs), context (regime), pipeline (stitch)
  labels/        triple_barrier geometry (done); directional labeler + gate (pending)
  models/        DirectionalModel — multi-head LightGBM (pending)
  evaluation/    walk-forward (done); embargo + uniqueness weights + cross-instrument (pending)
  utils/         settings, logging, provenance, time/grid, normalize (ATR/z-score/percentile)
configs/         label.yaml, features.yaml, cost_model.yaml, baseline.yaml
notebooks/       grid pipeline + verdict notebook
tests/           leakage + instrument-independence + profiles + labeler tests (28 passing)
data/raw|interim|processed/   broker exports / feature matrices / labels (read-only raw)
artifacts/       saved models, evaluation reports
docs/            feature_contract.md (authoritative), claude_code_amendments.md,
                 IMPLEMENTATION_STATUS.md (spec→repo map), decision_log.md
```

## Workflow Claude Code should follow

For any change:

1. **Read the relevant module's docstring** before editing. They state assumptions.
2. **Run `pytest -q` before and after.** If tests fail after your change, fix them or revert.
3. **Add a leakage test** for any new feature. Pattern in `tests/test_no_leakage.py`.
4. **Update `docs/decision_log.md`** with one line per architectural decision.
5. **Never edit `data/raw/`.** It is the source of truth from broker exports.

## What you do NOT do in this repo

- Do not reintroduce raw price / volume / premium as model features (breaks contract §2).
- Do not train on realized trades — the trade log is a validation overlay only (contract §7).
- Do not hand-craft setup rules to choose candidate moments — label every grid point.
- Do not detect *direction* on the option price — direction is read on the underlying.
- Do not import deep learning frameworks (torch, tensorflow). NN is the next milestone on this
  same feature contract, only after the directional gate clears.
- Do not call broker APIs from this repo. Historical CSV/parquet inputs only.
- Do not adjust the `evaluation/phase0.py` verdict thresholds after seeing results.

## Current open questions (update as resolved)

- [ ] **Option data availability** decides the gate mode: strike-level option history →
      `actual_option`; underlying + IV estimate → `bs_proxy`; underlying only → `atr_proxy`.
- [ ] **Calibrate `m_breakeven`** (the ATR move an ATM weekly option needs to clear theta+cost
      over H) — this single number gates every label.
- [ ] Calibrate slippage in `ZerodhaFnoCostModel` from your own fills.
- [ ] Confirm `label_horizon_minutes` H matches the real intended option holding period.

## How to get help when stuck

- Architecture questions: re-read `docs/architecture.md`.
- Why a rule exists: check `docs/decision_log.md`.
- "Should I add X?": if it's not in `pyproject.toml` deps and not in the phased roadmap, the
  default answer is no. Ask before adding.

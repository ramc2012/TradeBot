# Nomad Curie — Strategy Testing & Tuning Plan

**Owner:** Quant Lead
**Scope:** All trading lanes in `nomad-curie/backend` (NSE F&O system)
**Last updated:** 2026-06-06
**Status:** Working plan — execute in phases (Section 8)

---

## 1. Executive Summary & Objectives

This plan defines how every strategy lane in Nomad Curie is **backtested, tuned, walk-forward validated, paper-forward-tested, and promoted to live** — with a single, repeatable methodology and hard acceptance gates. The goal is to separate genuine edge from curve-fit artifacts **before** capital is at risk, and to do all heavy compute **off the production backend** to avoid the OOM→recreate→revert cascade documented in project memory.

### Objectives

1. **One methodology, all lanes.** Every tradeable lane runs the same back→walk-forward→paper pipeline with the same six overfitting gates (Section 4). No lane goes live on an in-sample backtest alone.
2. **Honest costs.** Apply NSE F&O round-trip costs (`backend/paper_engine/costs.py`) and 75th-percentile slippage on every close. A pre-cost edge is not an edge.
3. **Off-prod compute.** All backtests and sweeps run in **isolated sidecars** (the `gann_tp_delta/tune_sweep.py` pattern + the `sniper-shadow` sidecar) using direct DB connections — never `docker exec python` inside `nomadcurie_backend` (OOM rc=137 → container recreate → DB connection leak → pool exhaustion).
4. **Data-quality first.** Guard against the known `underlying_spot_candles` contamination (cross-symbol price bleed, 44% duplicate timestamps) on every read, and execute the backfill/arrangement plan (Section 2) before claiming a lane is "backtestable".
5. **Promotion discipline.** A param set moves backtest → paper → live only by passing explicit gates (Section 7). Degradation >40% vs backtest halts promotion.

### What counts as "tradeable" here

Tradeable lanes (get the full pipeline): **nse S1, nse S2/commodity MP+OF, directional_options, auction_intelligence, fractal_market_profile, gann_tp_delta, cbe_scanner, commodity, sniper**.
Research/monitoring lanes (validated, not promoted to capital): **sector_interaction, market_intelligence** — these get a forecast-skill validation (Section 3) but no paper/live capital gate.

---

## 2. Data Inventory & Quality

**DB:** TimescaleDB (`nomadcurie_db`, host `:5433`, network `db:5432`). DSN `postgresql://nomadcurie:nomadcurie@db:5432/nomadcurie`.
**Coverage (as of 2026-06-04):** `underlying_spot_candles` 45.2M rows (30m), `option_premium_candles` 336K rows, 195 option underlyings, 597 contracts, range **2025-03-28 → 2026-06-04 (~14 months)**.

### 2.1 Availability matrix (from data map)

| Lane | Primary table(s) | Candles | Greeks | MP | Order flow | Status |
|---|---|---|---|---|---|---|
| nse S1 (30m MACD ATM) | `option_premium_candles`, `underlying_spot_candles` | OK | **NO** (gamma/theta/vega not backfilled) | lite | NO | **PARTIAL** — usable, IV-only |
| nse S2 (5m MP+OF index) | `index_futures_candles`(1m), `market_profiles` | spot OK | NO | hourly | NO | **PARTIAL** — no 5m index options |
| directional_options | `option_premium_candles`, spot | OK | **NO** | no | NO | **PARTIAL** — greeks missing |
| auction_intelligence | `market_ticks`, `option_chain_snapshots`, `fo_option_chain_metrics` | ticks | 120s | hourly | **not archived** | **PARTIAL** — CVD live-only |
| fractal_market_profile | `underlying_spot_candles`, `hourly_profiles` | OK | NO | hourly | NO | **AVAILABLE** (verify MP depth) |
| gann_tp_delta | `underlying_spot_candles` (30m→15m resample) | OK | NO | no | NO | **AVAILABLE** |
| cbe_scanner | `cbe_scan_runs/results`, `option_premium_candles` | OK | NO | no | **RS not persisted** | **MUST-ARRANGE** |
| commodity | `commodity_profiles`(JSON), `commodity_archive`(JSON) | **NO 1m OHLC** | n/a | JSON | NO | **MUST-ARRANGE** |
| sniper | `index_futures_candles`(1m), `underlying_spot_candles` | recent only | 120s | hourly | NO | **PARTIAL** — shallow history |
| sector_interaction | (none persisted) | NO | NO | no | NO | **MUST-ARRANGE** |
| market_intelligence | live NSE aggregates (in-mem) | n/a | n/a | n/a | n/a | stateless monitor |

### 2.2 Data quality guards (mandatory on every read)

The spot feed is **known-contaminated** (project memory, confirmed via `source`/`instrument_key` columns):
- **Cross-symbol price bleed:** `source=local_csv_spot` file `1minute.csv.gz` mislabels ~4,500 SENSEX rows as NIFTY (71k–77k prints); `source=live_tick` carries 7 NIFTY rows at BANKNIFTY's exact price. One bad print explodes session high/low → ATR (BANKNIFTY 3543 corrupt vs 765 true; NIFTY 2511 vs 365).
- **~44% duplicate-timestamp rows.**
- **Clean source** for indices = `source=timescaledb_spot_1minute` (e.g. `NSE:NIFTY50-INDEX`, ~23k rows).

**Every backtest/sweep loader MUST replicate the sniper guard:**
1. **Dedup** on `(symbol/instrument_key, interval, time)` keep=last.
2. **RTH filter** 09:15–15:30 IST (NSE) / contract hours (MCX) before any session MP/VWAP/IB/ATR build; spot feed carries non-RTH junk rows.
3. **Outlier drop:** remove rows whose o/h/l/c deviate **>20% from per-session median close**.
4. **Source allow-list** for indices: prefer `timescaledb_spot_1minute`; treat `local_csv_spot` and contaminated `live_tick` rows as suspect.
5. **tz:** DB time is UTC tz-aware → `tz_convert('Asia/Kolkata')` before session logic.

These guards are already in `nomad_sniper` sidecar + scorer — **reuse that code**, do not re-implement per lane.

### 2.3 Backfill / arrangement plan (priority-ordered)

**CRITICAL (blocks faithful backtest):**
| Item | Action | Lanes unblocked | Effort |
|---|---|---|---|
| **MCX 1-min OHLC** | Upstox MCX historical backfill: CRUDEOIL, GOLD, SILVER(M), NATURALGAS, ZINC, LEAD, NICKEL, ≥6 months into a new `commodity_spot_candles` (or reuse `underlying_spot_candles` with MCX root) | commodity, nse S2/MP+OF | ~500–1000 API calls |
| **Sector/stock RS snapshots** | Daily cron archiving asset-winner + top-4 sectors + top-10 stocks/sector to `cbe_scan_runs/results` — **start immediately** (forward-only history accrues) | cbe_scanner, sector_interaction | 1 cron, accrues |
| **Options greeks history** | Backfill gamma/theta/vega into the 336K `option_premium_candles` rows from Upstox, OR accept IV-only + simulate greeks from IV term structure | directional_options, auction_intelligence | expensive — phase 2 |
| **MP intraday depth verify** | `SELECT min(time), max(time), count(*) FROM hourly_profiles` — if <6 months, backfill via FMP replay-suite | fractal_market_profile, nse S2 | 1 query + replay |

**HIGH:**
- **Per-minute CVD archive** (`market_ticks` companion table) — unblocks order-flow backtests for auction_intelligence + sniper B2 retrain. ~2–5 rows/min/underlying.
- **Wider option strikes** — `DISCOVERY_COMMON_STRIKES` is 2; 21,844 contracts skipped. Widen if directional needs OTM legs.
- **Index futures 1m depth** — backfill ≥6 months for sniper (table created May 2026, shallow).

**MEDIUM / accepted limits:**
- BANKNIFTY weekly retired Nov 2024 → run S2 on monthly proxy or exclude BANKNIFTY.
- L2–L5 depth not archived → MP `va_migration`/`ib_extension` extensions run forward-only.
- Pre-2025-03-28 history unavailable.

**Backfill verification:** after each backfill, run a coverage query per underlying (min/max date, row count, gap analysis); flag any underlying with <10 trading days or <2000 rows. Re-run the cross-symbol validation report (sample 10 underlyings, check duplicate timestamps across symbols).

---

## 3. Per-Strategy Testing Matrix

For each lane: **data source → backtest method → params to tune (ranges) → eval metrics → OOS/walk-forward design → paper gate.** Module paths are relative to `backend/`.

### 3.1 NSE Strategy-1 — 30m ATM MACD (long premium)
- **Data:** `option_premium_candles` (30m CE/PE) + `underlying_spot_candles` (30m). 14mo. IV-only (no greeks).
- **Backtest:** `analysis/backtest.py` (`MACDBacktester`); sweeps `analysis/macd_cross_location_sweep.py`, `option_indicator_sweep.py`, `staggered_exit_sweep.py`, `timeframe_sweep.py`. API `/api/backtester/run-csv|run-json`. Output → `runtime/index_analytics_data/`.
- **Params to tune:** MACD fast `8–15`, slow `20–30`, signal `7–9`; cross-location filter (distance from zero-line); RSI/ADX entry gate; staggered-exit fractions & R-multiples; timeframe confirm (HTF 30m/60m).
- **Metrics:** win_rate, avg/median return %, profit_factor, expectancy, opportunities count, per-timeframe breakdown.
- **OOS/WF:** rolling 12mo IS (cap to available 14mo) / 3mo OOS / monthly stride aligned to expiry. Min IS window practically ~9mo given data depth — flag MinBTL (Section 4).
- **Paper gate:** directional_options paper proposal `/api/directional-options/paper-proposal` → position journal. 4–6 wks, degradation 0.70–0.95.

### 3.2 NSE Strategy-2 — 5m MACD + Market Profile + Order Flow (index)
- **Data:** `index_futures_candles` (1m→5m), `market_profiles`/`hourly_profiles` (POC/VAH/VAL/IB). **Gap:** no 5m index option history → backtest the **signal on futures**, map to ATM option in paper. NIFTY+SENSEX only.
- **Backtest:** reuse the MP+OF evaluator path via `analysis/commodity_walkforward.py` replay harness (same `evaluate_*_mp_signal` machinery), adapted to index 1m bars.
- **Params:** TPO period (15m), IB window, value-area %, entry triggers (open_drive / ib_break / failed_auction / va_migration / lvn_fade) priority gates, OF confirm threshold (CVD/book_pressure), ATR stop/target multipliers.
- **Metrics:** R-multiple, win_rate, target_reached %, entry_reason / entry_style / MP_day_type breakdown.
- **OOS/WF:** verify `hourly_profiles` depth first. Rolling 6mo IS / 1mo OOS (data-limited). Forward-test-heavy until MP backfill lands.
- **Paper gate:** live agent at 60s during NSE hours → journal. Same 0.70–0.95.

### 3.3 Directional Options (long premium)
- **Data:** `option_premium_candles` (IV+delta; greeks missing) + spot. `directional_option_runs/candidates/trades` tables.
- **Backtest:** `directional_options/backtest.py` (`DirectionalOptionsBacktester.run`). API `/api/directional-options/backtest`.
- **Params:** timeframe, lookback_sessions; regime gate (ADX/RSI/EMA-spread thresholds); entry score floor; exit rules (target R, trail, time-stop, premium hard-stop).
- **Metrics:** engine_score, stability (Sharpe/maxDD), monthly P&L, regime_breakdown, exit_breakdown, recent_trades.
- **OOS/WF:** **add a sweep loop** (gap) — grid over timeframe × lookback_sessions × score-floor, then rolling 3mo OOS. Build `directional_options/tune_sweep.py` on the gann pattern.
- **Paper gate:** `PaperPositionBook` (one position/symbol). 4–6 wks.

### 3.4 Auction Intelligence (MP/OF/options, multi-agent)
- **Data:** `market_ticks`, `option_chain_snapshots` (120s greeks), `fo_option_chain_metrics` (30m PCR). **CVD not archived** → OF confirmation is forward-only until CVD backfill.
- **Backtest:** no standalone backtester (gap). Use `validation/engine.py` (`GateAValidator`) + `validation/gate_b.py` `_walk_forward_windows()` (3-window slice of recorded shadow trades). **Build `auction_intelligence/ai_backtest.py`** replaying agent/MP/OF decisions on historical bars (no sniper dependency for the core).
- **Params:** per-agent confidence floors (positional/swing/scalp), MP value-area gates, OF toxicity/book-pressure thresholds, sniper-overlay alignment weight (`_apply_sniper_overlay`).
- **Metrics:** win_rate, profit_factor, Sharpe, maxDD, monthly, per-agent score, confidence distribution.
- **OOS/WF:** Gate-B 3-window walk-forward on shadow records; expand to rolling 3mo OOS once `ai_backtest.py` exists.
- **Paper gate:** `paper/book.py` `PaperPositionBook` syncs `AnalysisBundle` executions; `run_market_hours_cycle()`.

### 3.5 Fractal Market Profile
- **Data:** `underlying_spot_candles` (1m) + `hourly_profiles` (POC/VAH/VAL/shape). `runtime/commodity_profiles/<ROOT>/<DATE>.json` prior-session.
- **Backtest:** `fractal_market_profile/paper.py` `FMPPaperStore.record_signal`; API `/api/fractal-market-profile/replay-report` (per-symbol), `/replay-suite` (all SUPPORTED_SYMBOLS).
- **Params (gap — add param loop):** setup confidence floor (0–1), volatility/IB thresholds, value-area %, shape-classification cutoffs, single-print sensitivity.
- **Metrics:** confidence, setup_name, action, rejection_reasons, paper_summary (equity, Sharpe, maxDD).
- **OOS/WF:** replay-suite over rolling windows; parametric sweep over confidence floor (Gann template).
- **Paper gate:** `FMPPaperStore` lifecycle; daily sync via supervisor.

### 3.6 Gann TP Delta — **reference implementation**
- **Data:** `underlying_spot_candles` (30m→15m resample, 150-day pull). Commodity frames via `market_intelligence_runtime.load_local_spot_rows` (capped 20d).
- **Backtest:** `gann_tp_delta/backtest.py` (`GannTPDeltaBacktester.run`) — event-driven R-multiples. **Sweep:** `gann_tp_delta/tune_sweep.py` (isolated asyncpg sidecar; local EMA/ADX/ATR compute). API `/api/gann-tp-delta/backtest`.
- **Params:** `continuation_min_conviction` (4.0–6.5), `commodity_min_conviction` (5.5–6.5), `per_underlying_min_conviction` (e.g. BANKNIFTY 6.0), anchor_mode (auto_pivot), h_mode (median_tpd). Conviction floor = dominant lever (proven).
- **Metrics:** win_rate_pct, expectancy_r, profit_factor, total_r, max_drawdown_r, per-archetype (entry reason × entry style × MP regime).
- **OOS/WF:** sweep across floor × underlying on 150d; rolling 3mo OOS. Current tuned floors: continuation 5.0, commodity 6.0, BANKNIFTY 6.0 (was negative-EV at every lower floor).
- **Paper gate:** `run_paper_agent_once()` at 60s; opposite-exit only if opposite conviction ≥ reversal bar.

### 3.7 CBE Scanner (asset rotation / alpha)
- **Data:** `cbe_scan_runs/cbe_scan_results` (minimal history), `option_premium_candles` for universe; **sector/stock RS not persisted (CRITICAL gap).**
- **Backtest:** none today; `cbe_scanner/alpha_engine.py` runs live 7-layer pyramid. **Cannot walk-forward without daily RS snapshots** → arrange first (Section 2.3).
- **Params (deterministic today — make parametric):** `TOP_N_WATCHLIST` (10), `LOW_CONVICTION_FLOOR` (50), composite_alpha_score layer weights (asset/sector/stock/option-filter/bias/MP/composite-RS), sectors-to-keep count, finalists count, `min_hold` (5d), rebalance cadence.
- **Metrics:** composite_alpha_score, top-N, bias accuracy; portfolio: equity, Sharpe, maxDD, win_rate (₹1M book).
- **OOS/WF:** once RS snapshots accrue ≥3mo, walk-forward on weekly rebalances.
- **Paper gate:** `cbe_scanner/paper.py` `CBEPaperBook`; `_cbe_runner()` 15m, `_cbe_marks_runner()` 5m LTP.

### 3.8 Commodity Strategy (MCX futures, MP+OF)
- **Data:** **MUST-ARRANGE 1m MCX OHLC** (Section 2.3). Today only JSON archives.
- **Backtest:** `analysis/commodity_walkforward.py` (`CommodityStrategyAgent` replay) → `ReplayPosition` records in `runtime/index_analytics_data/commodity_walkforward/`.
- **Params (hardcoded today — externalize):** `FUTURES_BREAK_EVEN_R_MULTIPLIER`, `FUTURES_TRAIL_ATR_MULTIPLIER`, trigger-priority gates, ATR stop/target multipliers, daily-loss cap (₹25k), per-underlying cap (₹15k), event-block (90m), cooldown (60m), `commodity_min_conviction` (≥6.0 from gann tuning).
- **Metrics:** R-multiple, win_rate, target_reached %, entry_reason/entry_style/MP_day_type breakdown. (Memory: GOLD/SILVERM/NG net positive; **CRUDEOIL structurally weak ~−1R at every floor — candidate to drop**.)
- **OOS/WF:** walkforward on 1m bars once backfilled; per-underlying floor sweep (gann template).
- **Paper gate:** live agent `_gann_runner()` 60s during MCX hours (09:15–23:30 IST). MCX slippage = **2× NSE baseline**.

### 3.9 Sniper (excursion estimator sidecar)
- **Data:** `underlying_spot_candles` (1m, guarded), `option_premium_candles` (o_* features), live OF via `/api/auction-intelligence/live-snapshot`. Model `excursion_estimator_sensex.joblib` (123 features {u:99, o:16, c:8}).
- **Backtest/score:** no classic backtester — **the shadow→score loop IS the OOS test.** `nomad_sniper/integration/sniper_sidecar.py` predicts; `sniper_scorer.py` joins matured predictions vs realized (ATR-normalized) → `sniper_metrics.json`, `sniper_dashboard.html`. Retrain: `sniper_retrain.py` (promotes only if cand mag-IC ≥ incumbent+0.03 across majority horizons on ≥120-row holdout).
- **Params:** horizon set (30/60/90/120m + eod/1d/2d/3d/1w/1M), confidence floor, `SNIPER_RETRAIN_TARGET` (600 OF-bearing labels), LightGBM hyperparams (retrain only), overlay alignment weight in AI lane.
- **Metrics:** per-horizon dir_acc, magnitude rank-IC, signed-IC, ttp-corr. (Early: overall dir_acc 0.59, **mag IC +0.35** — magnitude skill confirmed, direction noise-dominated at low n.)
- **OOS/WF:** time-holdout in `sniper_retrain.py`; the live shadow log is a continuous forward OOS.
- **Paper gate:** sniper is an **overlay**, not a standalone capital lane — its gate is "does adding the overlay improve the host lane's (auction_intelligence) paper metrics vs overlay-off A/B".

### 3.10 Sector Interaction & Market Intelligence (research/monitoring — no capital gate)
- **Data:** sector RS history **MUST-ARRANGE**; MI is live in-mem aggregates (`market_data/market_intelligence_runtime.py`).
- **Validation (not P&L):** Sector — VAR/Granger forecast skill on rolling windows: does the correlation/lead-lag model predict next-period sector returns better than random? (`/api/sector-interaction/model`). MI — data-freshness/coverage checks only.
- **Metrics:** forecast hit-rate, directional R², Granger p-values stability across windows.
- **No paper/live gate** — these feed other lanes; validate forecast skill, don't allocate capital.

---

## 4. Back → Forward Methodology & Overfitting Controls

The same pipeline applies to every tradeable lane.

### 4.1 In-sample → walk-forward → paper

```
Data split:   first ~70–75% IS pool  |  final 25–30% held-out OOS (never touched in tuning)
WF windows:   IS train = up to 2y rolling (cap to ~9–12mo given 14mo depth)
              OOS test = 3mo rolling forward
              stride   = monthly, aligned to NSE monthly expiry (last Thu)
Timestamps:   IST trade-date boundaries (never UTC) — avoids NSE-close misalignment
```

Use **rolling, not anchored** windows (keeps optimization data regime-relevant; 2y rolling covers ~24 monthly expiries without dragging stale regimes). Compute **Walk-Forward Efficiency** per OOS window:
`WFE = OOS_Sharpe / IS_Sharpe` — target ≥0.65; red flag <0.40. Log median + 10th-pct WFE across rolls.

### 4.2 The six mandatory overfitting gates (all must pass)

| Gate | Metric | Threshold | Why |
|---|---|---|---|
| 1 | OOS trade count | ≥100 combined OOS (≥50 min) | small-sample stats unreliable |
| 2 | Walk-Forward Efficiency | ≥0.50 (prefer ≥0.65) | parameter sensitivity / regime drift |
| 3 | Deflated Sharpe (DSR) | ≥0.40 | adjusts for N parameter combos tested |
| 4 | Prob. Backtest Overfitting (PBO) | <0.40 (prefer <0.25) | fraction of OOS windows below median |
| 5 | Minimum Backtest Length (MinBTL) | backtest ≥ MinBTL(N, SR) | sample adequacy for claimed Sharpe |
| 6 | 5th-pct Monte-Carlo Sharpe | >0.30 | survives bad-luck trade sequencing |

- **DSR:** `DSR = SR_sample · sqrt((1−γ)/N)`, γ≈0.5, **N = (#param sets) × (#WF windows)**. Log N per sweep — it directly deflates the claim.
- **PBO:** count OOS windows below the median OOS Sharpe / total windows. With 8 windows, ≥4 below ⇒ PBO≥0.50 ⇒ reject.
- **MinBTL rule of thumb:** `MinBTL_years ≈ sqrt(N / SR²)`. Test 50 sets at SR 1.2 ⇒ need ~3.3y. **We have ~14mo** — this is a real constraint: keep N small per lane and prefer plateau robustness over peak Sharpe (Section 5).

### 4.3 Monte-Carlo & bootstrap
- 1000× trade reshuffle (bootstrap with replacement): recompute equity, maxDD, Sharpe per resample → report median, 10th/5th-pct.
- Bootstrap 95% CI on maxDD (use 95th-pct as live risk budget), win-rate, avg win/loss.
- **Gate:** 5th-pct Sharpe >0.30; 5th-pct trade frequency >50% of median.

### 4.4 Regime-conditional evaluation
- Label OOS bars with a 3-state HMM on daily returns + realized vol (low / medium / high-vol-trending). (Gap — build a small `analytics/regime_hmm.py`.)
- **Gate:** profitable in all 3 regimes (low ≥0.40, medium ≥0.50, high ≥0.30 Sharpe). A lane profitable in only one regime is overfit to it.

### 4.5 Realistic costs (apply on CLOSE only)
- Use `backend/paper_engine/costs.py` round-trip model: brokerage ₹20/order, STT 0.10% options sell-leg / 0.02% futures sell-leg, exchange ~0.035% options / ~0.0017% futures, SEBI ~₹10/cr, stamp duty buy-leg, **GST 18%** on (brokerage+exchange+SEBI).
- **Slippage:** calibrate from paper fill log (mid-at-decision vs actual fill), segment by instrument × time-of-day × size; use **75th-pct** (conservative). Prefer 11:30–14:00 IST liquidity; **MCX slippage = 2× NSE**. Recalibrate quarterly.
- `net_pnl = gross_pnl − round_trip_charges(...)`. A pre-cost edge that dies post-cost is not promoted.

---

## 5. Tuning Workflow (the Gann template)

**Canonical pattern = `gann_tp_delta/tune_sweep.py`.** Every other lane's sweep is built to match it: self-contained, direct `asyncpg` (1 connection, no app/broker bootstrap), local feature compute (EMA via `analysis.macd_engine.compute_ema`, ADX via `analytics.technicals.compute_adx`, ATR), grid-loop over `param × underlying`, prints a results grid. The gann **conviction-floor sweep is the worked example**: 150-day pull → backtest each floor (4.0–6.5) × underlying → grid → pick floor that maximizes total_r with acceptable maxDD_r (proven: floor is the dominant lever; higher = fewer/better trades).

**Three-phase tuning:**
1. **Coarse grid** — enumerate obvious ranges (Section 3 per-lane), cheap & parallel, find ~5 promising regions. This is exactly what the gann floor sweep does today.
2. **Bayesian refine (Optuna TPE)** — seed from grid; objective = `median(OOS_Sharpe) − 0.1·std(OOS_Sharpe) − 0.05·maxDD_OOS` (penalize variance & drawdown). 50–200 evals. *(Gap: add `optuna` — MIT, no conflicts.)*
3. **Plateau selection (NOT peak-picking)** — map the param surface around the top-20; find a region where adjacent params give OOS Sharpe within ±0.05; pick from the plateau the set with **highest minimum rolling Sharpe**, recurring in CPCV top-5 across ≥5 folds, and **simplest** (Occam). A sharp peak is fragile; a plateau survives 20% param drift. Given our short history this is the most important discipline.

**CPCV sanity check** (secondary): 5–10 purged folds, embargo ±1 trading day (options theta/IV leakage), 3–5 combinatorial draws. Flag high cross-fold variance or wildly-differing top-K params as signal instability.

**Reoptimization cadence:** monthly at expiry (trailing 2y rolling, capped to data); quarterly regime re-eval. If forward degrades >40% vs backtest → STOP, reoptimize.

---

## 6. Infrastructure & Safety

**Hard rule: never heavy-exec inside the prod backend container.** Project memory is unambiguous — `docker exec nomadcurie_backend python` running feature builds / multi-underlying backtests OOMs (rc=137) → triggers a container **recreate** → `/app` bind mount re-syncs from image baseline (reverts edits) → OOM-killed procs leak DB connections → pool hits `max_connections=25` → `FATAL: too many clients` → `atm_watchlist_service` fails. A bare import or config read is fine; a backtest is not.

**Approved compute lanes:**
1. **Gann sweep sidecar** — `docker run -d --name <lane>-sweep --network tradebot_default --memory=900m <image> python <lane>/tune_sweep.py`. Direct asyncpg, no broker bootstrap, scan ~2s/6 underlyings. This is the template for **all** sweeps.
2. **Sniper shadow sidecar** — `sniper-shadow:latest` (image `deploy/Dockerfile.sniper`), persistent host dir **`/opt/sniper`** (outside the bind mount → survives recreates), reads candles directly from TimescaleDB, zero prod load. systemd `sniper-shadow.timer` (predict→score) + `sniper-retrain.timer`.
3. **Local off-DB-pull** — pull candles to a dev box, backtest/tune locally. Preferred for big sweeps.

**Memory guards proven to work:** `scan_concurrency=3` (not 6), per-run snapshot cache reused by `_refresh_open_positions`, `--memory=900m` cap. Keep multi-underlying sweeps to ≤6 names per process or one-at-a-time.

**Deploy mechanics (when promoting tuned params/code):** push via base64-over-SSM to `/opt/TradeBot/backend/...` then `docker restart nomadcurie_backend` (restart **preserves** bind-mount edits; recreate reverts them). Chunk files >~70KB (SSM ~100KB limit silently truncates). `rm -rf .../__pycache__` before restart. **Durable** deploy = commit to repo + rebuild image. Sweep artifacts/params live in `/opt/sniper` or `runtime/index_analytics_data/`, never in the bind mount.

**DB recovery if pool saturates:** `docker restart nomadcurie_db` then `docker restart nomadcurie_backend` (healthy steady state ~18/25 connections).

---

## 7. Acceptance Gates & Promotion Criteria

A tuned param set advances through three gated stages. **Any failure halts promotion.**

### Stage A — Backtest → eligible for paper
All six Section 4.2 gates pass **on the held-out OOS** (WFE≥0.50, DSR≥0.40, PBO<0.40, MinBTL satisfied, ≥100 OOS trades, MC 5th-pct Sharpe>0.30), **plus** profitable in all 3 HMM regimes, **plus** positive net of Section 4.5 costs. Plateau-selected (not peak). Log N (param×window combos) with the DSR.

### Stage B — Paper forward-test → eligible for live
4–6 weeks live-data paper via the lane's paper store (directional_options proposal / `PaperPositionBook` / `FMPPaperStore` / `CBEPaperBook` / commodity agent journal). Compute **degradation factor** = live/backtest per metric:

| Metric | Target ratio | Hard fail |
|---|---|---|
| Win rate | within ±10pp | degrades >20% |
| Profit factor | 0.70–0.95 | flips negative |
| Avg trade P&L | within ±30% | — |
| Max drawdown | ≤ backtest 95th-pct | blows past 95th-pct |
| Trade frequency | ≥50% of backtest | drops >50% |

All metrics in [0.70, 0.95] band → promote. Multiple metrics degrading >30% → back to Section 5 reoptimize.

### Stage C — Live (micro → scale)
- Start **1–5% of target capital**, 4–8 weeks, same degradation gates.
- If pass: scale +25% every 2 weeks; **stop if drawdown exceeds backtest 95th-pct**.
- Monthly: live Sharpe must stay above the MC 5th-pct bootstrap; quarterly reoptimize at expiry.
- **Sniper exception:** promoted only by A/B improvement of the host (auction_intelligence) paper metrics with overlay on vs off — never as a standalone capital lane.

### Lane-specific promotion notes
- **gann_tp_delta:** already paper-validated (NIFTY +3.24R PF2.62, SENSEX +2.44R PF1.30 edge; BANKNIFTY/CRUDEOIL negative — **exclude/drop** per tuning). Ready for Stage C micro on the edge underlyings only.
- **commodity:** blocked at Stage A until 1m MCX backfill; CRUDEOIL flagged for drop.
- **cbe_scanner / sector_interaction:** blocked at Stage A until RS snapshots accrue ≥3mo.
- **nse S2 / directional / auction:** Stage A pending greeks + 5m option + CVD arrangement; run forward-only paper meanwhile.

---

## 8. Phased Roadmap & Milestones

**Phase 0 — Foundations (Week 1–2)**
- [ ] Stand up shared guarded loader (reuse `nomad_sniper` dedup/RTH/outlier guards) as `analysis/safe_candles.py`; every sweep imports it.
- [ ] Start CRITICAL accruing backfills **now**: daily sector/stock RS snapshot cron; verify `hourly_profiles` depth; kick off MCX 1m + index-futures 1m Upstox backfill.
- [ ] Add `paper_engine/costs.py` integration hook to each backtester's close path; build slippage calibrator from existing paper fills.
- *Milestone:* every backtest applies guards + costs; backfills running.

**Phase 1 — Methodology harness (Week 2–4)**
- [ ] Build `analysis/validation_metrics.py`: WFE, DSR, PBO, MinBTL, MC reshuffle, bootstrap CIs — log N per sweep.
- [ ] Build `analytics/regime_hmm.py` (3-state HMM) for regime-conditional gating.
- [ ] Add `optuna` + plateau-selection helper; generalize the gann `tune_sweep.py` into a reusable sweep harness.
- *Milestone:* any lane can be run through all six gates + regime + MC with one call.

**Phase 2 — Lane sweeps, data-ready first (Week 4–8)**
- [ ] gann_tp_delta: re-run floor sweep through the new gate harness (validate the 5.0/6.0 floors hold OOS). *(ready now)*
- [ ] fractal_market_profile + nse S1: confidence-floor / MACD-param sweeps. *(data available)*
- [ ] directional_options: build `tune_sweep.py`, grid timeframe×lookback×score-floor. *(IV-only)*
- [ ] auction_intelligence: build `ai_backtest.py`; run Gate-B walk-forward.
- *Milestone:* 4 lanes through Stage A with logged gates.

**Phase 3 — Data-gated lanes (Week 6–12, as backfills land)**
- [ ] commodity: walkforward + per-underlying floor sweep on backfilled MCX 1m; decide CRUDEOIL drop.
- [ ] cbe_scanner: parametrize layer weights; walk-forward once RS snapshots ≥3mo.
- [ ] sniper: continue shadow→score accrual toward 600 OF-bearing labels; first gated retrain.
- [ ] sector_interaction: VAR/Granger forecast-skill walk-forward.
- *Milestone:* all tradeable lanes have a Stage-A verdict.

**Phase 4 — Paper → live promotion (Week 12+)**
- [ ] Run Stage-B paper (4–6 wks) on every Stage-A pass; compute degradation factors.
- [ ] Promote gann edge underlyings to Stage-C micro; scale per Section 7.
- [ ] Cross-strategy KPI dashboard (consolidate the varied JSON/JSONL journals into one schema — addresses the "no unified metrics export" gap).
- *Milestone:* ≥1 lane live at micro size with full gate audit trail; monthly reoptimization cadence running at expiry.

**Standing cadence (steady state):** monthly reoptimize at expiry on trailing rolling window; quarterly regime re-eval + slippage recalibration; halt-and-reoptimize on any >40% forward degradation.

---

## Appendix — Known gaps to close (from infra/data/methodology maps)
- No standalone backtester for **auction_intelligence** (gate-based only) → `ai_backtest.py`.
- No parameter sweep for **directional_options / fractal_market_profile / cbe_scanner** → add Gann-pattern sweeps.
- **Optuna / plateau / CPCV / WFE / DSR / PBO / MinBTL / MC / HMM** not yet in repo → Phase 1 harness.
- **Greeks history, 5m index options, MCX 1m, sector RS, per-min CVD, L2-L5 depth** → Section 2.3 backfill plan.
- **Heterogeneous paper journals** (per-lane JSON/JSONL) → unify to one trade-record schema (Phase 4 dashboard).

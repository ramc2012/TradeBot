# MACD Refined

A standalone long-premium options lane implementing
`STRATEGY-premium-macd-lowiv.md` end-to-end:

> Buy the single-leg ATM option (CE **or** PE) whose **option-premium MACD(12,26,9)**
> just crossed zero, with IV rank recorded as market-regime context (not an entry gate),
> sized to its live traded volume, held to the expiry-7d window, run as separate
> capped CE / PE books — calls carry up-markets, puts carry down-markets, volume
> gives ~6 days' and a directional heads-up.

## Layout

| File | Role |
|---|---|
| `config.py` | Frozen parameters from the study (spec §11A), data roots, live universe, label `MACD Refined`. |
| `indicators.py` | MACD, IV-rank, turnover, realised-vol, trailing turnover baseline. |
| `data.py` | Reads the repo-root `data/` dataset (30-min option premium candles + spot + catalogs + validated signals); builds per-underlying ATM-IV history; resolves current + next monthly expiry. |
| `signals.py` | One ATM premium-MACD zero-cross signal per leg per cycle (spec §4), with liquidity / window gates (§5) and IV-regime mapping. |
| `risk.py` | Liquidity-scaled sizing (§6), kill switch (§9). |
| `backtest.py` | Portfolio overlay (separate CE/PE books, slots, one-leg-per-stock, daily cap, compounding sizing) over either the **research** (validated) or **engine** (causal) signal source. |
| `paper.py` | File-backed CE/PE paper book + journal + capital summary (spec §7–§9). |
| `live.py` | Fetches current + next monthly expiry chains via the active broker, **persists per-contract volume/turnover** (the "volume tracking" the spec asks for), and generates causal proposals for the paper book. |
| `service.py` | Orchestrator + singleton `macd_refined_service`. |

## Backtest: two honest views

`service.backtest_compare()` returns both:

- **`research`** — replays `data/signals/macd_signals.parquet` (the project's own
  validated signal set the doc is built from). Reproduces the documented edge
  (~86% win, +114% median). **The rupee level of the equity curve is a
  compounding artifact (spec §10) — trust the structure, not the level.**
- **`engine`** — the CAUSAL forward generator (no hindsight leg selection),
  with the −50% catastrophe stop and round-trip slippage. Its numbers are
  materially lower; **this is the walk-forward gap the deploy protocol (§11)
  exists to measure**, and the doc explicitly flags the research win-rates as
  optimistic upper bounds.

> Why the gap: the research selects the winning leg per cycle with cycle-direction
> information that is not cleanly available at entry. A deployable, causal long-ATM
> book is roughly a coin-flip on direction with option convexity — so the lane's
> real value is the live paper walk-forward, not the backtest level.

## Live / next-month positioning

The market-hours supervisor runs `service.run_live_cycle()` on a cadence. Each
cycle resolves the **current + next monthly expiry** for the live universe,
fetches those chains, persists per-contract volume/turnover under
`runtime/macd_refined/volume_tracking/<SYMBOL>.parquet`, and feeds causal
proposals to the paper book. With no broker authenticated it degrades cleanly
(`broker_ready: false`) and touches nothing.

## Implemented vs deferred (honest map against the spec)

**Implemented:** premium-MACD(12,26,9) ATM zero-cross entry (§4.1–4.3); IV-rank
mapping computed causally as-of the signal date (§5.1); liquidity floor on trailing daily turnover (§5.2);
entry window expiry−7d (§5.3); trend leg-selection CE/PE with a CE/PE
turnover-imbalance fallback (§4.5, §5.4); liquidity-scaled compounding sizing
(§6); separate CE/PE books, slot limits, one-leg-per-stock, daily new-entry cap
(§7); hold-to-window exit + −50% catastrophe stop (§8); kill switch on rolling
win-rate / drawdown (§9); frozen params + research-vs-causal split (§11).

**Deferred (config placeholders present, logic NOT wired — flagged in `config.py`):**
- The **two-stage early-warning starter** (§4.4/§6 — deploy ⅓ on a >2× volume
  surge ~6 days before the MACD cross). The surge ratio + CE/PE turnover bias are
  *computed and recorded as context*, and the imbalance is used as the
  leg-selection fallback, but no standalone starter trade is opened.
- **Starter invalidation** (§8) — pairs with the unimplemented starter.
- **Profit-scaling** (§8) — this is the spec's *optional* add-on; the spec default
  is pure hold, so leaving it off is spec-faithful. Toggle `profit_scale_enabled`.

**Modeling notes:** the paper-book kill switch / drawdown are computed on the
realized closed-trade equity curve (standard); open unrealized marks gate via the
mark-to-market on each cycle but are not folded into the drawdown figure.

## API (`/api/macd-refined`)

`summary`, `backtest` (`?source=research|engine`), `backtest-compare`,
`positioning`, `paper-positions`, `paper-journal`, `paper-summary`,
`run-live-cycle` (POST), `reset-paper` (POST).

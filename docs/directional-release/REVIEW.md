# Directional paper desk review — 9 September 2026

Scope: the existing fast directional options book and its distribution workspace. Paper execution only. The separate index swing book retains its identity, positions and journals; its surface and cost utilities are reused. No alpha or model has been promoted to live trading.

## Review of the memo

The direction is useful: invest in stable surface coordinates, execution fidelity and causal validation before adding more directional indicators. A contract series can be modelled when expiry, moneyness and surface dynamics are accounted for; constant coordinates make that decomposition easier rather than making contract modelling impossible.

The supplied inventory is partly outdated. This checkout already contains the index-only `directional_options/vol` substrate (Black-76 inversion, SVI, quarantine, constant-maturity/delta coordinates, realised volatility and cones), a separate 1–5-session index paper engine, Greek attribution, and participant OI ingestion. This release extends and connects those components rather than building competing fetchers or books.

| Proposal | Decision and implementation |
|---|---|
| Constant maturity / constant delta | Reuse stored front and true 30-day coordinates. UI separates them; missing brackets remain gaps. ATM means at the forward. RR = call IV − put IV; BF = mean wing IV − ATM IV. |
| Arbitrage-free SVI | Correct the total-variance wing slope bound (2, independent of maturity), check minimum variance and preserve residual quarantine. Finite-grid checks do **not** establish a globally arbitrage-free surface. |
| RND | New density chart over observed support, checked for nonnegative density and finite probability mass. Omitted tails are never normalised away. This is risk-neutral pricing information, not a forecast of physical returns. |
| IV−RV / cones | Display shared causal IV/RV series and the existing RR/BF coordinates. Existing cone computation remains the canonical implementation. Short chain history does not support an IV-rank claim. |
| Vanna / charm / stress grids | Reuse Black-76 Greeks; new per-unit ATM call/put grid at forward ±3%, IV ±2 vol points and one calendar day. Hypothetical contracts, sticky-strike IV, zero rates, before charges; not portfolio P&L. |
| Minimum-variance delta | Correct the existing label to sticky-log-moneyness skew proxy, including the index book UI and API metadata. A smile slope is not an empirically estimated conditional IV/spot response. No claim of an MV hedge. |
| Greek attribution | Already present on the separate index swing desk; link to that book rather than attributing its results to the fast directional ledger. |
| Spread in vol points / impact | New fast-book entries reuse the shared adverse-fill model and retain its component estimates, including half-spread/vega in vol points. Assumed liquidity/ADV and impact coefficients are priors, not calibrated execution evidence. |
| Forward GEX / dealer inventory | Existing GEX remains an assumption-based exposure view. A credible dealer inventory profile cannot be inferred from anonymous aggregate OI alone; do not label it observed inventory. |
| Leg-linked flow / counterparties / synthetic MBO | Feed lacks order identities and dependable spread linkage. Depth-five changes cannot uniquely reconstruct MBO or counterparties. These panels remain unavailable. |
| OF deseasonalisation and 30min–5day research | Keep as preregistered experiments with training-only time-of-day baselines, session-clustered errors, lagged fills and 2–3× cost stress. Near-zero IC is not proof of clock contamination. No unvalidated signal change. |
| Participant OI | Read the already-ingested NSE table. Display publication date and participant/instrument-class totals. It has no stock/index-name attribution and does not describe BSE SENSEX positioning. |
| Single stocks | No fitted surface is exposed for thin stock chains. Retain directional spot/positioning and payoff analysis; the existing fast-lane stock universe is not silently expanded. |

The weekly-expiry claim needs exchange qualification: NSE retains NIFTY weeklies, while BSE retains SENSEX weeklies. Expiry dates and lot sizes must continue to come from the contract catalog. The memo's delta-adjusted OI limits are regulatory position-limit definitions, not cash budgets or directional signals. MWPL comparisons spanning definition changes need explicit regimes; the current missing MWPL series cannot support a historical feature.

## Execution defects corrected

- Carried premiums and untimestamped cache values could create protective/expiry fills. Quote observation time is now checked, including a final book-level check. Unpriced exits remain visibly pending and retain their holdings; zero-value observed exits are preserved.
- Held stocks could stop receiving marks when omitted from the rotating signal scan. A bounded whole-book pass now marks and manages protective exits independently of signal selection.
- Per-instance locks did not protect a shared database book. All normal book mutations now hold a cross-process lock on the shared durable volume. Final funding and loss-window checks run within that lock. New entries reserve whole-lot premium, entry charges and a conservative charge buffer; they cannot spend unrealised gains.
- Paper-specific daily/weekly loss limits apply even when the app's signal-validation flag is uncapped. These are funding controls, not confidence/regime signal filters. No premium-per-trade percentage cap was introduced.
- New trades use a versioned shared spread/impact prior with adverse tick rounding and dated NSE/BSE fees. Zero-price exits cannot manufacture one tick of proceeds. Gross P&L includes fill slippage; only charges are deducted afterward, exactly once. Existing entry records and closed history are retained.
- Live decision time is recorded separately from the signal bar. The selector now measures remaining time to 15:30 IST and refuses expired options, rather than flooring expired contracts to positive DTE. Planned stop loss is separate from full premium at risk.
- Surface inversions/fits are content-addressed across the builder and paper consumers, and run off the event loop. Shared deployments also coalesce chain refreshes before broker I/O and analytics. Distribution page reads do not fetch broker data or refit surfaces.

## What this release does not establish

This is a funded paper simulator, not an exchange matching engine or evidence of a profitable strategy. It does not simulate queue position or partial fills. Current LTPs plus adverse priors are not actual bid/ask executions, and old and new fill cohorts should not be pooled without qualification. The adaptive policy still needs independent causal holdouts. Broker/exchange fee schedules require ongoing reconciliation. Pending expired stock options require an observed exit or an explicit settlement workflow; they must not be liquidated at a fabricated price. There is no fabricated settlement or live-order path in this release.

The process lock assumes the current deployment's shared runtime volume. Multi-host deployments need a database transaction/advisory-lock design. The existing policy/telemetry callbacks are not a transactional outbox; the authoritative position book remains the accounting source.

## Sources checked

- [Gatheral and Jacquier: Arbitrage-free SVI volatility surfaces](https://arxiv.org/abs/1204.0646) — total variance, butterfly checks and density.
- [Hull and White: Optimal Delta Hedging](https://www-2.rotman.utoronto.ca/~hull/downloadablepublications/Optimal%20Delta%20Hedging.pdf) — conditional volatility response for an MV hedge.
- [NSE participant reports](https://www.nseindia.com/all-reports-derivatives) — published participant-wise OI.
- [NSE weekly-expiry discontinuation circular](https://nsearchives.nseindia.com/content/circulars/FAOP64506.pdf) and [exchange expiry coverage](https://support.zerodha.com/category/trading-and-markets/trading-faqs/f-otrading/articles/weekly-expiries-discontinued-for-certain-index-derivatives).
- [NSE STT schedule](https://www.nseindia.com/static/products-services/equity-derivatives-securities-transaction-tax) and [FATAX73524](https://nsearchives.nseindia.com/content/circulars/FATAX73524.pdf) — option-sale STT changes from 0.10% to 0.15% on 1 April 2026.
- [BSE transaction-charge filing](https://nsearchives.nseindia.com/corporate/BSE_27092024184037_NSEintimation.pdf) — SENSEX options ₹3,250/crore premium turnover.

The full Claude artifact could not be accessed. This review uses the supplied memo and the checked checkout/runtime.

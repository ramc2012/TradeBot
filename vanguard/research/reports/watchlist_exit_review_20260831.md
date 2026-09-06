# VANGUARD watchlist and exit review — 31 August 2026

## Result and provenance

The ten-name source-session 28 August list is a **neural next-session option watchlist**, not the MP `gap_overnight` (BTST) book. It was frozen on Saturday 29 August at 14:08 IST from `mlp_quantile_v1_20260829T065450Z`. It could not have captured Friday's closing entry. The separate BTST book's BANKBARODA trade returned approximately +0.033% net on its stock-price proxy; its existing next-open exit remains unchanged.

Watchlist observation starts at the first 30-minute close on 31 August (available at 09:45 IST), not at the prior close. The same exact September contracts are retained. These are premium-price observations, not broker fills or proof that the underlying predictive model has an edge. The current model predicts the next 30-minute return; using its ranking for the next session is a different, unvalidated target.

## Corrected premium returns

The old best/worst calculation included the entry candle's pre-entry extremes. Corrected extremes exclude that candle. All ten original performance records are retained in `performance_audit`.

| Contract | Post-entry peak, gross | Hold to 15:15, gross | Runner exit, net |
|---|---:|---:|---:|
| AUROPHARMA26SEP1660CE | +93.00% | +63.87% | +62.87% |
| KAYNES26SEP3900PE | +67.20% | +67.20% | +66.20% |
| GRASIM26SEP3280CE | +46.36% | +46.36% | +45.36% |
| FORCEMOT26SEP18000CE | +11.11% | +11.11% | -19.24% |
| JSWENERGY26SEP560CE | +7.06% | +7.06% | -26.88% |
| LICI26SEP420CE | +4.35% | +1.09% | -20.02% |
| TORNTPHARM26SEP5000CE | +4.19% | -24.25% | -16.83% |
| RADICO26SEP4700CE | +0.00% | -40.26% | -26.97% |
| CAMS26SEP720PE | +0.00% | -57.75% | -25.65% |
| CUMMINSIND26SEP5300CE | +0.00% | -0.80% | -1.80% |

Peaks are observed opportunities, not attainable exit promises. Prices are evaluated only through 15:15 IST, the close of the last full 30-minute candle. The archive lacked the final 15-minute candle for every listed contract; no synthetic 15:30 mark is used. Worst/peak opportunity measurements continue through the common horizon, independently of whether a runner already exited.

## Versioned exit experiment

`watchlist_runner_v1` is a shadow comparison only and has no ticket/order path:

- Initial stop: 15% below entry.
- After a completed candle reaches +20%, raise the stop to entry plus assumed costs.
- After +30%, protect 50% of the peak gain, without a profit cap.
- A raised stop becomes effective in the next candle, never earlier in the candle that set the peak.
- A gap through an existing stop fills at the worse opening price; a 15% stop is not a guaranteed maximum loss.
- Scheduled exit: 15:15 IST; no overnight extension.
- Reject incomplete/invalid paths and missing candles before an unresolved exit.
- Control: the same initial stop and scheduled exit, without trailing or breakeven ratchets.

| Paired comparison, 10 contracts | Equal-weight mean net premium return |
|---|---:|
| Hold to 15:15 | +6.36% |
| Profit-protection runner | +3.70% |
| Stop-only control | +3.70% |

All net figures subtract the **same assumed 1% round-trip premium cost**, not measured spreads, slippage, taxes or fees. The runner underperformed holding by 2.66 percentage points, and its trailing component added no benefit over the stop-only control in this session. It reduced the worst single-contract net loss from -58.75% to -26.97%, while stopping out some contracts that later recovered. This is not a capital-sized portfolio or portfolio drawdown calculation.

The policy was registered at 2026-08-31 16:03:21 UTC, after the observation session. This replay is explicitly retrospective, not out-of-sample validation. Parameters were not tuned to maximize these results. No claim of improved expected returns is justified from one list.

## Implemented integrity and desk improvements

- Completed-candle timing and exact same-bar option marks; no two-day stale quote substitution.
- Stored source time, actual decision time and timing policy; delayed replays cannot masquerade as timely decisions. Existing predictions are not overwritten on replay.
- Legacy timing rows excluded from audited model outcomes; latest-bar counts separated from cumulative counts.
- Automatic nightly model retraining removed; standalone training registers shadow models and cannot automatically activate or retire an existing paper model.
- M2 report accumulation fixed; Decision Flow's missing `side_momentum` SQL projection fixed.
- Historical watchlist session selector, separate peak/hold/runner displays, paired stop-only control, explicit neural-versus-BTST labels and navigation.
- Additive migration `016_watchlist_audit_and_exits.sql`, including immutable policy registration and preservation of prior performance values.

The 31 August frozen list contains zero qualifying names. It was not rewritten to manufacture a fresh list after the timing fixes. Select **2026-08-28** in Watchlist to inspect the ten-name observation above.

## Verification and preservation

- Entire VANGUARD test suite: **456 passed** using the project's host virtual environment.
- VANGUARD router tests: **25 passed**. These are focused router tests, not the entire TradeBot backend suite.
- Production frontend build passed. Backend and VANGUARD cycle restarted; frontend rebuilt/recreated. The strategy process remained running.
- Browser checks covered historical selection, return cards, model audit and separate BTST book. Watchlist and Decision Flow APIs returned HTTP 200.
- MP positions 15/16/17 (ICICIBANK, RBLBANK, HDFCBANK) preserved at their original entry prices; central ITC paper position still 1,725 units. VANGUARD outcomes remain at zero open positions. All three stored neural models remain shadow.
- Central NSE auto-run remains paused with its kill switch armed. No credentials, broker-order settings or held-position exits changed.
- Scoped pre-change source/data backups: `/tmp/vanguard-improvements.6RSN0y`. Source changes remain uncommitted alongside existing user changes; do not roll back the whole checkout or restore the whole database over subsequent trading data.

## Still required before promotion

Collect genuinely prospective observations under the frozen model and fixed exit policy; evaluate enough independent sessions using chronological holdouts and measured execution costs. A next-session-specific predictor and a frozen v1/v2 comparison remain research work, not implemented or validated forecasting improvements. The new runner must not be activated simply because three option premiums had large peaks.

The Decision Flow HTTP error is repaired, but that tab still describes the legacy sequential-filter funnel. Its individual leg counts can increase between stages and must not be interpreted as neural-model attrition. Converting that older visualization to the neural selector's actual decision audit remains a separate UI improvement; the Model tab is the relevant source for model status.

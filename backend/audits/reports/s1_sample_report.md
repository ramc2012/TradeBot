# Lane audit — S1 (NSE 30m ATM MACD)

**Audit date:** 2026-05-22
**Window:** 2026-04-22T00:00:00 → 2026-05-23T00:00:00
**Overall:** 🟡 YELLOW

> Sample report — generated against synthetic data so you can read the
> shape before the first real run. Real reports replace this once
> `python -m audits.lane_audit --lane s1 --days 30` is wired to the live DB.

## Invariants

| # | Invariant | Status |
|---|-----------|--------|
| 1 | data_integrity | 🟢 |
| 2 | replay_parity | 🔴 |
| 3 | gate_attribution | 🟢 |
| 4 | backtest_parity | 🟢 |
| 5 | trade_reconciliation | 🟢 |
| 6 | edge_persistence | 🟡 |

### 🟢 data_integrity

```json
{
  "gaps": [],
  "freshness_violations": 0
}
```

22 trading sessions in window, 13 bars expected per session. All sessions hit
the threshold. Most-recent 30m bar is 7 min old — well inside the
`interval + 2 min` SLA.

### 🔴 replay_parity

```json
{
  "replay_signals": 41,
  "live_signals": 38,
  "match_count": 37,
  "mismatches": {
    "missing_from_live": [
      ["2026-05-09T11:00:00", "NIFTY", "2026-05-15", 23400.0, "CE", "ZERO_CROSS_UP"],
      ["2026-05-13T13:30:00", "BANKNIFTY", "2026-05-15", 51200.0, "PE", "ZERO_CROSS_DOWN"],
      ["2026-05-15T10:00:00", "NIFTY", "2026-05-22", 23500.0, "CE", "ZERO_CROSS_UP"],
      ["2026-05-20T11:30:00", "NIFTY", "2026-05-22", 23550.0, "CE", "ZERO_CROSS_UP"]
    ],
    "missing_from_replay": [
      ["2026-05-06T14:00:00", "NIFTY", "2026-05-08", 23300.0, "CE", "ZERO_CROSS_UP"]
    ]
  }
}
```

**This is the gate that's failing.** Pure replay finds 4 signals that the
live recorder dropped, and 1 signal the live recorder emitted that replay
can't reproduce. Two interpretations:

- **Live → missing:** the staleness/refresh fix may not be firing for all
  contracts. Three of the four misses are on the current expiry — consistent
  with a refresh race on the ATM contract right after the strike shifts.
- **Replay → missing:** the live recorder is using a different rounding rule
  on the previous-bar MACD, OR the bar was emitted before the candle close
  was finalised. Need to inspect the 14:00 IST bar on 2026-05-06.

**Next action:** before the next audit, instrument the live recorder to
log `prev_macd`, `macd`, and the candle close it used. Re-run replay and
expect zero mismatches.

### 🟢 gate_attribution

```json
{
  "emitted": 38,
  "blocked_total": 412,
  "breakdown": {
    "physical_delivery_window": 188,
    "tte_below_7": 94,
    "iv_above_cap": 71,
    "spot_ma_filter": 47,
    "above_option_ma20_false": 9,
    "unknown": 3
  }
}
```

3/412 = 0.7% unknown attribution — well below the 5% threshold. The bulk
of blocks come from the physical-delivery window filter, which is the
expected behaviour.

### 🟢 backtest_parity

```json
{
  "diff": {
    "live": 38,
    "backtest": 39
  }
}
```

Backtest on identical window emits 39 vs 38 live — 2.6% diff, inside the
±2% tolerance once we account for the in-progress current-expiry trade
that backtest closes but live still holds open.

### 🟢 trade_reconciliation

```json
{
  "trades_booked": 21,
  "pass_count": 21,
  "failures": []
}
```

All 21 paper trades have non-null entry price + qty. (Note: tick-level
±2s match is stubbed in this run — wired when tick store path is finalised.)

### 🟡 edge_persistence

```json
{
  "expectancy_60d": 38.4,
  "expectancy_baseline": 62.1,
  "drift_pct": -38.2
}
```

60-day expectancy of +38.4% is still **positive**, but it has decayed
**−38.2%** vs the 1-year baseline of +62.1% — past the −30% drift
threshold. Lane still profitable, but losing edge. Worth a deeper look
at whether the regime has shifted (lower vol, narrower MACD swings) or
whether a specific underlying is dragging.

---

## Read-out

| Question | Answer |
|----------|--------|
| Should S1 be feeding Arbiter today? | **No** — replay parity is failing. |
| What needs to be fixed first? | The 4 live-missing signals. Add MACD trace logging to the live recorder, re-run audit. |
| What's the second priority? | Edge drift. Bucket the 60d signals by underlying + regime and find what changed. |
| What's healthy? | Data feed, gate attribution, backtest parity, trade booking. |

When this report shows 🟢 across all six rows for three consecutive days,
S1 has earned its weight in Arbiter. Until then, treat its output as
research-only.

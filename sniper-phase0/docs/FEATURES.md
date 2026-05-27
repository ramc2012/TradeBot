# Feature dictionary

All features carry a `data_available_at` timestamp. The leakage guard (`features/base.py`) asserts `data_available_at <= decision_ts` for every value.

## Market Profile — intraday (`mp_*`)

Computed from ticks strictly before `decision_ts`.

| Feature | Description | Availability |
|---|---|---|
| `mp_dist_poc_pct` | % distance from spot to POC | `decision_ts` (built from ticks < `decision_ts`) |
| `mp_dist_vah_pct` | % distance from spot to VAH | same |
| `mp_dist_val_pct` | % distance from spot to VAL | same |
| `mp_in_value_area` | 1 if VAL ≤ spot ≤ VAH | same |
| `mp_above_vah` | 1 if spot > VAH | same |
| `mp_below_val` | 1 if spot < VAL | same |
| `mp_ib_position` | (spot - IB low) / (IB high - IB low) | same; NaN before IB completes |
| `mp_va_width_pct` | (VAH - VAL) / VAL × 100 | same |
| `mp_tpo_count` | Total TPO letters counted | same |

All NaN if fewer than 30 ticks observed before `decision_ts` (too early in session).

## Market Profile — prior session (`mp_prev_*`)

Computed from the previous business day's completed session. `data_available_at` = prior session close (≪ today's `decision_ts`), so these never leak.

| Feature | Description |
|---|---|
| `mp_prev_dist_poc_pct`, `mp_prev_dist_vah_pct`, `mp_prev_dist_val_pct` | % distance from spot to prior POC/VAH/VAL |
| `mp_prev_in_value_area`, `mp_prev_above_vah`, `mp_prev_below_val` | Boolean position relative to prior VA |
| `mp_single_prints_prev` | # of bins with exactly 1 TPO letter in prior session |
| `mp_poor_high_prev`, `mp_poor_low_prev` | Boolean — extreme bin had ≤2 TPOs (clean rejection without rotation) |
| `mp_nearest_hvn_dist_pct` | % distance from spot to nearest prior-session HVN |
| `mp_nearest_lvn_dist_pct` | % distance from spot to nearest prior-session LVN |
| `mp_value_migration_pct` | Reserved — needs 2-day MP cache; NaN in v0 |

## Order Flow (`of_*`, `book_*`)

| Feature | Description | Notes |
|---|---|---|
| `of_inferred_delta_5s/30s/300s` | Tick-rule signed volume over windows ending strictly before `decision_ts` | Inferred from tick prints — NOT true MBO |
| `of_tick_count_30s/300s` | Tick activity proxy | |
| `of_logret_5s/30s/300s` | Log-return over window | |
| `book_apparent_imbalance_l1` | (bid_qty_1 - ask_qty_1) / (bid_qty_1 + ask_qty_1) | NaN if no book snapshot |
| `book_apparent_imbalance_l5` | Same but summed across 5 levels | |
| `book_spread_bps` | (ask_1 - bid_1) / mid × 1e4 | |

**Honesty:** `inferred_*` and `apparent_*` prefixes are mandatory because NSE retail does not give us true MBO data. Do not rename these.

## Context (`ctx_*`)

| Feature | Description |
|---|---|
| `ctx_minutes_into_session` | 0 at 09:15, 375 at 15:30 |
| `ctx_dow` | 0=Mon, 4=Fri |
| `ctx_is_monday`, `ctx_is_friday` | Boolean |
| `ctx_dte` | Days to expiry |
| `ctx_is_expiry_day`, `ctx_is_expiry_week` | Boolean |
| `ctx_overnight_gap_pct` | (today_open - prior_close) / prior_close × 100 |
| `ctx_gap_up`, `ctx_gap_down` | Boolean, ±0.3% threshold |
| `ctx_atr_14d` | 14-day ATR of underlying |

Several context features are stubbed in v0 (expiry, gap, ATR) — they require a small underlying-OHLC backfill before they activate.

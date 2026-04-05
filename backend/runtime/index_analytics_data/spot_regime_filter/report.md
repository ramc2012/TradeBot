# Spot Regime Filter Analysis

Generated: 2026-04-04T14:17:43.676002+05:30
Trade source: `/Users/chinnadurairamachandran/Claude Projects/TradingBot/nomad-curie/backend/runtime/index_analytics_data/indicator_sweep_ohlc/trade_results.csv`

## Baseline

- Opportunities: 2016
- Win rate: 68.70%
- Avg return: -3.76%

## Robust Candidates

- `weekly_series | 15m | spot 15m | ema_macd_agree`
  opportunities=76, kept=41.99%, win=77.63%, avg=7.76%, median=11.95%
- `weekly_series | 15m | spot 30m | macd_bias`
  opportunities=83, kept=45.86%, win=73.49%, avg=7.02%, median=12.47%
- `weekly_series | 15m | spot 15m | ema_alignment`
  opportunities=101, kept=55.80%, win=74.26%, avg=4.86%, median=12.47%
- `monthly_series | 5m | spot 60m | ema_macd_agree`
  opportunities=36, kept=41.38%, win=75.00%, avg=10.17%, median=11.86%
- `monthly_series | 5m | spot 60m | ema_alignment`
  opportunities=38, kept=43.68%, win=73.68%, avg=9.94%, median=11.86%
- `monthly_series | 5m | spot 30m | ema_alignment`
  opportunities=36, kept=41.38%, win=72.22%, avg=8.07%, median=13.60%
- `monthly_series | 5m | spot 30m | macd_bias`
  opportunities=41, kept=47.13%, win=70.73%, avg=6.84%, median=14.40%
- `monthly_series | 5m | spot 15m | ema_alignment`
  opportunities=41, kept=47.13%, win=70.73%, avg=6.25%, median=12.79%
- `weekly_series | 15m | spot 30m | ema_macd_agree`
  opportunities=72, kept=39.78%, win=70.83%, avg=4.22%, median=11.11%
- `monthly_series | 5m | spot 30m | ema_macd_agree`
  opportunities=34, kept=39.08%, win=70.59%, avg=6.01%, median=11.86%
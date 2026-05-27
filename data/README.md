# NSE F&O Historical Data

**Exported:** 31 March 2026
**Source:** nomad-curie PostgreSQL (TimescaleDB)
**Format:** Apache Parquet (Snappy compression)
**Total:** 4.78M rows, 186 MB

---

## Folder Structure

```
data/
├── README.md                          ← You are here
├── option_candles/                    ← 3.15M rows, 151 MB
│   ├── expiry_2025-04-24.parquet
│   ├── expiry_2025-05-29.parquet
│   ├── ...
│   └── expiry_2026-03-24.parquet      (13 monthly expiry files)
│
├── spot_candles/                      ← 678K rows, 16 MB
│   ├── spot_3.parquet                 (360ONE)
│   ├── spot_A.parquet                 (18 symbols starting with A)
│   ├── ...
│   └── spot_Z.parquet                 (25 letter-group files)
│
├── catalogs/                          ← Reference data
│   ├── underlyings.parquet            (211 F&O underlyings)
│   ├── expiries.parquet               (2,237 underlying+expiry pairs)
│   └── contracts.parquet              (108K option contracts)
│
├── option_chain_metrics/              ← 838K rows, 17 MB
│   └── metrics.parquet                (OI, volume, PCR per bar)
│
└── signals/                           ← Strategy analysis outputs
    ├── macd_signals.parquet           (725 MACD zero-cross signals)
    ├── spot_ma_context.parquet        (631 spot MA20/MA50 context at signal)
    ├── option_ma_context.parquet      (630 option MA20/MA50 context)
    ├── option_vs_spot_macd_timing.parquet  (lead/lag timing analysis)
    ├── strike_ladder_returns.parquet  (ITM/ATM/OTM return comparison)
    ├── macd_regime_analysis.parquet   (3,655 CE+PE MACD regime outcomes)
    └── spot_moves_5pct_plus.parquet   (751 cycles with 5%+ spot moves)
```

---

## Data Schemas

### option_candles/ — 30-Minute Option Premium Candles

| Column | Type | Description |
|---|---|---|
| time | datetime | Bar timestamp (UTC) |
| underlying | string | Symbol (e.g., RELIANCE, SBIN, NIFTY) |
| expiry | date | Expiry date |
| strike | float | Strike price |
| option_type | string | CE or PE |
| open | float | Opening premium |
| high | float | High premium |
| low | float | Low premium |
| close | float | Closing premium |
| volume | int | Contracts traded |
| oi | int | Open interest |
| iv | float | Implied volatility (decimal, e.g., 0.25 = 25%) |
| delta | float | Option delta |
| gamma | float | Option gamma |
| theta | float | Option theta |
| vega | float | Option vega |
| underlying_price | float | Spot price at bar time |
| time_to_expiry_years | float | Time to expiry in years |

**Coverage:** 211 underlyings, 13 monthly expiries (Apr 2025 — Mar 2026), 9,502 contracts

### spot_candles/ — 30-Minute Underlying Spot Candles

| Column | Type | Description |
|---|---|---|
| time | datetime | Bar timestamp (UTC) |
| underlying | string | Symbol |
| open | float | Open price |
| high | float | High price |
| low | float | Low price |
| close | float | Close price |
| volume | int | Shares traded |
| oi | int | Open interest (futures, if available) |

**Coverage:** 211 underlyings, Mar 2025 — Mar 2026

### catalogs/ — Reference Data

**underlyings.parquet** — F&O universe

| Column | Type | Description |
|---|---|---|
| symbol | string | Trading symbol |
| kind | string | Instrument type |
| spot_instrument_key | string | Exchange key for spot quotes |
| underlying_key | string | Exchange key for derivatives |
| spot_range_start | date | Earliest spot data available |
| spot_range_end | date | Latest spot data available |

**expiries.parquet** — Expiry calendar

| Column | Type | Description |
|---|---|---|
| underlying | string | Symbol |
| expiry | date | Expiry date |
| previous_monthly_expiry | date | Previous month's expiry |
| selection_spot_price | float | Spot price when expiry was selected |
| contract_count | int | Number of option contracts |

**contracts.parquet** — Option contract catalog

| Column | Type | Description |
|---|---|---|
| instrument_key | string | Unique exchange key |
| trading_symbol | string | Full trading symbol |
| underlying | string | Symbol |
| expiry | date | Expiry date |
| strike | float | Strike price |
| option_type | string | CE or PE |
| lot_size | int | Lot size for trading |
| tick_size | float | Minimum price increment |
| freeze_quantity | int | Max order qty per order |
| candle_count | int | Number of candles synced |
| first_candle_time | datetime | Earliest candle |
| last_candle_time | datetime | Latest candle |

### option_chain_metrics/ — Aggregate OI & Volume

| Column | Type | Description |
|---|---|---|
| time | datetime | Bar timestamp (UTC) |
| underlying | string | Symbol |
| expiry | date | Expiry date |
| ce_contracts | int | Active CE contracts |
| pe_contracts | int | Active PE contracts |
| ce_oi | int | Total CE open interest |
| pe_oi | int | Total PE open interest |
| ce_volume | int | Total CE volume |
| pe_volume | int | Total PE volume |
| oi_pcr | float | Put-Call Ratio (OI) |
| volume_pcr | float | Put-Call Ratio (Volume) |
| underlying_price | float | Spot price |

### signals/ — Strategy Analysis Outputs

See `STRATEGY_DOCUMENT.md` in the parent directory for full methodology.

**macd_signals.parquet** — 725 validated MACD zero-cross signals

| Column | Description |
|---|---|
| underlying, expiry, opt_type | Instrument identifiers |
| spot_pct | Spot move % in the trading window |
| atm_strike | ATM strike used |
| signal_date | Date MACD zero-cross fired |
| entry_price | Option premium at entry |
| entry_iv_pct | IV at entry (%) |
| tte_at_signal_days | Days to expiry at signal |
| max_return | Maximum premium % return achieved |
| exit_window_return | Return at window end |
| target_10/20/30/50 | Return using fixed % target exits |
| trail_ret | Return using trailing stop exit |
| iv_adj_max/exit/trail/t50 | Volatility-adjusted returns |

---

## Quick Start

### Python (pandas)

```python
import pandas as pd

# Read all option candles for one expiry
df = pd.read_parquet('data/option_candles/expiry_2025-12-30.parquet')

# Read specific underlying's spot data
spot = pd.read_parquet('data/spot_candles/spot_R.parquet')
reliance = spot[spot['underlying'] == 'RELIANCE']

# Read all MACD signals
signals = pd.read_parquet('data/signals/macd_signals.parquet')

# Read contract catalog to get lot sizes
contracts = pd.read_parquet('data/catalogs/contracts.parquet')
```

### Python (polars — faster)

```python
import polars as pl

df = pl.read_parquet('data/option_candles/expiry_2025-12-30.parquet')
reliance = df.filter(pl.col('underlying') == 'RELIANCE')
```

### R

```r
library(arrow)
df <- read_parquet("data/option_candles/expiry_2025-12-30.parquet")
```

### DuckDB (SQL on files)

```sql
SELECT underlying, expiry, strike, option_type,
       MIN(time) as first_bar, MAX(time) as last_bar, COUNT(*) as bars
FROM read_parquet('data/option_candles/*.parquet')
WHERE underlying = 'RELIANCE'
GROUP BY ALL;
```

---

## Notes

- All timestamps are **UTC**. IST = UTC + 5:30.
- NSE market hours: 09:15-15:30 IST = 03:45-10:00 UTC.
- Option premiums are in INR per share (not per lot). Multiply by `lot_size` from contracts catalog for actual P&L.
- Trading window for stock options: `previous_expiry − 7 days` to `current_expiry − 7 days` (physical delivery constraint).
- Parquet files use Snappy compression. Readable by pandas, polars, pyarrow, DuckDB, Spark, R arrow, and most modern data tools.

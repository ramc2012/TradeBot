-- Tick storage hardening for the `market_ticks` TimescaleDB hypertable.
--
-- Context
-- -------
-- `market_ticks` is the dedicated tick store (one row per broker tick across
-- all subscribed index + option symbols). At ~1.6M rows/day it had grown to
-- ~1.3 GB with compression OFF and no retention — unbounded growth on a
-- single EC2 box.
--
-- A separate physical database for ticks is NOT warranted at this scale: a
-- TimescaleDB hypertable IS the purpose-built tick store, and a second DB
-- instance would add a container, a connection pool, and backups for no
-- benefit. The correct architecture is tiered storage:
--   * HOT  — Redis `tick:{symbol}` last-value key (sub-ms latest mark; see
--            market_data/data_router.py LATEST_TICK_KEY_PREFIX).
--   * WARM — recent uncompressed hypertable chunks (intraday MP/CVD reads).
--   * COLD — compressed chunks > 1 day old (~10-20x smaller).
--   * Dropped — chunks > 14 days (raw ticks beyond that have no value; the
--               durable history lives in the *_candles tables).
--
-- This script is idempotent (safe to re-run): every policy call uses
-- `if_not_exists => true`, and the compression ALTER is a no-op when already
-- set. Apply it whenever the hypertable is (re)created.

-- 1. Granular chunks so compression + retention act day-by-day instead of on
--    coarse multi-day chunks. Affects NEW chunks only.
SELECT set_chunk_time_interval('market_ticks', INTERVAL '1 day');

-- 2. Enable native compression. Segment by symbol (ticks of one contract
--    compress together extremely well) and order by time descending so the
--    newest rows in a compressed chunk decompress first.
ALTER TABLE market_ticks SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'symbol',
    timescaledb.compress_orderby = 'time DESC'
);

-- 3. Compress chunks older than 1 day (yesterday's ticks and earlier).
SELECT add_compression_policy('market_ticks', INTERVAL '1 day', if_not_exists => true);

-- 4. Drop chunks older than 14 days. The candle tables (underlying_spot_candles,
--    option_premium_candles, index_futures_candles) hold the durable history;
--    raw ticks past two weeks aren't read by any strategy.
SELECT add_retention_policy('market_ticks', INTERVAL '14 days', if_not_exists => true);

-- 5. One-off: compress existing eligible chunks immediately so the space is
--    reclaimed now rather than waiting for the background job's first run.
--    (Safe to re-run — already-compressed chunks are skipped.)
SELECT compress_chunk(c, if_not_compressed => true)
FROM show_chunks('market_ticks', older_than => INTERVAL '1 day') c;

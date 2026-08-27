-- Vanguard schema, migration 003: explicit instrument fields on tickets.
--
-- 002 stored the tradable contract only as the encoded `instrument` string
-- (e.g. "TCS26AUG3500CE"). M9 (paper fill/exit simulation) and M8 (backtest
-- replay) both need to re-query option_premium_candles for that exact
-- contract's SUBSEQUENT prints -- doing that by re-parsing the encoded
-- string back into strike/expiry/option_type is fragile (NSE symbols like
-- "360ONE" contain digits, so a naive suffix-strip is the only safe parse,
-- and even that is needless risk for data M6 already had in hand at ticket
-- time). Store it explicitly instead. Additive-only, nullable (existing
-- rows from before this migration have no fills/outcomes yet to walk
-- forward, so backfilling them is unnecessary).

ALTER TABLE tickets ADD COLUMN IF NOT EXISTS strike NUMERIC;
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS option_type TEXT;   -- 'CE' | 'PE'
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS expiry DATE;
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS lot_size INTEGER;

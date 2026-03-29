CREATE OR REPLACE VIEW validation_cache_coverage_vw AS
WITH expiry_stats AS (
    SELECT
        e.underlying,
        COUNT(*) AS expiry_rows,
        COUNT(*) FILTER (WHERE e.selection_spot_price IS NOT NULL) AS expiries_with_selection_spot,
        MIN(e.selection_date) AS first_selection_date,
        MAX(e.selection_date) AS last_selection_date
    FROM fo_expiry_catalog e
    GROUP BY e.underlying
),
contract_stats AS (
    SELECT
        c.underlying,
        COUNT(*) FILTER (WHERE c.sync_status = 'complete') AS complete_contracts,
        COUNT(DISTINCT c.expiry) FILTER (WHERE c.sync_status = 'complete') AS expiries_with_complete_contracts,
        COALESCE(SUM(c.candle_count) FILTER (WHERE c.sync_status = 'complete'), 0) AS cached_option_candles,
        MIN(c.first_candle_time) FILTER (WHERE c.sync_status = 'complete') AS first_option_candle_time,
        MAX(c.last_candle_time) FILTER (WHERE c.sync_status = 'complete') AS last_option_candle_time
    FROM fo_contract_catalog c
    GROUP BY c.underlying
)
SELECT
    u.symbol AS underlying,
    u.kind,
    COALESCE(es.expiry_rows, 0) AS expiry_rows,
    COALESCE(es.expiries_with_selection_spot, 0) AS expiries_with_selection_spot,
    es.first_selection_date,
    es.last_selection_date,
    COALESCE(cs.expiries_with_complete_contracts, 0) AS expiries_with_complete_contracts,
    COALESCE(cs.complete_contracts, 0) AS complete_contracts,
    COALESCE(cs.cached_option_candles, 0) AS cached_option_candles,
    cs.first_option_candle_time,
    cs.last_option_candle_time,
    u.expiries_synced_at,
    u.spot_synced_at
FROM fo_underlying_catalog u
LEFT JOIN expiry_stats es
    ON es.underlying = u.symbol
LEFT JOIN contract_stats cs
    ON cs.underlying = u.symbol;


CREATE OR REPLACE VIEW validation_atm_monthly_pairs_vw AS
WITH paired_contracts AS (
    SELECT
        e.underlying,
        e.expiry,
        e.selection_date,
        e.selection_spot_time,
        e.selection_spot_price,
        ce.strike,
        ABS(ce.strike - e.selection_spot_price) AS strike_gap,
        ce.instrument_key AS ce_instrument_key,
        ce.trading_symbol AS ce_trading_symbol,
        ce.candle_count AS ce_candle_count,
        ce.first_candle_time AS ce_first_candle_time,
        ce.last_candle_time AS ce_last_candle_time,
        pe.instrument_key AS pe_instrument_key,
        pe.trading_symbol AS pe_trading_symbol,
        pe.candle_count AS pe_candle_count,
        pe.first_candle_time AS pe_first_candle_time,
        pe.last_candle_time AS pe_last_candle_time
    FROM fo_expiry_catalog e
    JOIN fo_contract_catalog ce
        ON ce.underlying = e.underlying
       AND ce.expiry = e.expiry
       AND ce.option_type = 'CE'
       AND ce.sync_status = 'complete'
    JOIN fo_contract_catalog pe
        ON pe.underlying = e.underlying
       AND pe.expiry = e.expiry
       AND pe.option_type = 'PE'
       AND pe.sync_status = 'complete'
       AND pe.strike = ce.strike
    WHERE e.selection_date IS NOT NULL
      AND e.selection_spot_price IS NOT NULL
),
ranked_pairs AS (
    SELECT
        p.*,
        ROW_NUMBER() OVER (
            PARTITION BY p.underlying, p.expiry
            ORDER BY
                p.strike_gap ASC,
                GREATEST(p.ce_candle_count, p.pe_candle_count) DESC,
                p.strike ASC
        ) AS atm_rank
    FROM paired_contracts p
)
SELECT *
FROM ranked_pairs;


CREATE OR REPLACE VIEW validation_chain_metrics_summary_vw AS
SELECT
    m.underlying,
    m.expiry,
    m.interval,
    COUNT(*) AS bar_count,
    MIN(m.time) AS first_bar_time,
    MAX(m.time) AS last_bar_time,
    AVG(m.oi_pcr) AS avg_oi_pcr,
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY m.oi_pcr) AS median_oi_pcr,
    AVG(m.volume_pcr) AS avg_volume_pcr,
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY m.volume_pcr) AS median_volume_pcr,
    AVG(m.ce_oi) AS avg_ce_oi,
    AVG(m.pe_oi) AS avg_pe_oi,
    AVG(m.ce_volume) AS avg_ce_volume,
    AVG(m.pe_volume) AS avg_pe_volume,
    AVG(m.underlying_price) AS avg_underlying_price
FROM fo_option_chain_metrics m
GROUP BY
    m.underlying,
    m.expiry,
    m.interval;

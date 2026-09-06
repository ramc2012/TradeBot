-- 012: paper book for the two validated Market-Profile edges (2026-08-29).
--
-- Separate from the option-ticket book (tickets/fills/outcomes) on purpose:
-- these are FUTURES-PROXY trades with their own lifecycle (overnight gap:
-- enter at the close, exit at the NEXT 09:15 open; oversold: hold 4 sessions,
-- NO stop -- the research showed a tight stop halves that edge), and mixing
-- them into the option book's capital would contaminate both P&L series.
--
-- Universe is the RESEARCHED one only: NIFTY, BANKNIFTY and the 16 bank
-- stocks. Every other name may carry the same flags in features_mp; trading
-- them would be extrapolation the research never tested.

CREATE TABLE IF NOT EXISTS mp_paper_trades (
    id            SERIAL PRIMARY KEY,
    strategy      TEXT        NOT NULL,   -- 'gap_overnight' | 'oversold_mtf'
    underlying    TEXT        NOT NULL,
    side          TEXT        NOT NULL DEFAULT 'long',
    signal_dt     DATE        NOT NULL,   -- session whose close generated the signal
    entry_px      NUMERIC     NOT NULL,   -- that session's close (futures close
                                          -- for the indices when available)
    entry_src     TEXT        NOT NULL,   -- 'futures' | 'spot'
    notional      NUMERIC     NOT NULL,
    cost_bp       NUMERIC     NOT NULL,   -- assumed round-trip cost, basis points
    exit_due_dt   DATE,                   -- oversold: 4th session; gap: next session
    exit_ts       TIMESTAMPTZ,
    exit_px       NUMERIC,
    exit_reason   TEXT,                   -- 'next_open' | 'h4_close'
    gross_ret_pct NUMERIC,
    net_ret_pct   NUMERIC,
    status        TEXT        NOT NULL DEFAULT 'open',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (strategy, underlying, signal_dt)
);

CREATE INDEX IF NOT EXISTS idx_mp_paper_trades_open
    ON mp_paper_trades (status, strategy) WHERE status = 'open';

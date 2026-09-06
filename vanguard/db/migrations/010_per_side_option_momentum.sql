-- 010: per-side (CE / PE) option momentum, and the leg that selects on it.
--
-- WHY. The lane chose its instrument with `option_type = "CE" if bullish else
-- "PE"` -- the option inferred from a view about the UNDERLYING. That treats a
-- contract as a direction token rather than as a traded instrument with its own
-- order flow. A call can be bleeding while the underlying drifts up (theta, an
-- IV crush, or simply nobody paying up for it), and a put can be accumulated
-- while spot goes nowhere.
--
-- Measured on 2026-08-28, across the names that had a flow score:
--
--     flow said        side had momentum    side did NOT
--     bullish (CE)            12                 47
--     bearish (PE)             2                 92
--
-- i.e. 139 of 153 tickets would have been placed into a contract that was not
-- being accumulated. The two sides are also not mirror images -- 14 names had
-- BOTH sides short_covering and 11 had BOTH long_unwind, states a mirrored
-- model cannot express -- so the side's own reading cannot be derived from the
-- underlying's direction even in principle.
--
-- ce_state / pe_state apply classify_oi_state() to each side's OWN open
-- interest and OWN OI-weighted premium, rather than to combined OI against
-- spot. Every statement is additive, per db/apply.py's contract.

ALTER TABLE features_flow ADD COLUMN IF NOT EXISTS ce_state TEXT;
ALTER TABLE features_flow ADD COLUMN IF NOT EXISTS pe_state TEXT;

-- The journal records the inputs it judged on, so the two states travel with
-- the evaluation, plus the verdict of the leg that reads them.
ALTER TABLE candidate_evaluations ADD COLUMN IF NOT EXISTS ce_state TEXT;
ALTER TABLE candidate_evaluations ADD COLUMN IF NOT EXISTS pe_state TEXT;
ALTER TABLE candidate_evaluations ADD COLUMN IF NOT EXISTS leg_side_momentum BOOLEAN;

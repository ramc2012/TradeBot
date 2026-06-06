/**
 * Shapes for the Sniper desk, built from the real backend payloads:
 *   GET /api/auction-intelligence/sniper-signal           -> { signals: { [SYM]: SniperSignal } }
 *   GET /api/auction-intelligence/sniper-signal?symbol=X  -> { symbol, signal: SniperSignal & { age_sec } | null }
 *
 * SniperSignal is the dataclass in backend/auction_intelligence/sniper_signal.py.
 * The isolated sidecar reduces the per-horizon excursion estimator to ONE
 * directional call before POSTing (sniper_sidecar.py::_reduce_to_signal):
 *   direction      LONG | SHORT | FLAT
 *   magnitude_atr  |signed expected favorable excursion| in ATR units
 *   confidence     tanh(magnitude / CONF_SCALE) in 0..1
 *   up_atr/down_atr predicted upside / downside excursion (ATR)
 *   extras         free-form bag; may carry has_options / has_live_of / scorer bits
 */

export type SniperSignal = {
  symbol: string;
  direction: string; // LONG | SHORT | FLAT
  magnitude_atr: number;
  confidence: number; // 0..1
  horizon: string; // e.g. "60m", "1d", "eod"
  decision_time?: string | null;
  model?: string | null;
  up_atr?: number | null;
  down_atr?: number | null;
  extras?: Record<string, unknown> | null;
  received_at?: string | null;
  age_sec?: number | null;
};

/** A flattened row used by the board / quadrant / ladder. */
export type SniperRow = SniperSignal & { symbol: string };

export type SniperSignalsResponse = { signals?: Record<string, SniperSignal> };
export type SniperSingleResponse = { symbol: string; signal: (SniperSignal & { age_sec?: number }) | null };

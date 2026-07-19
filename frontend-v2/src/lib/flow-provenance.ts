/**
 * flow-provenance — source grading, and the FEATURE-AWARE correction to it.
 *
 * ─── Why this module exists (2026-07-19) ────────────────────────────────────
 *
 * Grading used to be purely SOURCE-aware: `market_ticks` in ⇒ `observed` out,
 * and every consumer then rendered its order-flow panel as REAL/observed. That
 * overstates the evidence, and the backend says so in plain text:
 *
 *   backend/analytics/orderflow.py (module docstring)
 *     "Indian retail brokers do not push public trade prints to subscribers,
 *      so true Lee-Ready CVD (each trade tagged buy/sell by aggressor) is not
 *      available. These functions approximate the same intuitions from OHLCV
 *      bars + L1 snapshots."
 *
 * Ground truth in the schema (db/migrations/versions/001_initial_schema.py:118):
 * `market_ticks` stores time, symbol, ltp, OHLC, CUMULATIVE volume, oi, bid,
 * ask, bid_qty, ask_qty. There is NO trade_id, NO last-trade quantity and NO
 * broker-supplied aggressor/side. `institutional_convergence/engine.py`
 * `build_footprint` buckets `ltp` and infers the buy/sell split heuristically.
 *
 * So the honest split is:
 *
 *   the QUOTE STREAM and the OHLCV BARS  → genuinely OBSERVED
 *   every buy/sell ATTRIBUTION built on  → MODELLED FROM QUOTES
 *   them (CVD, footprint buy/sell split,
 *   delta, aggression, absorption)
 *   an aggressor-tagged TRADE PRINT tape → UNAVAILABLE (we do not have one)
 *
 * `classifySourceGrade(source)` with no feature keeps its ORIGINAL, unchanged
 * behaviour — it answers "how was this source obtained?", which is what the
 * readiness gates in `OrderflowWorkbench.evaluateReadiness` and
 * `MpDesk.ofBadgeOf` ask. Passing a `feature` answers the sharper question
 * "how was THIS NUMBER obtained?", which is what every flow surface must show.
 *
 * Pure module: no React, no imports, no I/O — so it is unit-testable on its
 * own (see `frontend-v2/tests/flow-provenance.test.ts`).
 */

// ─── Vocabulary ─────────────────────────────────────────────────────────────

export type SourceGrade =
  | "observed"
  /** A genuine book/tape rebuild whose per-event side is still inferred. */
  | "reconstructed"
  /**
   * Derived from an OBSERVED quote/bar stream, but the buy/sell attribution
   * on top of it is a heuristic — there is no aggressor tape to check it
   * against. CVD, footprint sides, delta, aggression and absorption are all
   * this grade no matter how good the underlying quote stream is.
   */
  | "modelled_from_quotes"
  | "modelled"
  | "bar_inferred"
  | "unavailable";

/** Badge variants supported by `StatusBadge` — the contract emits only these. */
export type BadgeVariant = "neutral" | "success" | "warn" | "error" | "info";

/**
 * What KIND of number is being graded.
 *
 *   quote        — ltp / bid / ask / bid_qty / ask_qty snapshot        (observed)
 *   bar          — OHLCV bar                                          (observed)
 *   flow_attribution — CVD, footprint buy/sell split, delta,
 *                  aggression, absorption, imbalance                  (modelled)
 *   trade_print  — aggressor-tagged prints. WE DO NOT HAVE THESE from
 *                  any wired broker; always grades `unavailable`.
 */
export type MarketFeature = "quote" | "bar" | "flow_attribution" | "trade_print";

/** The buy/sell-attribution features. Kept as data so tests can enumerate. */
export const FLOW_ATTRIBUTION_FEATURES = [
  "cvd",
  "cumulative_delta",
  "delta",
  "footprint",
  "footprint_sides",
  "aggressive_buy_volume",
  "aggressive_sell_volume",
  "aggression",
  "absorption",
  "trade_imbalance",
] as const;

/**
 * Standing capability statement. No wired Indian retail broker pushes public
 * aggressor-tagged trade prints, so this is a constant, not a runtime probe.
 */
export const AGGRESSOR_TAPE_AVAILABLE = false as const;
export const NO_AGGRESSOR_TAPE_NOTE =
  "no aggressor tape — buy/sell sides are inferred from quotes";

// ─── Source sets (source-level grading; unchanged from the original) ────────

/**
 * Directly observed QUOTE/BAR streams. Note what this does and does not say:
 * the ltp/bid/ask stream is genuinely observed; the buy/sell split someone
 * later computes from it is NOT (see `MarketFeature`).
 */
const OBSERVED_SOURCES = new Set(["market_ticks", "tick_reconstruction", "ticks", "live_tick"]);

const RECONSTRUCTED_SOURCES = new Set(["tick_reconstruction_book", "depth_reconstruction"]);

/** Fabricated from OHLCV bars — never a tape. */
const BAR_INFERRED_SOURCES = new Set([
  "bar_inference",
  "bar_proxy",
  "bar_fallback",
  "bar_proxy_timeout",
  "insufficient_ticks",
  "spot_index_proxy",
  "historical_bar_inference",
  "snapshot",
  "scan_guarded",
]);

/** Computed by a model (Black-Scholes greeks, policy heads, fitted regimes). */
const MODELLED_SOURCES = new Set([
  "black_scholes",
  "bs_model",
  "greeks_model",
  "policy",
  "policy_head",
  "modelled",
  "model",
  "synthetic_quote",
]);

const UNAVAILABLE_SOURCES = new Set(["", "unknown", "unavailable", "none", "null"]);

export function normalizeSource(source?: string | null): string {
  return String(source ?? "").trim().toLowerCase();
}

/** Source-level grade only: "how was this stream obtained?". */
function gradeOfSourceString(source?: string | null): SourceGrade {
  const s = normalizeSource(source);
  if (UNAVAILABLE_SOURCES.has(s)) return "unavailable";
  if (OBSERVED_SOURCES.has(s)) return "observed";
  if (RECONSTRUCTED_SOURCES.has(s)) return "reconstructed";
  if (BAR_INFERRED_SOURCES.has(s)) return "bar_inferred";
  if (MODELLED_SOURCES.has(s)) return "modelled";
  // TimescaleDB history reads are observed prints replayed from storage.
  if (s.startsWith("timescaledb") || s.startsWith("timescale_")) return "observed";
  if (s.includes("bar_") || s.includes("_proxy") || s.includes("inference")) return "bar_inferred";
  if (s.includes("tick")) return "reconstructed";
  // Unrecognised string: do NOT promote it. Unverified reads as inferred-grade.
  return "bar_inferred";
}

/**
 * Classify a backend source string into the shared grade vocabulary.
 *
 * `feature` is the honesty correction. Without it the answer is about the
 * STREAM (and is byte-identical to the pre-2026-07-19 behaviour, so no gate
 * that calls this bare can change). With `feature: "flow_attribution"` the
 * answer is about the buy/sell-attributed NUMBER, which can never be better
 * than `modelled_from_quotes` because no wired broker sends an aggressor tape.
 */
export function classifySourceGrade(
  source?: string | null,
  feature: MarketFeature = "quote",
): SourceGrade {
  if (feature === "trade_print") {
    // There is no aggressor-tagged print tape on any wired broker. Reserved
    // so the terminal can SAY that rather than silently grading something else.
    return "unavailable";
  }
  const grade = gradeOfSourceString(source);
  if (feature !== "flow_attribution") return grade;
  // Quote/bar-grade evidence, buy/sell-attributed by heuristic ⇒ modelled.
  if (grade === "observed" || grade === "reconstructed") return "modelled_from_quotes";
  return grade;
}

/** Convenience: grade a buy/sell-attribution number (CVD, footprint, delta). */
export function classifyFlowGrade(source?: string | null): SourceGrade {
  return classifySourceGrade(source, "flow_attribution");
}

/** True when a grade means "no buy/sell side here was actually observed". */
export function isInferredSideGrade(grade: SourceGrade): boolean {
  return grade === "modelled_from_quotes" || grade === "bar_inferred" || grade === "modelled";
}

const GRADE_LABEL: Record<SourceGrade, string> = {
  observed: "OBSERVED",
  reconstructed: "RECONSTRUCTED",
  modelled_from_quotes: "INFERRED FROM QUOTES",
  modelled: "MODELLED",
  bar_inferred: "BAR INFERRED",
  unavailable: "SOURCE UNKNOWN",
};

const GRADE_VARIANT: Record<SourceGrade, BadgeVariant> = {
  observed: "success",
  reconstructed: "info",
  // NOT success — an inferred side must never read as a measured one.
  modelled_from_quotes: "info",
  modelled: "info",
  bar_inferred: "warn",
  unavailable: "neutral",
};

export function sourceGradeLabel(grade: SourceGrade): string {
  return GRADE_LABEL[grade];
}

export function sourceGradeVariant(grade: SourceGrade): BadgeVariant {
  return GRADE_VARIANT[grade];
}

/** True when a grade means "this was fabricated from bars, not observed". */
export function isFabricatedGrade(grade: SourceGrade): boolean {
  return grade === "bar_inferred" || grade === "unavailable";
}

// ─── Order-flow badge classification ────────────────────────────────────────

/**
 * `quote_derived` replaces the old `real` kind. Nothing an order-flow surface
 * renders is a trade print, so no kind may be called "real".
 */
export type OfSourceKind = "quote_derived" | "bar_inferred" | "unknown";

export type OfSourceClass = {
  kind: OfSourceKind;
  /** Terse badge text. Never asserts a trade print. */
  label: string;
  /** The grade of the buy/sell attribution this badge sits on. */
  grade: SourceGrade;
  /** Tooltip body — states the derivation in one line. */
  note: string;
};

/**
 * The order-flow streams this badge may call quote-derived (tick-granular).
 * Deliberately narrower than `classifySourceGrade` — a TimescaleDB history
 * read grades `observed` for provenance purposes but is not a live order-flow
 * stream, so it must never claim tick granularity here.
 */
const OF_TICK_STREAM_SOURCES = new Set([
  "market_ticks",
  "tick_reconstruction",
  "tick_reconstruction_book",
]);

const BAR_LABEL_SOURCES = new Set([
  "bar_inference",
  "bar_proxy",
  "bar_fallback",
  "bar_proxy_timeout",
  "spot_index_proxy",
]);

/**
 * Classify an order-flow source string for display.
 *
 * The old labels were "REAL TICKS" / "REAL TICKS · BOOK". They asserted trade
 * prints the feed has never carried. Every label now names the derivation.
 */
export function classifyOfSource(source?: string | null): OfSourceClass {
  const s = normalizeSource(source);
  const grade = classifyFlowGrade(s);

  if (OF_TICK_STREAM_SOURCES.has(s)) {
    return {
      kind: "quote_derived",
      label:
        s === "tick_reconstruction_book"
          ? "BOOK QUOTES · SIDES INFERRED"
          : "TICK QUOTES · SIDES INFERRED",
      grade,
      note:
        s === "tick_reconstruction_book"
          ? `order flow rebuilt from the L2 book snapshot stream (${s}); ${NO_AGGRESSOR_TAPE_NOTE}`
          : `order flow rebuilt from the L1 quote/tick stream (${s}); ${NO_AGGRESSOR_TAPE_NOTE}`,
    };
  }
  if (grade === "unavailable") {
    return {
      kind: "unknown",
      label: "SOURCE UNKNOWN",
      grade,
      note: "the payload did not report an order-flow source — treat with suspicion",
    };
  }
  if (s === "insufficient_ticks") {
    return {
      kind: "bar_inferred",
      label: "INSUFFICIENT TICKS · BAR INFERRED",
      grade,
      note: `too few quotes in the window, so flow fell back to OHLCV bars; ${NO_AGGRESSOR_TAPE_NOTE}`,
    };
  }
  if (BAR_LABEL_SOURCES.has(s)) {
    return {
      kind: "bar_inferred",
      label: "BAR PROXY · SIDES INFERRED",
      grade,
      note: `order flow fabricated from OHLCV bar shape (${s}), not from a quote stream; ${NO_AGGRESSOR_TAPE_NOTE}`,
    };
  }
  // Recognised by the contract but not a known OF stream name — show it
  // verbatim and flag it rather than silently promoting it.
  return {
    kind: "bar_inferred",
    label: `${s.replace(/_/g, " ").toUpperCase()} · UNVERIFIED`,
    grade,
    note: `unrecognised order-flow source "${s}" — graded down, not promoted; ${NO_AGGRESSOR_TAPE_NOTE}`,
  };
}

/**
 * One-line caption for any buy/sell-attributed series, e.g.
 * `"CVD · inferred from quotes · no aggressor tape"`.
 */
export function describeFlowDerivation(featureLabel: string, source?: string | null): string {
  const { kind } = classifyOfSource(source);
  const basis =
    kind === "quote_derived" ? "inferred from quotes" : kind === "bar_inferred" ? "inferred from bars" : "source unknown";
  return `${featureLabel} · ${basis} · no aggressor tape`;
}

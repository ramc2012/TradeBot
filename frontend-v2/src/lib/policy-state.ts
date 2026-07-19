/**
 * policy-state — the ONE mapping from each lane's NATIVE state vocabulary into
 * the canonical policy state the Strategies matrix compares across policies.
 *
 * ─── The rule this module exists to enforce ─────────────────────────────────
 *
 * The four policies do NOT speak the same language, and pretending they do is
 * how a comparison screen starts lying:
 *
 *   · Convergence emits a genuine setup lifecycle (`setup_state`) and a boolean
 *     gate ladder. It is the only lane that maps DIRECTLY.
 *   · Auction emits a regime + a risk decision + agent decisions. It has NO
 *     "armed" stage at all — no field means "structure in place, waiting".
 *   · MP+OF emits PROSE (`candidate_reason`, `signal_validation_detail`) and a
 *     signal/candidate pair. No booleans, no armed stage, no exit state.
 *   · Directional emits regime + signal + selected contract, and ONLY inside a
 *     heavy per-underlying snapshot. Its `/summary` carries no state at all.
 *
 * So every mapping below is either a real field or `UNAVAILABLE` WITH THE
 * REASON. Synthesising an ARMED tier for Auction out of a confidence threshold,
 * or an EXITING tier for MP+OF out of nothing, would put a state on screen that
 * no lane ever computed. That is forbidden here, not merely discouraged.
 *
 * ─── Disagreement is the product ────────────────────────────────────────────
 *
 * There is deliberately NO consensus score and NO averaging in this module.
 * `findDisagreements` returns the conflicting PAIRS; where only one policy
 * covers an instrument it says so, rather than letting a lone opinion read as
 * agreement.
 *
 * Pure module — no React, no network, no `@/` imports beyond the pure semantic
 * contract — so it runs under bare `node --test`.
 */
import type { BadgeVariant } from "./flow-provenance";
import type { TradingHorizon } from "./lane-taxonomy";

// ─── Canonical vocabulary ───────────────────────────────────────────────────

export type PolicyState =
  | "WATCHING"
  | "ARMED"
  | "ACTIONABLE"
  | "BLOCKED"
  | "EXITING"
  | "UNAVAILABLE";

export const POLICY_STATE_LABEL: Record<PolicyState, string> = {
  WATCHING: "watching",
  ARMED: "armed",
  ACTIONABLE: "actionable",
  BLOCKED: "blocked",
  EXITING: "exiting",
  UNAVAILABLE: "unavailable",
};

/**
 * Colour semantics, delegated to the SAME contract the rest of the terminal
 * uses (lib/status-variants): green is ONLY actionable-confirmed, armed is
 * BLUE because it is a promise about the next bar rather than a state to act
 * on, and unavailable is neutral — never a warning, because nothing is wrong.
 */
export const POLICY_STATE_VARIANT: Record<PolicyState, BadgeVariant> = {
  ACTIONABLE: "success",
  ARMED: "info",
  WATCHING: "neutral",
  BLOCKED: "error",
  EXITING: "warn",
  UNAVAILABLE: "neutral",
};

export function policyStateLabel(s: PolicyState): string {
  return POLICY_STATE_LABEL[s];
}

export function policyStateVariant(s: PolicyState): BadgeVariant {
  return POLICY_STATE_VARIANT[s];
}

// ─── Policies ───────────────────────────────────────────────────────────────

/**
 * Five policy ids over FOUR columns: MP+OF is two policy ids sharing one
 * library (index long-premium and commodity futures), and they answer for
 * different instruments, so they are chips inside one column rather than an
 * averaged single cell.
 */
export type PolicyId =
  | "auction"
  | "mpof_index"
  | "mpof_commodity"
  | "convergence"
  | "directional";

export type PolicyColumnId = "auction" | "mpof" | "convergence" | "directional";

export const POLICY_COLUMNS: PolicyColumnId[] = [
  "auction",
  "mpof",
  "convergence",
  "directional",
];

export const POLICY_COLUMN_LABEL: Record<PolicyColumnId, string> = {
  auction: "Auction",
  mpof: "MP + OF",
  convergence: "Convergence",
  directional: "Directional",
};

export const POLICY_COLUMN_MEMBERS: Record<PolicyColumnId, PolicyId[]> = {
  auction: ["auction"],
  mpof: ["mpof_index", "mpof_commodity"],
  convergence: ["convergence"],
  directional: ["directional"],
};

export const POLICY_LABEL: Record<PolicyId, string> = {
  auction: "Auction",
  mpof_index: "MP+OF · index",
  mpof_commodity: "MP+OF · commodity",
  convergence: "Convergence",
  directional: "Directional",
};

/** Which horizons a policy actually operates at, and the evidence for it. */
export const POLICY_HORIZONS: Record<PolicyId, { horizons: TradingHorizon[]; evidence: string }> = {
  auction: {
    horizons: ["intraday"],
    evidence:
      "the auction bundle is rebuilt per session from that session's profile and order flow; it carries no multi-session view",
  },
  mpof_index: {
    horizons: ["intraday"],
    evidence: "3-minute signal bars over the current session's developing profile",
  },
  mpof_commodity: {
    horizons: ["intraday"],
    evidence: "3-minute MCX signal bars, session-scoped, with a weekly/monthly HTF gate as a filter only",
  },
  convergence: {
    horizons: ["intraday"],
    evidence:
      "3-minute bars with an initial-balance window and a noon quarantine — both session-scoped constructs",
  },
  directional: {
    horizons: ["intraday", "positional"],
    evidence:
      "DirectionalSignal.positional selects the MONTHLY DTE window; without it the regime's weekly preference applies",
  },
};

/** True when this policy has any read at all at this horizon. */
export function policyOperatesAt(policy: PolicyId, horizon: TradingHorizon): boolean {
  return POLICY_HORIZONS[policy].horizons.includes(horizon);
}

// ─── The cell ───────────────────────────────────────────────────────────────

export type PolicyCellData = {
  policyId: PolicyId;
  state: PolicyState;
  /** LONG / SHORT / FLAT / null — never inferred from a score. */
  direction: string | null;
  /** 0..1 as the lane emits it, or null. Never defaulted to 0. */
  confidence: number | null;
  /** The lane's own native state word, kept verbatim for audit. */
  nativeState: string | null;
  /** How long this read is good for, when the lane says. Usually null. */
  validity: string | null;
  blockers: string[];
  /** Prose the lane emitted that a boolean cannot carry. */
  note: string | null;
  /** Non-null ⇒ nothing was measured; this is the reason, not an error. */
  unavailableReason: string | null;
};

function cell(policyId: PolicyId, partial: Partial<PolicyCellData>): PolicyCellData {
  return {
    policyId,
    state: "UNAVAILABLE",
    direction: null,
    confidence: null,
    nativeState: null,
    validity: null,
    blockers: [],
    note: null,
    unavailableReason: null,
    ...partial,
  };
}

const norm = (v: unknown): string => String(v ?? "").trim().toUpperCase();

// ─── Convergence ────────────────────────────────────────────────────────────

export type ConvergenceInput = {
  available: boolean;
  reason: string | null;
  kind?: string | null;
  setupState: string | null;
  action: string | null;
  quality?: string | null;
  direction: string | null;
  score: number | null;
  confirmations: number | null;
  required: number | null;
  blocked: string[];
  gates?: Record<string, boolean> | null;
  readinessGates?: Record<string, boolean> | null;
  tickAgeMs?: number | null;
  tickLimitMs?: number | null;
  rr?: number | null;
  entry?: number | null;
  stop?: number | null;
  target1?: number | null;
  /** An open paper leg for this symbol on this lane — the only EXITING source. */
  hasOpenPosition?: boolean;
};

/**
 * Convergence is the ONLY direct map: `setup_state` is a real lifecycle.
 * (institutional_convergence/engine.py — WATCHING / ARMED / CONFIRMED /
 * CONFIRMED_BLOCKED / MISSED_NO_CHASE / CONFLICT / EXPIRED.)
 */
export function convergenceCell(input: ConvergenceInput): PolicyCellData {
  if (!input.available) {
    return cell("convergence", {
      state: "UNAVAILABLE",
      unavailableReason: input.reason ?? "not in the convergence scan universe",
    });
  }

  const native = norm(input.setupState);
  const action = norm(input.action);
  const base = {
    nativeState: input.setupState ?? null,
    direction: input.action || input.direction || null,
    confidence: input.score == null ? null : Math.max(0, Math.min(1, input.score / 100)),
    blockers: input.blocked ?? [],
  };

  // A stock row is evaluated by a DIFFERENT function whose gate dict hardcodes
  // three falses (service.py evaluate_stock_context: intraday_profile_ready,
  // real_book_data, footprint_trigger). It is not "waiting" — it can never
  // confirm on today's data, and the cell says so rather than showing a
  // permanently red gate ladder as if it were a transient block.
  if (String(input.kind ?? "").toLowerCase() === "stock") {
    return cell("convergence", {
      ...base,
      state: "BLOCKED",
      note:
        "stock rows run evaluate_stock_context, whose intraday_profile_ready / real_book_data / footprint_trigger gates are hardcoded false — this lane cannot confirm a stock on today's data",
    });
  }

  if (input.hasOpenPosition) {
    return cell("convergence", { ...base, state: "EXITING" });
  }

  if (!native) {
    return cell("convergence", {
      ...base,
      state: "UNAVAILABLE",
      unavailableReason: "in the scan universe but no evaluation landed this cycle",
    });
  }

  if (native === "CONFIRMED" && (action === "LONG" || action === "SHORT")) {
    return cell("convergence", { ...base, state: "ACTIONABLE" });
  }
  if (native === "CONFIRMED_BLOCKED" || native === "MISSED_NO_CHASE" || native === "CONFLICT") {
    return cell("convergence", {
      ...base,
      state: "BLOCKED",
      note:
        native === "CONFLICT"
          ? "both directions scored identically — the lane refuses to pick a side"
          : native === "MISSED_NO_CHASE"
            ? "the move already ran past the entry band; chasing is disallowed"
            : null,
    });
  }
  if (native === "ARMED") return cell("convergence", { ...base, state: "ARMED" });
  if (native === "EXPIRED") {
    return cell("convergence", {
      ...base,
      state: "WATCHING",
      note: "the previous setup window lapsed without confirmation",
    });
  }
  return cell("convergence", { ...base, state: "WATCHING" });
}

// ─── Auction ────────────────────────────────────────────────────────────────

export type AuctionInput = {
  /** False until the heavy per-symbol snapshot is loaded — never guessed. */
  loaded: boolean;
  reason: string | null;
  regime: string | null;
  confidence?: number | null;
  allowedDirections?: string[] | null;
  allowed: boolean | null;
  killSwitch?: boolean | null;
  reasons: string[];
  /** agent_decisions[] actions, in order. FLAT included. */
  agentActions?: string[] | null;
  agentConfidence?: number | null;
  executionPlanCount?: number | null;
  openLots?: number;
};

/**
 * Auction has NO armed tier. There is no field in AnalysisBundle meaning
 * "structure in place, waiting for confirmation" — regime.confidence is a
 * classification score, not a stage, and thresholding it would invent one.
 */
export const AUCTION_NO_ARMED_STAGE =
  "the auction bundle emits no armed/pending stage — regime, risk and agent decisions only";

export function auctionCell(input: AuctionInput): PolicyCellData {
  if (!input.loaded) {
    return cell("auction", {
      state: "UNAVAILABLE",
      unavailableReason:
        input.reason ??
        "auction state exists only inside the 59 KB per-symbol snapshot — load it for this instrument",
    });
  }

  const actions = (input.agentActions ?? []).map(norm).filter(Boolean);
  const live = actions.filter((a) => a !== "FLAT");
  const base = {
    nativeState: input.regime ?? null,
    direction: live[0] ?? (actions.length ? "FLAT" : null),
    confidence: input.agentConfidence ?? input.confidence ?? null,
    blockers: input.reasons ?? [],
  };

  if (input.killSwitch) {
    return cell("auction", { ...base, state: "BLOCKED", note: "risk kill switch is engaged" });
  }
  if (input.allowed === false) {
    return cell("auction", { ...base, state: "BLOCKED" });
  }
  if (input.allowed == null) {
    return cell("auction", {
      ...base,
      state: "UNAVAILABLE",
      unavailableReason: "the snapshot carried no risk decision",
    });
  }
  if (live.length && (input.executionPlanCount ?? 0) > 0) {
    return cell("auction", { ...base, state: "ACTIONABLE" });
  }
  if (live.length) {
    return cell("auction", {
      ...base,
      state: "BLOCKED",
      note: "an agent chose a side but the bundle produced no execution instruction",
    });
  }
  return cell("auction", { ...base, state: "WATCHING", note: AUCTION_NO_ARMED_STAGE });
}

// ─── MP + OF ────────────────────────────────────────────────────────────────

export type MpofInput = {
  available: boolean;
  reason: string | null;
  /** "warming_up" | "ready" | … */
  mpStatus?: string | null;
  /** The monitor's own `reason` field — insufficient_data etc. */
  dataReason?: string | null;
  signal: string | null;
  candidate: string | null;
  candidateReason: string | null;
  validationDetail: string | null;
  confidence: number | null;
  mpDirection?: string | null;
};

const MPOF_DATA_REASONS = new Set([
  "insufficient_data",
  "insufficient_1m_spot",
  "no_session_rows",
]);

export const MPOF_NO_ARMED_STAGE =
  "the MP+OF monitor emits a signal / candidate pair and prose — it has no armed stage between them";
export const MPOF_NO_EXIT_STATE =
  "exits live on the strategy agent's position rows, not on this monitor payload";

export function mpofCell(policyId: "mpof_index" | "mpof_commodity", input: MpofInput): PolicyCellData {
  if (!input.available) {
    return cell(policyId, {
      state: "UNAVAILABLE",
      unavailableReason: input.reason ?? "no MP+OF monitor row for this instrument",
    });
  }

  const status = String(input.mpStatus ?? "").toLowerCase();
  const dataReason = String(input.dataReason ?? "").toLowerCase();
  if (status === "warming_up" || MPOF_DATA_REASONS.has(dataReason)) {
    return cell(policyId, {
      state: "UNAVAILABLE",
      nativeState: input.mpStatus ?? input.dataReason ?? null,
      unavailableReason:
        status === "warming_up"
          ? "the profile is still warming up for this session"
          : `the monitor reported ${dataReason.replace(/_/g, " ")}`,
    });
  }

  const signal = norm(input.signal);
  const candidate = norm(input.candidate);
  const base = {
    nativeState: input.signal ?? input.candidate ?? input.mpStatus ?? null,
    direction: input.signal || input.candidate || input.mpDirection || null,
    confidence: input.confidence,
  };

  if (signal === "BUY" || signal === "SELL") {
    return cell(policyId, { ...base, state: "ACTIONABLE", note: input.validationDetail });
  }
  if (candidate === "BUY" || candidate === "SELL") {
    return cell(policyId, {
      ...base,
      state: "BLOCKED",
      blockers: [input.candidateReason].filter(Boolean) as string[],
      note: input.validationDetail,
    });
  }
  return cell(policyId, { ...base, state: "WATCHING", note: MPOF_NO_ARMED_STAGE });
}

// ─── Directional ────────────────────────────────────────────────────────────

export type DirectionalInput = {
  /** False until the per-underlying snapshot is loaded. */
  loaded: boolean;
  reason: string | null;
  regimeLabel?: string | null;
  tradeAllowed?: boolean | null;
  regimeReasons?: string[] | null;
  signalDirection?: string | null;
  signalConfidence?: number | null;
  thesis?: string | null;
  positional?: boolean | null;
  hasSelectedContract?: boolean | null;
  riskReasons?: string[] | null;
  ruleBlockers?: string[] | null;
  executionReady?: boolean | null;
  degradedReason?: string | null;
  selectionReason?: string | null;
};

export function directionalCell(input: DirectionalInput): PolicyCellData {
  if (!input.loaded) {
    return cell("directional", {
      state: "UNAVAILABLE",
      unavailableReason:
        input.reason ??
        "/api/directional-options/summary carries the universe only — per-underlying state lives in the live snapshot, loaded on request",
    });
  }

  const base = {
    nativeState: input.regimeLabel ?? null,
    direction: input.signalDirection ?? null,
    confidence: input.signalConfidence ?? null,
    note: input.thesis ?? input.selectionReason ?? null,
  };

  const blockers = [
    ...(input.ruleBlockers ?? []),
    ...(input.tradeAllowed === false ? input.regimeReasons ?? [] : []),
    ...(input.riskReasons ?? []),
  ].filter(Boolean);

  if (input.executionReady === false) {
    return cell("directional", {
      ...base,
      state: "BLOCKED",
      blockers: [
        ...(input.degradedReason ? [String(input.degradedReason).replace(/_/g, " ")] : []),
        ...blockers,
      ],
    });
  }
  if (input.tradeAllowed === false || blockers.length) {
    return cell("directional", { ...base, state: "BLOCKED", blockers });
  }
  if (!input.signalDirection) {
    return cell("directional", { ...base, state: "WATCHING" });
  }
  if (input.hasSelectedContract) {
    return cell("directional", { ...base, state: "ACTIONABLE" });
  }
  // A signal with no contract IS this lane's armed stage: the view exists, the
  // instrument to express it in does not yet.
  return cell("directional", {
    ...base,
    state: "ARMED",
    note: input.selectionReason ?? base.note,
  });
}

// ─── Disagreement ───────────────────────────────────────────────────────────

export type Disagreement = {
  a: PolicyId;
  b: PolicyId;
  kind: "opposite_direction" | "actionable_vs_blocked";
  detail: string;
};

const SIDE = (d: string | null): "LONG" | "SHORT" | null => {
  const v = norm(d);
  if (v === "LONG" || v === "BUY" || v === "CE" || v === "BULLISH") return "LONG";
  if (v === "SHORT" || v === "SELL" || v === "PE" || v === "BEARISH") return "SHORT";
  return null;
};

/**
 * Every pair of policies that DISAGREE about the pinned instrument.
 *
 * No consensus score is computed anywhere, on purpose: averaging four
 * incommensurable lanes into one number is precisely the thing that hides the
 * information a trader needs. A policy that is UNAVAILABLE has no opinion and
 * therefore never participates in a disagreement.
 */
export function findDisagreements(cells: PolicyCellData[]): Disagreement[] {
  const out: Disagreement[] = [];
  const opinionated = cells.filter((c) => c.state !== "UNAVAILABLE");
  for (let i = 0; i < opinionated.length; i++) {
    for (let j = i + 1; j < opinionated.length; j++) {
      const a = opinionated[i];
      const b = opinionated[j];
      const sa = SIDE(a.direction);
      const sb = SIDE(b.direction);
      if (sa && sb && sa !== sb) {
        out.push({
          a: a.policyId,
          b: b.policyId,
          kind: "opposite_direction",
          detail: `${POLICY_LABEL[a.policyId]} reads ${sa}, ${POLICY_LABEL[b.policyId]} reads ${sb}`,
        });
        continue;
      }
      const actionable = a.state === "ACTIONABLE" ? a : b.state === "ACTIONABLE" ? b : null;
      const blocked = a.state === "BLOCKED" ? a : b.state === "BLOCKED" ? b : null;
      if (actionable && blocked && actionable !== blocked) {
        out.push({
          a: actionable.policyId,
          b: blocked.policyId,
          kind: "actionable_vs_blocked",
          detail: `${POLICY_LABEL[actionable.policyId]} is actionable while ${POLICY_LABEL[blocked.policyId]} is blocked${
            blocked.blockers.length ? ` on ${blocked.blockers.slice(0, 2).join(", ")}` : ""
          }`,
        });
      }
    }
  }
  return out;
}

export const NO_SECOND_OPINION =
  "no second opinion available for this instrument — one policy covering it is not agreement";

/** How many policies actually hold an opinion. Drives the no-second-opinion note. */
export function opinionCount(cells: PolicyCellData[]): number {
  return cells.filter((c) => c.state !== "UNAVAILABLE").length;
}

// ─── Decision waterfall ─────────────────────────────────────────────────────

export type StageKey =
  | "data"
  | "structure"
  | "flow"
  | "anti_chase"
  | "risk"
  | "execution";

export const STAGE_KEYS: StageKey[] = [
  "data",
  "structure",
  "flow",
  "anti_chase",
  "risk",
  "execution",
];

export const STAGE_LABEL: Record<StageKey, string> = {
  data: "Data sufficient",
  structure: "Structure",
  flow: "Flow confirmation",
  anti_chase: "Anti-chase",
  risk: "Risk",
  execution: "Execution",
};

export type StageVerdict = "passed" | "failed" | "unavailable";

export type WaterfallStage = {
  key: StageKey;
  verdict: StageVerdict;
  /** The payload field this verdict was read from — shown on click. */
  observationField: string | null;
  /** The value that field held, rendered as text. */
  observationValue: string | null;
  /** For `unavailable`: why this policy emits nothing at this stage. */
  reason: string | null;
};

function stage(
  key: StageKey,
  verdict: StageVerdict,
  observationField: string | null,
  observationValue: string | null,
  reason: string | null = null,
): WaterfallStage {
  return { key, verdict, observationField, observationValue, reason };
}

const boolVerdict = (v: boolean | null | undefined): StageVerdict =>
  v == null ? "unavailable" : v ? "passed" : "failed";

const showList = (v: string[] | null | undefined): string =>
  v && v.length ? v.join(", ") : "none";

/**
 * Convergence — the ONLY policy with a real boolean at every one of the six
 * stages. That is why the waterfall is worth building: the comparison shows at
 * a glance which lanes actually audit themselves and which cannot.
 */
export function convergenceWaterfall(input: ConvergenceInput): WaterfallStage[] {
  const gates = input.gates ?? {};
  const readiness = input.readinessGates ?? {};
  const has = (k: string) => Object.prototype.hasOwnProperty.call(gates, k);
  const readinessKeys = Object.keys(readiness);
  const readinessOk = readinessKeys.length ? readinessKeys.every((k) => readiness[k]) : null;

  return [
    stage(
      "data",
      readinessOk == null ? "unavailable" : readinessOk ? "passed" : "failed",
      "readiness_gates",
      readinessKeys.length
        ? readinessKeys.map((k) => `${k}=${readiness[k] ? "pass" : "fail"}`).join(", ")
        : null,
      readinessKeys.length ? null : "no readiness_gates block on this row (summary tier)",
    ),
    stage(
      "structure",
      has("structural_setup_armed") ? boolVerdict(gates.structural_setup_armed) : "unavailable",
      "gates.structural_setup_armed",
      has("structural_setup_armed") ? String(gates.structural_setup_armed) : null,
      has("structural_setup_armed") ? null : "gate ladder not loaded — open the detail tier",
    ),
    stage(
      "flow",
      has("confirmation_2_of_3") ? boolVerdict(gates.confirmation_2_of_3) : "unavailable",
      "gates.confirmation_2_of_3",
      input.confirmations == null
        ? has("confirmation_2_of_3")
          ? String(gates.confirmation_2_of_3)
          : null
        : `${input.confirmations}/${input.required ?? "?"} confirmations`,
      has("confirmation_2_of_3") ? null : "gate ladder not loaded — open the detail tier",
    ),
    stage(
      "anti_chase",
      has("not_chasing") ? boolVerdict(gates.not_chasing) : "unavailable",
      "gates.not_chasing",
      has("not_chasing") ? String(gates.not_chasing) : null,
      has("not_chasing")
        ? null
        : "gate ladder not loaded — open the detail tier (engine _risk_plan: |entry − level| ≤ max_chase_atr × ATR)",
    ),
    stage(
      "risk",
      has("reward_risk_1_5") ? boolVerdict(gates.reward_risk_1_5) : "unavailable",
      "gates.reward_risk_1_5 · risk.reward_risk",
      input.rr == null ? (has("reward_risk_1_5") ? String(gates.reward_risk_1_5) : null) : `${input.rr.toFixed(2)}R`,
      has("reward_risk_1_5") ? null : "gate ladder not loaded — open the detail tier",
    ),
    stage(
      "execution",
      norm(input.action) === "LONG" || norm(input.action) === "SHORT"
        ? "passed"
        : input.setupState
          ? "failed"
          : "unavailable",
      "status · action",
      input.setupState ? `${input.setupState} · ${input.action ?? "FLAT"}` : null,
      input.setupState ? null : "no evaluation landed this cycle",
    ),
  ];
}

export function auctionWaterfall(input: AuctionInput & { staleSeconds?: number | null; snapshotMode?: string | null }): WaterfallStage[] {
  if (!input.loaded) {
    return STAGE_KEYS.map((k) =>
      stage(k, "unavailable", null, null, "the per-symbol auction snapshot has not been loaded"),
    );
  }
  return [
    stage(
      "data",
      input.staleSeconds == null ? "unavailable" : input.staleSeconds <= 300 ? "passed" : "failed",
      "data_status.stale_data_seconds · snapshot_mode",
      input.staleSeconds == null
        ? null
        : `${Math.round(input.staleSeconds)}s${input.snapshotMode ? ` · ${input.snapshotMode}` : ""}`,
      input.staleSeconds == null ? "the snapshot carried no data_status age" : null,
    ),
    stage(
      "structure",
      input.regime ? (input.allowedDirections?.length ? "passed" : "failed") : "unavailable",
      "regime.label · regime.allowed_directions",
      input.regime
        ? `${input.regime}${input.allowedDirections?.length ? ` · ${input.allowedDirections.join("/")}` : " · no direction allowed"}`
        : null,
      input.regime ? null : "no regime assessment in the bundle",
    ),
    stage(
      "flow",
      "unavailable",
      null,
      null,
      "this policy emits no discrete flow-confirmation gate — order-flow metrics feed regime.scorecard opaquely and are never surfaced as a pass/fail",
    ),
    stage(
      "anti_chase",
      "unavailable",
      null,
      null,
      "this policy has no chase test — no distance-from-level bound exists in the auction bundle",
    ),
    stage(
      "risk",
      input.allowed == null ? "unavailable" : input.allowed && !input.killSwitch ? "passed" : "failed",
      "risk.allowed · risk.kill_switch · risk.reasons",
      input.allowed == null
        ? null
        : `allowed=${input.allowed}${input.killSwitch ? " · kill switch ON" : ""}${
            input.reasons?.length ? ` · ${input.reasons.join(", ")}` : ""
          }`,
      input.allowed == null ? "no risk decision in the bundle" : null,
    ),
    stage(
      "execution",
      input.executionPlanCount == null
        ? "unavailable"
        : input.executionPlanCount > 0
          ? "passed"
          : "failed",
      "execution_plan[]",
      input.executionPlanCount == null ? null : `${input.executionPlanCount} instruction(s)`,
      input.executionPlanCount == null ? "no execution_plan in the bundle" : null,
    ),
  ];
}

export function mpofWaterfall(
  input: MpofInput & { ofSource?: string | null; ofCoveredBars?: number | null; htfBias?: string | null; isCommodity?: boolean },
): WaterfallStage[] {
  const status = String(input.mpStatus ?? "").toLowerCase();
  const dataReason = String(input.dataReason ?? "").toLowerCase();
  const dataVerdict: StageVerdict = !input.available
    ? "unavailable"
    : status === "ready" && !MPOF_DATA_REASONS.has(dataReason)
      ? "passed"
      : status || dataReason
        ? "failed"
        : "unavailable";

  const signal = norm(input.signal);
  const candidate = norm(input.candidate);

  return [
    stage(
      "data",
      dataVerdict,
      "mp_status · reason · of_source · of_tick_covered_bars",
      input.available
        ? [
            input.mpStatus ? `mp_status=${input.mpStatus}` : null,
            dataReason ? `reason=${dataReason}` : null,
            input.ofSource ? `of_source=${input.ofSource}` : null,
            input.ofCoveredBars == null ? null : `tick-covered bars=${input.ofCoveredBars}`,
          ]
            .filter(Boolean)
            .join(" · ")
        : null,
      input.available ? null : input.reason ?? "no monitor row for this instrument",
    ),
    stage(
      "structure",
      input.mpDirection ? "passed" : dataVerdict === "passed" ? "failed" : "unavailable",
      "mp_direction · value_migration_state",
      input.mpDirection ?? null,
      input.mpDirection ? null : dataVerdict === "passed" ? null : "profile not ready",
    ),
    // The monitor's flow stage is PROSE, not a boolean. It is only a verdict
    // when the signal/candidate pair disambiguates it; otherwise the text is
    // shown as an observation with no pass/fail claim attached.
    stage(
      "flow",
      signal === "BUY" || signal === "SELL"
        ? "passed"
        : candidate === "BUY" || candidate === "SELL"
          ? "failed"
          : "unavailable",
      "signal · candidate_signal · signal_validation_detail",
      [input.candidateReason, input.validationDetail].filter(Boolean).join(" — ") || null,
      signal || candidate
        ? null
        : "this policy emits prose here, not a gate — with no signal or candidate there is nothing to read a verdict from",
    ),
    stage(
      "anti_chase",
      "unavailable",
      null,
      null,
      "this policy has no chase test in the monitor payload",
    ),
    stage(
      "risk",
      input.isCommodity ? (input.htfBias ? "passed" : "unavailable") : "unavailable",
      input.isCommodity ? "htf_bias (COMMODITY_HTF_GATE_ENABLED)" : null,
      input.isCommodity ? input.htfBias ?? null : null,
      input.isCommodity
        ? input.htfBias
          ? null
          : "the higher-timeframe gate emitted no bias this cycle"
        : "the higher-timeframe gate is commodity-only; the index monitor carries no risk block",
    ),
    stage(
      "execution",
      signal === "BUY" || signal === "SELL" ? "passed" : dataVerdict === "passed" ? "failed" : "unavailable",
      "signal · entry_style",
      input.signal ?? null,
      dataVerdict === "passed" ? null : "profile not ready",
    ),
  ];
}

export function directionalWaterfall(input: DirectionalInput): WaterfallStage[] {
  if (!input.loaded) {
    return STAGE_KEYS.map((k) =>
      stage(k, "unavailable", null, null, "the per-underlying directional snapshot has not been loaded"),
    );
  }
  return [
    stage(
      "data",
      boolVerdict(input.executionReady),
      "data_status.execution_ready · degraded_reason",
      input.executionReady == null
        ? null
        : `${input.executionReady}${input.degradedReason ? ` · ${input.degradedReason}` : ""}`,
      input.executionReady == null ? "no data_status on the snapshot" : null,
    ),
    stage(
      "structure",
      input.regimeLabel ? boolVerdict(input.tradeAllowed) : "unavailable",
      "regime.label · regime.trade_allowed · regime.reasons",
      input.regimeLabel
        ? `${input.regimeLabel} · trade_allowed=${input.tradeAllowed}${
            input.regimeReasons?.length ? ` · ${input.regimeReasons.join(", ")}` : ""
          }`
        : null,
      input.regimeLabel ? null : "no regime on the snapshot",
    ),
    stage(
      "flow",
      "unavailable",
      null,
      null,
      "this policy consumes no order flow — its features are price, vol and option positioning only",
    ),
    stage(
      "anti_chase",
      "unavailable",
      null,
      null,
      "this policy has no chase test — no distance-from-trigger bound exists in its rule set",
    ),
    stage(
      "risk",
      (input.ruleBlockers?.length ?? 0) + (input.riskReasons?.length ?? 0) > 0
        ? "failed"
        : input.signalDirection
          ? "passed"
          : "unavailable",
      "rule_blockers[] · risk.reasons[]",
      showList([...(input.ruleBlockers ?? []), ...(input.riskReasons ?? [])]),
      input.signalDirection ? null : "no signal, so no risk plan was built",
    ),
    stage(
      "execution",
      input.hasSelectedContract == null
        ? "unavailable"
        : input.hasSelectedContract
          ? "passed"
          : "failed",
      "selected_contract · selection_reason",
      input.selectionReason ?? (input.hasSelectedContract ? "contract selected" : null),
      input.hasSelectedContract == null ? "the snapshot carried no contract selection" : null,
    ),
  ];
}

/** Counts for the "how much of this policy is auditable" caption. */
export function waterfallCoverage(stages: WaterfallStage[]): {
  passed: number;
  failed: number;
  unavailable: number;
} {
  return {
    passed: stages.filter((s) => s.verdict === "passed").length,
    failed: stages.filter((s) => s.verdict === "failed").length,
    unavailable: stages.filter((s) => s.verdict === "unavailable").length,
  };
}

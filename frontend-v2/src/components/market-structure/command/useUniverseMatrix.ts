"use client";

/**
 * useUniverseMatrix — the ONE summary query set behind the Command matrix.
 *
 * DATA SOURCING RULE (Phase 1c): the matrix is composed from endpoints that
 * ALREADY exist, and every cell is either a real observation or an explicit
 * `unavailable` with a reason. Nothing is inferred to fill a column, and no
 * column is dropped just because coverage is partial — a column with no source
 * for a row renders "—" with the reason in its title, which is the honest
 * statement, not a blank.
 *
 * Endpoints used (all read-only, all pre-existing, ~240 KB for 216 NSE rows):
 *   /api/market/atm-watchlist                      → NSE universe (216), spot,
 *                                                     lot, expiry, per-symbol
 *                                                     as_of, CE/PE OI + IV
 *   /api/institutional-convergence/status           → NSE convergence rows (12)
 *   /api/institutional-convergence/commodity/status → MCX universe + rows (8)
 *   /api/commodity/index-monitor                    → MP + MP/OF (NIFTY, BANKNIFTY)
 *   /api/trading/positions                          → portfolio intent (26 legs)
 *   /api/auction-intelligence/summary               → auction lane book + loop
 *
 * DELIBERATELY NOT USED in the refresh loop, because they are enormous and
 * would re-create the poll-starvation class the backend was just cured of:
 *   /api/commodity/overview               (306 KB)
 *   /api/directional-options/paper-positions (1.4 MB)
 *   /api/auction-intelligence/live-snapshot  (59 KB *per symbol*)
 *   /api/institutional-convergence/status/{symbol} (195 KB)
 * Those are drawer-only, detail-on-demand.
 *
 * The composed row shape is deliberately the shape a future additive backend
 * aggregate (`GET /api/system/universe-matrix`) would return, so swapping the
 * client-side composition for one server call later is a change to THIS file
 * only. It is not implemented today: the live stack cannot be restarted, so a
 * new endpoint would 404 against the running backend and the UI would fall back
 * to exactly this composition anyway.
 */
import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";

import { REFRESH_MS } from "@/components/desk-ui";
import {
  getATMWatchlist,
  getAuctionIntelligenceSummary,
  getCommodityIndexMonitor,
  getCommodityInstitutionalConvergenceStatus,
  getInstitutionalConvergenceStatus,
  getPositions,
} from "@/lib/api";
import {
  type Freshness,
  type MarketFeature,
  type Provenance,
  type SourceGrade,
  type Sufficiency,
  provenanceOf,
  rrRender,
} from "@/lib/market-semantics";

import type { MarketKey } from "../context/schema";

// ─── Row shape ──────────────────────────────────────────────────────────────

/** A cell that may legitimately have NO source. `reason` says which it is. */
export type Availability = { available: boolean; reason: string | null };

export const AVAILABLE: Availability = { available: true, reason: null };
export const unavailable = (reason: string): Availability => ({ available: false, reason });

export type MatrixRowBase = {
  symbol: string;
  kind: string;
  market: MarketKey;
  contract: string | null;
  spot: number | null;
  changePct: number | null;
  lotSize: number | null;
  expiry: string | null;
  /** Per-symbol observation timestamp — the basis of the readiness column. */
  asOf: string | null;
  source: string | null;
  /**
   * What KIND of number `source` describes. MCX rows fall back to the
   * convergence CVD/footprint source, which is a buy/sell-ATTRIBUTED feature —
   * grading that `observed` would claim a trade tape the feed does not carry
   * (`backend/analytics/orderflow.py`, 2026-07-19). Quote/watchlist sources
   * stay `"quote"`, i.e. unchanged.
   */
  sourceFeature: MarketFeature;
  /** Convergence tick telemetry, feeds readiness sufficiency when present. */
  tickAgeMs: number | null;
  tickLimitMs: number | null;
  degradedReason: string | null;

  mp: Availability & {
    regime: string | null;
    dayType: string | null;
    poc: number | null;
    vah: number | null;
    val: number | null;
    migrationState: string | null;
    migrationDirection: string | null;
  };
  auction: Availability & {
    regime: string | null;
    allowed: boolean | null;
    reasons: string[];
    openLots: number;
  };
  mpof: Availability & {
    signal: string | null;
    candidate: string | null;
    blockReason: string | null;
    confidence: number | null;
    ofSource: string | null;
    ofCoveredBars: number | null;
    detail: string | null;
  };
  convergence: Availability & {
    setupState: string | null;
    action: string | null;
    score: number | null;
    confirmations: number | null;
    required: number | null;
    quality: string | null;
    direction: string | null;
    blocked: string[];
    cvdSource: string | null;
    footprintSource: string | null;
  };
  options: Availability & {
    pcr: number | null;
    ceOiChangePct: number | null;
    peOiChangePct: number | null;
    iv: number | null;
    callWall: number | null;
    putWall: number | null;
  };
  risk: Availability & {
    planComplete: boolean | null;
    missing: string[];
    rrText: string;
    rr: number | null;
    entry: number | null;
    stop: number | null;
    target1: number | null;
  };
  intent: {
    lanes: string[];
    legs: number;
    netQty: number;
    side: "LONG" | "SHORT" | "MIXED" | null;
    unrealized: number | null;
  };
};

/** A base row plus the freshness verdict, recomputed by the view's one ticker. */
export type MatrixRow = MatrixRowBase & {
  provenance: Provenance;
  grade: SourceGrade;
  freshness: Freshness;
  ageSeconds: number | null;
  sufficiency: Sufficiency;
  readinessReasons: string[];
};

export type ColumnCoverage = {
  key: string;
  label: string;
  covered: number;
  total: number;
  /** Non-null when the column has NO source at this scope at all. */
  unavailableReason: string | null;
};

export type UniverseMatrix = {
  rows: MatrixRowBase[];
  coverage: ColumnCoverage[];
  generatedAt: string | null;
  universeSource: string | null;
  universeDetail: string | null;
  isLoading: boolean;
  isFetching: boolean;
  errors: string[];
};

// ─── Payload fragments (only what the matrix reads) ──────────────────────────

type WatchlistLeg = {
  as_of?: string | null;
  oi?: number | null;
  oi_change_pct?: number | null;
  iv?: number | null;
  ltp?: number | null;
};
type WatchlistRow = {
  underlying?: string;
  kind?: string;
  spot_price?: number | null;
  as_of?: string | null;
  expiry?: string | null;
  lot_size?: number | null;
  live_source?: string | null;
  ce?: WatchlistLeg | null;
  pe?: WatchlistLeg | null;
};
type WatchlistPayload = {
  rows?: WatchlistRow[] | null;
  source?: string | null;
  detail?: string | null;
  build_status?: string | null;
  timestamp?: string | null;
  expiry?: string | null;
};

type ConvergenceResult = {
  symbol?: string;
  kind?: string | null;
  setup_state?: string | null;
  action?: string | null;
  score?: number | null;
  quality?: string | null;
  preferred_direction?: string | null;
  confirmation_count?: number | null;
  confirmation_required?: number | null;
  blocked_reasons?: string[] | null;
  futures_contract?: string | null;
  tick_age_ms?: number | null;
  tick_freshness_limit_ms?: number | null;
  cvd?: { source?: string | null } | null;
  footprint?: { source?: string | null } | null;
  options?: { call_wall?: number | null; put_wall?: number | null } | null;
  risk?: {
    entry?: number | null;
    stop?: number | null;
    target1?: number | null;
    reward_risk?: number | null;
  } | null;
};
type ConvergencePayload = {
  market_open?: boolean;
  latest?: { generated_at?: string | null; results?: ConvergenceResult[] | null } | null;
  paper?: { open_positions?: Array<Record<string, unknown>> | null } | null;
};

type IndexMonitorRow = {
  symbol?: string;
  underlying?: string;
  regime?: string | null;
  signal?: string | null;
  candidate_signal?: string | null;
  candidate_reason?: string | null;
  reason?: string | null;
  signal_validation_detail?: string | null;
  confidence?: number | null;
  of_source?: string | null;
  of_tick_covered_bars?: number | null;
  mp_day_type?: string | null;
  mp_poc?: number | null;
  mp_vah?: number | null;
  mp_val?: number | null;
  mp_status?: string | null;
  value_migration_state?: string | null;
  value_migration_direction?: string | null;
  price?: number | null;
  change_pct?: number | null;
  bar_time?: string | null;
};

type PositionRow = {
  symbol?: string;
  action?: string;
  qty?: number | null;
  unrealized_pnl?: number | null;
  strategy_key?: string | null;
  strategy_label?: string | null;
  source?: string | null;
};

type AuctionSummary = {
  paper_trading?: {
    summary?: { last_synced_at?: string | null } | null;
    open_positions?: Array<{ underlying_symbol?: string; quantity?: number | null; regime_last?: string | null; signal_action?: string | null }> | null;
  } | null;
};

// ─── Helpers ────────────────────────────────────────────────────────────────

/**
 * Number coercion that PRESERVES missing-ness. `Number(null)` is 0 and
 * `Number("")` is 0, so the naive version silently converts every absent field
 * into a measured zero — which is precisely the failure this workspace exists
 * to remove (a null stop becoming `0.00` and yielding a fabricated 1.00R).
 */
const num = (v: unknown): number | null => {
  if (v == null || v === "") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
};

/** `OPT:HAL:2026-07-28:4400:PE` → `HAL`. Anything else is passed through. */
export function underlyingOfPositionSymbol(symbol: string): string {
  const parts = String(symbol || "").split(":");
  if (parts.length >= 2 && parts[0].toUpperCase() === "OPT") return parts[1].toUpperCase();
  return String(symbol || "").toUpperCase();
}

function emptyRow(symbol: string, market: MarketKey, kind: string): MatrixRowBase {
  return {
    symbol,
    kind,
    market,
    contract: null,
    spot: null,
    changePct: null,
    lotSize: null,
    expiry: null,
    asOf: null,
    source: null,
    sourceFeature: "quote" as MarketFeature,
    tickAgeMs: null,
    tickLimitMs: null,
    degradedReason: null,
    mp: { ...unavailable("no market-profile source for this instrument"), regime: null, dayType: null, poc: null, vah: null, val: null, migrationState: null, migrationDirection: null },
    auction: { ...unavailable("auction state is per-symbol only — open the drawer"), regime: null, allowed: null, reasons: [], openLots: 0 },
    mpof: { ...unavailable("MP+OF monitor covers NIFTY / BANKNIFTY only"), signal: null, candidate: null, blockReason: null, confidence: null, ofSource: null, ofCoveredBars: null, detail: null },
    convergence: { ...unavailable("not in the convergence scan universe"), setupState: null, action: null, score: null, confirmations: null, required: null, quality: null, direction: null, blocked: [], cvdSource: null, footprintSource: null },
    options: { ...unavailable("no option chain row"), pcr: null, ceOiChangePct: null, peOiChangePct: null, iv: null, callWall: null, putWall: null },
    risk: { ...unavailable("no trade plan from any lane"), planComplete: null, missing: [], rrText: "—", rr: null, entry: null, stop: null, target1: null },
    intent: { lanes: [], legs: 0, netQty: 0, side: null, unrealized: null },
  };
}

// ─── The hook ───────────────────────────────────────────────────────────────

export function useUniverseMatrix(market: MarketKey): UniverseMatrix {
  const isNse = market === "NSE";

  const watchlist = useQuery({
    queryKey: ["ms-matrix", "atm-watchlist"],
    queryFn: async () => (await getATMWatchlist()).data as WatchlistPayload,
    enabled: isNse,
    refetchInterval: REFRESH_MS.summary,
    refetchOnWindowFocus: false,
    retry: false,
  });

  const convergence = useQuery({
    queryKey: ["ms-matrix", "convergence", market],
    queryFn: async () =>
      (isNse
        ? (await getInstitutionalConvergenceStatus()).data
        : (await getCommodityInstitutionalConvergenceStatus()).data) as ConvergencePayload,
    refetchInterval: REFRESH_MS.summary,
    refetchOnWindowFocus: false,
    retry: false,
  });

  const indexMonitor = useQuery({
    queryKey: ["ms-matrix", "index-monitor"],
    queryFn: async () =>
      (await getCommodityIndexMonitor()).data as { rows?: IndexMonitorRow[] | null; as_of?: string | null },
    enabled: isNse,
    refetchInterval: REFRESH_MS.summary,
    refetchOnWindowFocus: false,
    retry: false,
  });

  const positions = useQuery({
    queryKey: ["ms-matrix", "positions"],
    queryFn: async () => (await getPositions()).data as PositionRow[],
    refetchInterval: REFRESH_MS.summary,
    refetchOnWindowFocus: false,
    retry: false,
  });

  const auction = useQuery({
    queryKey: ["ms-matrix", "auction-summary"],
    queryFn: async () => (await getAuctionIntelligenceSummary()).data as AuctionSummary,
    enabled: isNse,
    refetchInterval: REFRESH_MS.summary,
    refetchOnWindowFocus: false,
    retry: false,
  });

  return useMemo(() => {
    const rowsBySymbol = new Map<string, MatrixRowBase>();
    const universeSource = isNse ? watchlist.data?.source ?? null : "convergence_scan";
    const universeDetail = isNse ? watchlist.data?.detail ?? null : null;

    // 1 — the universe itself.
    if (isNse) {
      for (const r of watchlist.data?.rows ?? []) {
        const symbol = String(r.underlying ?? "").toUpperCase();
        if (!symbol) continue;
        const row = emptyRow(symbol, market, String(r.kind ?? "STOCK"));
        row.spot = num(r.spot_price);
        row.lotSize = num(r.lot_size);
        row.expiry = r.expiry ?? null;
        row.asOf = r.as_of ?? null;
        row.source = watchlist.data?.source ?? r.live_source ?? null;

        const ceOi = num(r.ce?.oi);
        const peOi = num(r.pe?.oi);
        const pcr = ceOi != null && peOi != null && ceOi > 0 ? peOi / ceOi : null;
        const hasChain = r.ce != null || r.pe != null;
        row.options = {
          ...(hasChain ? AVAILABLE : unavailable("no CE/PE leg in the watchlist row")),
          pcr,
          ceOiChangePct: num(r.ce?.oi_change_pct),
          peOiChangePct: num(r.pe?.oi_change_pct),
          iv: num(r.ce?.iv) ?? num(r.pe?.iv),
          callWall: null,
          putWall: null,
        };
        rowsBySymbol.set(symbol, row);
      }
    } else {
      for (const r of convergence.data?.latest?.results ?? []) {
        const symbol = String(r.symbol ?? "").toUpperCase();
        if (!symbol) continue;
        const row = emptyRow(symbol, market, String(r.kind ?? "commodity"));
        row.options = { ...unavailable("MCX option context is not in this lane's payload"), pcr: null, ceOiChangePct: null, peOiChangePct: null, iv: null, callWall: null, putWall: null };
        rowsBySymbol.set(symbol, row);
      }
    }

    // 2 — convergence overlay (stage, evidence, risk plan, contract, ticks).
    for (const r of convergence.data?.latest?.results ?? []) {
      const symbol = String(r.symbol ?? "").toUpperCase();
      if (!symbol) continue;
      const row = rowsBySymbol.get(symbol) ?? emptyRow(symbol, market, String(r.kind ?? "unknown"));
      rowsBySymbol.set(symbol, row);

      row.contract = r.futures_contract ?? row.contract;
      row.tickAgeMs = num(r.tick_age_ms);
      row.tickLimitMs = num(r.tick_freshness_limit_ms);

      // A universe member with a null setup_state was scanned but produced no
      // evaluation — that is "scanned, no result", not a missing source.
      const evaluated = r.setup_state != null || r.action != null;
      row.convergence = {
        ...(evaluated ? AVAILABLE : unavailable("in the scan universe but no evaluation this cycle")),
        setupState: r.setup_state ?? null,
        action: r.action ?? null,
        score: num(r.score),
        confirmations: num(r.confirmation_count),
        required: num(r.confirmation_required),
        quality: r.quality ?? null,
        direction: r.preferred_direction ?? null,
        blocked: Array.isArray(r.blocked_reasons) ? r.blocked_reasons.map(String) : [],
        cvdSource: r.cvd?.source ?? null,
        footprintSource: r.footprint?.source ?? null,
      };

      if (r.options && (r.options.call_wall != null || r.options.put_wall != null)) {
        row.options = {
          ...row.options,
          available: true,
          reason: null,
          callWall: num(r.options.call_wall),
          putWall: num(r.options.put_wall),
        };
      }

      // RISK — routed through the SAME rrRender the desks use, so a plan with a
      // null stop can never surface a reward/risk number here either.
      if (r.risk) {
        const verdict = rrRender(r.risk);
        row.risk = {
          ...AVAILABLE,
          planComplete: verdict.ok,
          missing: verdict.ok ? [] : verdict.missing,
          rrText: verdict.text,
          rr: verdict.ok ? verdict.value : null,
          entry: num(r.risk.entry),
          stop: num(r.risk.stop),
          target1: num(r.risk.target1),
        };
      }

      // MCX rows have no watchlist; the convergence scan time is their as_of.
      if (!row.asOf) row.asOf = convergence.data?.latest?.generated_at ?? null;
      if (!row.source) {
        row.source = r.cvd?.source ?? r.footprint?.source ?? null;
        if (row.source) row.sourceFeature = "flow_attribution";
      }
    }

    // 3 — MP + MP/OF overlay. NSE indices only; every other row keeps its
    //     explicit "no source" state rather than being silently blanked.
    for (const r of indexMonitor.data?.rows ?? []) {
      const symbol = String(r.symbol ?? r.underlying ?? "").toUpperCase();
      if (!symbol) continue;
      const row = rowsBySymbol.get(symbol);
      if (!row) continue;
      const mpReady = r.mp_status != null || r.mp_poc != null;
      row.mp = {
        ...(mpReady ? AVAILABLE : unavailable("MP not built for this session yet")),
        regime: r.regime ?? null,
        dayType: r.mp_day_type ?? null,
        poc: num(r.mp_poc),
        vah: num(r.mp_vah),
        val: num(r.mp_val),
        migrationState: r.value_migration_state ?? null,
        migrationDirection: r.value_migration_direction ?? null,
      };
      row.mpof = {
        ...AVAILABLE,
        signal: r.signal ?? null,
        candidate: r.candidate_signal ?? null,
        blockReason: r.candidate_reason ?? r.reason ?? null,
        confidence: num(r.confidence),
        ofSource: r.of_source ?? null,
        ofCoveredBars: num(r.of_tick_covered_bars),
        detail: r.signal_validation_detail ?? null,
      };
      if (row.spot == null) row.spot = num(r.price);
      if (row.changePct == null) row.changePct = num(r.change_pct);
    }

    // 4 — portfolio intent, from the real book (small payload) + the auction
    //     lane's own paper book. Lanes that expose only megabyte-scale position
    //     endpoints are excluded here and reachable from their own desks.
    for (const p of positions.data ?? []) {
      const symbol = underlyingOfPositionSymbol(String(p.symbol ?? ""));
      const row = rowsBySymbol.get(symbol);
      if (!row) continue;
      const qty = num(p.qty) ?? 0;
      const signed = String(p.action ?? "BUY").toUpperCase() === "SELL" ? -qty : qty;
      row.intent.legs += 1;
      row.intent.netQty += signed;
      row.intent.unrealized = (row.intent.unrealized ?? 0) + (num(p.unrealized_pnl) ?? 0);
      const lane = String(p.strategy_key ?? p.source ?? "book");
      if (!row.intent.lanes.includes(lane)) row.intent.lanes.push(lane);
    }
    for (const p of auction.data?.paper_trading?.open_positions ?? []) {
      const symbol = String(p.underlying_symbol ?? "").toUpperCase();
      const row = rowsBySymbol.get(symbol);
      if (!row) continue;
      const qty = num(p.quantity) ?? 0;
      const signed = String(p.signal_action ?? "LONG").toUpperCase() === "SHORT" ? -qty : qty;
      row.intent.legs += 1;
      row.intent.netQty += signed;
      if (!row.intent.lanes.includes("auction_intelligence")) row.intent.lanes.push("auction_intelligence");
      // The auction lane's own regime for a symbol it HOLDS is an observation,
      // but it is the regime recorded on the position, not a fresh snapshot —
      // the cell says so rather than passing it off as current auction state.
      row.auction = {
        ...AVAILABLE,
        regime: p.regime_last ?? row.auction.regime,
        allowed: row.auction.allowed,
        reasons: ["from the auction lane's open position, not a fresh snapshot"],
        openLots: row.auction.openLots + 1,
      };
    }
    rowsBySymbol.forEach((row) => {
      row.intent.side =
        row.intent.netQty > 0 ? "LONG" : row.intent.netQty < 0 ? "SHORT" : row.intent.legs > 0 ? "MIXED" : null;
    });

    const rows = Array.from(rowsBySymbol.values()).sort((a, b) => a.symbol.localeCompare(b.symbol));
    const total = rows.length || 1;
    const count = (fn: (r: MatrixRowBase) => boolean) => rows.filter(fn).length;

    const coverage: ColumnCoverage[] = [
      { key: "readiness", label: "Readiness", covered: count((r) => !!r.asOf), total, unavailableReason: null },
      {
        key: "mp",
        label: "MP regime",
        covered: count((r) => r.mp.available),
        total,
        unavailableReason: isNse
          ? null
          : "the index MP monitor is NSE-only; MCX profiles live on the commodity desk",
      },
      {
        key: "auction",
        label: "Auction",
        covered: count((r) => r.auction.available),
        total,
        unavailableReason:
          "auction state has no universe-scale endpoint (live-snapshot is 59 KB per symbol) — loaded per selection",
      },
      { key: "mpof", label: "MP+OF", covered: count((r) => r.mpof.available), total, unavailableReason: isNse ? null : "MCX MP+OF lives on the commodity desk" },
      { key: "convergence", label: "Convergence", covered: count((r) => r.convergence.available), total, unavailableReason: null },
      { key: "options", label: "Options / OI", covered: count((r) => r.options.available), total, unavailableReason: isNse ? null : "no MCX option context in these lanes" },
      { key: "risk", label: "Risk plan", covered: count((r) => r.risk.available), total, unavailableReason: null },
      {
        key: "intent",
        label: "Intent",
        covered: count((r) => r.intent.legs > 0),
        total,
        // Partial by construction — say which books were read, so `flat*` is
        // never mistaken for "no exposure anywhere".
        unavailableReason:
          "composed from the real broker book and the auction paper book only; lanes whose position endpoint is megabyte-scale are read from their own desk, so `flat*` means flat in the polled books",
      },
    ];

    const errors = [
      watchlist.error ? "atm-watchlist unavailable" : null,
      convergence.error ? "convergence status unavailable" : null,
      indexMonitor.error ? "index monitor unavailable" : null,
      positions.error ? "positions unavailable" : null,
      auction.error ? "auction summary unavailable" : null,
    ].filter(Boolean) as string[];

    return {
      rows,
      coverage,
      generatedAt:
        (isNse ? watchlist.data?.timestamp : null) ??
        convergence.data?.latest?.generated_at ??
        null,
      universeSource,
      universeDetail,
      isLoading:
        (isNse && watchlist.isLoading) || convergence.isLoading,
      isFetching:
        watchlist.isFetching || convergence.isFetching || indexMonitor.isFetching || positions.isFetching || auction.isFetching,
      errors,
    };
  }, [
    isNse,
    market,
    watchlist.data,
    watchlist.error,
    watchlist.isLoading,
    watchlist.isFetching,
    convergence.data,
    convergence.error,
    convergence.isLoading,
    convergence.isFetching,
    indexMonitor.data,
    indexMonitor.error,
    indexMonitor.isFetching,
    positions.data,
    positions.error,
    positions.isFetching,
    auction.data,
    auction.error,
    auction.isFetching,
  ]);
}

/**
 * Decorate base rows with the freshness verdict. Called ONCE per tick in the
 * view (not per row component) so 216 rows cost one memo pass and ZERO timers —
 * the matrix must never open a per-symbol subscription.
 */
export function decorateRows(
  rows: MatrixRowBase[],
  nowMs: number,
  opts: { replay?: boolean; sessionOpen?: boolean } = {},
): MatrixRow[] {
  // An explicit replay pin and a closed session are the same claim about the
  // numbers: they describe a session that is not the one happening now. Saying
  // "unknown" instead would be a hedge the desks do not make.
  const replayed = !!opts.replay || opts.sessionOpen === false;
  return rows.map((r) => {
    const provenance = provenanceOf({
      source: r.source,
      feature: r.sourceFeature,
      asOf: r.asOf,
      dataMode: replayed ? "historical_replay" : undefined,
      dataStatus: replayed ? { snapshot_mode: "historical_replay" } : undefined,
      degradedReason: r.degradedReason,
      tickAgeMs: r.tickAgeMs,
      tickLimitMs: r.tickLimitMs,
      // Strategy gate blockers are deliberately NOT fed in here: a blocked
      // setup is an evidence fact, not a data-quality fact, and conflating the
      // two would paint every honest row "degraded".
      nowMs,
    });
    return {
      ...r,
      provenance,
      grade: provenance.grade,
      freshness: provenance.freshness,
      ageSeconds: provenance.ageSeconds,
      sufficiency: provenance.sufficiency,
      readinessReasons: provenance.reasons,
    };
  });
}

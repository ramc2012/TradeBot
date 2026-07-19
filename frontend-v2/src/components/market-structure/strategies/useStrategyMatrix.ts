"use client";

/**
 * useStrategyMatrix — composes the FOUR policies' read on the pinned instrument.
 *
 * ─── Sourcing, and why there is no new backend endpoint ─────────────────────
 *
 * Two of the four policies keep their per-symbol state ONLY inside a heavy
 * snapshot (auction: 59 KB/symbol; directional: a full live-snapshot per
 * underlying; commodity MP+OF: /api/commodity/overview). Fetching all three on
 * every pin change would re-create exactly the poll-starvation class the
 * backend was cured of, so they are DETAIL-ON-DEMAND: nothing is fetched until
 * the trader asks for that policy, and until then the cell renders UNAVAILABLE
 * with the reason — never a guess, never a stale carry-over.
 *
 * An aggregate endpoint would collapse the three fetches into one ~4 KB call.
 * It is deliberately NOT added in this pass: the live stack cannot be restarted
 * this weekend, so a new route would 404 against the running backend and this
 * composition is what would run anyway. When it lands, it replaces the bodies
 * below and the on-demand gating stays as the fallback.
 *
 * ─── Query-key discipline ───────────────────────────────────────────────────
 *
 * The convergence detail reuses the drawer's EXACT key
 * (`["ms-detail","convergence",market,symbol]`), so drawer + Strategies view
 * share ONE poll rather than doubling the pinned-symbol rate. The two heavy
 * on-demand queries have `refetchInterval:false` and are gated on an explicit
 * request, mirroring `useInstrumentDetail`'s auction rule.
 */
import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import { REFRESH_MS } from "@/components/desk-ui";
import {
  getAuctionIntelligenceLiveSnapshot,
  getCommodityOverview,
  getDirectionalOptionsLiveSnapshot,
  getInstitutionalConvergenceDetail,
} from "@/lib/api";
import {
  auctionCell,
  auctionWaterfall,
  convergenceCell,
  convergenceWaterfall,
  directionalCell,
  directionalWaterfall,
  findDisagreements,
  mpofCell,
  mpofWaterfall,
  opinionCount,
  type AuctionInput,
  type ConvergenceInput,
  type DirectionalInput,
  type MpofInput,
  type PolicyCellData,
  type PolicyId,
  type WaterfallStage,
} from "@/lib/policy-state";

import type { MatrixRow } from "../command/useUniverseMatrix";
import type { WorkspaceContext } from "../context/schema";

// ─── Loose payload shapes (only the fields read here) ───────────────────────

type Dict = Record<string, unknown>;
const dict = (v: unknown): Dict => (v && typeof v === "object" ? (v as Dict) : {});
const arr = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);
const str = (v: unknown): string | null => (v == null || v === "" ? null : String(v));
const num = (v: unknown): number | null => {
  if (v == null || v === "") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
};
const bool = (v: unknown): boolean | null => (typeof v === "boolean" ? v : null);
const strList = (v: unknown): string[] => arr(v).map((x) => String(x)).filter(Boolean);

export type PolicyEntry = {
  policyId: PolicyId;
  cell: PolicyCellData;
  stages: WaterfallStage[];
  /** Non-null ⇒ this policy has a heavy fetch the trader can trigger. */
  loader: { label: string; loaded: boolean; loading: boolean; onLoad: () => void } | null;
};

export type StrategyMatrix = {
  entries: Record<PolicyId, PolicyEntry>;
  disagreements: ReturnType<typeof findDisagreements>;
  opinions: number;
  convergenceDetailLoading: boolean;
};

export function useStrategyMatrix(ctx: WorkspaceContext, row: MatrixRow | null, active: boolean): StrategyMatrix {
  const symbol = row?.symbol ?? ctx.symbol;
  const enabled = active && !!symbol;

  const [wantAuction, setWantAuction] = useState(false);
  const [wantDirectional, setWantDirectional] = useState(false);
  const [wantCommodity, setWantCommodity] = useState(false);

  // Same key as the drawer's detail query ⇒ one shared poll, not two.
  const convergenceDetail = useQuery({
    queryKey: ["ms-detail", "convergence", ctx.market, symbol],
    queryFn: async () =>
      (await getInstitutionalConvergenceDetail(symbol, ctx.market)).data as Dict,
    enabled,
    refetchInterval: REFRESH_MS.snapshot,
    refetchOnWindowFocus: false,
    retry: false,
  });

  const auctionDetail = useQuery({
    queryKey: ["ms-detail", "auction", symbol],
    queryFn: async () => (await getAuctionIntelligenceLiveSnapshot(symbol)).data as Dict,
    enabled: enabled && wantAuction,
    refetchInterval: false,
    refetchOnWindowFocus: false,
    retry: false,
  });

  const directionalDetail = useQuery({
    queryKey: ["ms-detail", "directional", symbol],
    queryFn: async () => (await getDirectionalOptionsLiveSnapshot(symbol)).data as Dict,
    enabled: enabled && wantDirectional,
    refetchInterval: false,
    refetchOnWindowFocus: false,
    retry: false,
  });

  const commodityDetail = useQuery({
    queryKey: ["ms-detail", "commodity-overview"],
    queryFn: async () => (await getCommodityOverview()).data as Dict,
    enabled: enabled && wantCommodity,
    refetchInterval: false,
    refetchOnWindowFocus: false,
    retry: false,
  });

  return useMemo(() => {
    // ── Convergence ────────────────────────────────────────────────────────
    const detailResult = dict(dict(convergenceDetail.data).result);
    const convergenceInput: ConvergenceInput = {
      available: row?.convergence.available ?? false,
      reason: row?.convergence.reason ?? "no instrument pinned",
      kind: str(detailResult.kind) ?? row?.kind ?? null,
      setupState: row?.convergence.setupState ?? null,
      action: row?.convergence.action ?? null,
      quality: row?.convergence.quality ?? null,
      direction: row?.convergence.direction ?? null,
      score: row?.convergence.score ?? null,
      confirmations: row?.convergence.confirmations ?? null,
      required: row?.convergence.required ?? null,
      blocked: row?.convergence.blocked ?? [],
      gates: (detailResult.gates as Record<string, boolean> | undefined) ?? null,
      readinessGates: (detailResult.readiness_gates as Record<string, boolean> | undefined) ?? null,
      tickAgeMs: row?.tickAgeMs ?? null,
      tickLimitMs: row?.tickLimitMs ?? null,
      rr: row?.risk.rr ?? null,
      entry: row?.risk.entry ?? null,
      stop: row?.risk.stop ?? null,
      target1: row?.risk.target1 ?? null,
      hasOpenPosition: (row?.intent.lanes ?? []).some((l) =>
        String(l).toLowerCase().includes("convergence"),
      ),
    };

    // ── Auction (heavy, on request) ────────────────────────────────────────
    const auctionPayload = dict(auctionDetail.data);
    const analysis = dict(auctionPayload.analysis);
    const auctionRisk = dict(analysis.risk);
    const auctionRegime = dict(analysis.regime);
    const auctionDataStatus = dict(auctionPayload.data_status);
    const agentDecisions = arr(analysis.agent_decisions).map(dict);
    const auctionLoaded = auctionDetail.isSuccess;
    const auctionInput: AuctionInput & { staleSeconds?: number | null; snapshotMode?: string | null } = {
      loaded: auctionLoaded,
      reason: auctionDetail.isError
        ? "the auction snapshot request failed for this instrument"
        : null,
      regime: str(auctionRegime.label) ?? row?.auction.regime ?? null,
      confidence: num(auctionRegime.confidence),
      allowedDirections: strList(auctionRegime.allowed_directions),
      allowed: auctionLoaded ? bool(auctionRisk.allowed) : row?.auction.allowed ?? null,
      killSwitch: bool(auctionRisk.kill_switch),
      reasons: auctionLoaded ? strList(auctionRisk.reasons) : row?.auction.reasons ?? [],
      agentActions: agentDecisions.map((d) => String(d.action ?? "")),
      agentConfidence: agentDecisions.length ? num(agentDecisions[0].confidence) : null,
      executionPlanCount: auctionLoaded ? arr(analysis.execution_plan).length : null,
      staleSeconds: num(auctionDataStatus.stale_data_seconds),
      snapshotMode: str(auctionDataStatus.snapshot_mode),
    };

    // ── MP+OF, index (already in the summary tier) ─────────────────────────
    const mpofIndexInput: MpofInput & { ofSource?: string | null; ofCoveredBars?: number | null } = {
      available: row?.mpof.available ?? false,
      reason: row?.mpof.reason ?? "no instrument pinned",
      mpStatus: row?.mp.available ? "ready" : null,
      dataReason: null,
      signal: row?.mpof.signal ?? null,
      candidate: row?.mpof.candidate ?? null,
      candidateReason: row?.mpof.blockReason ?? null,
      validationDetail: row?.mpof.detail ?? null,
      confidence: row?.mpof.confidence ?? null,
      mpDirection: row?.mp.regime ?? null,
      ofSource: row?.mpof.ofSource ?? null,
      ofCoveredBars: row?.mpof.ofCoveredBars ?? null,
    };

    // ── MP+OF, commodity (heavy, on request) ───────────────────────────────
    const commodityStatus = dict(dict(commodityDetail.data).status);
    const commodityRows = arr(commodityStatus.futures_watchlist).map(dict);
    const commodityRow = commodityRows.find((r) => {
      const keys = [r.symbol, r.underlying, r.configured_symbol, r.display_name];
      return keys.some((k) => String(k ?? "").toUpperCase().includes(symbol.toUpperCase()));
    });
    const commodityLoaded = commodityDetail.isSuccess;
    const isMcx = ctx.market === "MCX";
    const mpofCommodityInput: MpofInput & {
      ofSource?: string | null;
      htfBias?: string | null;
      isCommodity?: boolean;
    } = {
      available: Boolean(commodityLoaded && commodityRow),
      reason: !isMcx
        ? "the commodity MP+OF policy answers for MCX roots only"
        : !commodityLoaded
          ? "commodity MP+OF state lives in /api/commodity/overview (306 KB) — load it for this instrument"
          : "this root is not in the commodity strategy watchlist",
      mpStatus: str(commodityRow?.mp_status),
      dataReason: str(commodityRow?.reason),
      signal: str(commodityRow?.signal),
      candidate: str(commodityRow?.candidate_signal),
      candidateReason: str(commodityRow?.reason),
      validationDetail: str(commodityRow?.signal_validation_detail),
      confidence: num(commodityRow?.confidence),
      mpDirection: str(commodityRow?.mp_direction),
      htfBias: str(commodityRow?.htf_bias),
      isCommodity: true,
    };

    // ── Directional (heavy, on request) ────────────────────────────────────
    const dPayload = dict(directionalDetail.data);
    const dRegime = dict(dPayload.regime);
    const dSignal = dict(dPayload.signal);
    const dRisk = dict(dPayload.risk);
    const dPolicy = dict(dPayload.policy);
    const dModel = dict(dPolicy.model);
    const dDataStatus = dict(dPayload.data_status);
    const directionalLoaded = directionalDetail.isSuccess;
    const directionalInput: DirectionalInput = {
      loaded: directionalLoaded,
      reason: directionalDetail.isError
        ? "the directional live snapshot request failed for this underlying"
        : null,
      regimeLabel: str(dRegime.label),
      tradeAllowed: bool(dRegime.trade_allowed),
      regimeReasons: strList(dRegime.reasons),
      signalDirection: dPayload.signal ? str(dSignal.direction) : null,
      signalConfidence: num(dSignal.confidence),
      thesis: str(dSignal.thesis),
      positional: bool(dSignal.positional),
      hasSelectedContract: directionalLoaded ? Boolean(dPayload.selected_contract) : null,
      riskReasons: strList(dRisk.reasons),
      ruleBlockers: strList(dModel.rule_blockers),
      executionReady: bool(dDataStatus.execution_ready),
      degradedReason: str(dDataStatus.degraded_reason),
      selectionReason: str(dPayload.selection_reason),
    };

    const entries: Record<PolicyId, PolicyEntry> = {
      convergence: {
        policyId: "convergence",
        cell: convergenceCell(convergenceInput),
        stages: convergenceWaterfall(convergenceInput),
        loader: null,
      },
      auction: {
        policyId: "auction",
        cell: auctionCell(auctionInput),
        stages: auctionWaterfall(auctionInput),
        loader: {
          label: "Load auction read (59 KB)",
          loaded: auctionLoaded,
          loading: auctionDetail.isFetching,
          onLoad: () => setWantAuction(true),
        },
      },
      mpof_index: {
        policyId: "mpof_index",
        cell: mpofCell("mpof_index", mpofIndexInput),
        stages: mpofWaterfall({ ...mpofIndexInput, isCommodity: false }),
        loader: null,
      },
      mpof_commodity: {
        policyId: "mpof_commodity",
        cell: mpofCell("mpof_commodity", mpofCommodityInput),
        stages: mpofWaterfall(mpofCommodityInput),
        loader: isMcx
          ? {
              label: "Load commodity read (306 KB)",
              loaded: commodityLoaded,
              loading: commodityDetail.isFetching,
              onLoad: () => setWantCommodity(true),
            }
          : null,
      },
      directional: {
        policyId: "directional",
        cell: directionalCell(directionalInput),
        stages: directionalWaterfall(directionalInput),
        loader: {
          label: "Load directional read",
          loaded: directionalLoaded,
          loading: directionalDetail.isFetching,
          onLoad: () => setWantDirectional(true),
        },
      },
    };

    const cells = Object.values(entries).map((e) => e.cell);
    return {
      entries,
      disagreements: findDisagreements(cells),
      opinions: opinionCount(cells),
      convergenceDetailLoading: convergenceDetail.isLoading,
    };
  }, [
    ctx.market,
    symbol,
    row,
    convergenceDetail.data,
    convergenceDetail.isLoading,
    auctionDetail.data,
    auctionDetail.isSuccess,
    auctionDetail.isError,
    auctionDetail.isFetching,
    directionalDetail.data,
    directionalDetail.isSuccess,
    directionalDetail.isError,
    directionalDetail.isFetching,
    commodityDetail.data,
    commodityDetail.isSuccess,
    commodityDetail.isFetching,
  ]);
}

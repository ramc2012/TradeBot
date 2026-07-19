"use client";

/**
 * useMarketCanvas — the ONE detail query set behind the Structure and Flow
 * views, for exactly one pinned instrument.
 *
 * Two rules carried over from `useInstrumentDetail`, because they are what kept
 * this workspace from re-creating the poll storm the backend was cured of:
 *
 *   1. Every query is gated on `enabled` (the caller passes
 *      `ctx.view === "structure" || ctx.view === "flow"`) AND on a symbol. A
 *      view that is not on screen fetches nothing.
 *   2. Nothing here fetches the enormous per-symbol payloads on a timer that
 *      the matrix already refuses to (`/api/commodity/overview`,
 *      `/api/directional-options/paper-positions`, the auction live-snapshot).
 *
 * The convergence detail reuses the EXACT query key the drawer uses
 * (`["ms-detail","convergence",market,symbol]`), so opening the drawer and the
 * Structure view at the same time is ONE poll, not two.
 *
 * ─── SOURCE TIERS, and why the view says which one it is on ─────────────────
 *
 * There is no single endpoint that serves price + flow + profile for all 216
 * instruments, so the canvas resolves a tier and DECLARES it:
 *
 *   orderflow   NIFTY / BANKNIFTY / SENSEX / CRUDEOIL only (the backend's
 *               SUPPORTED_SYMBOLS). `/api/orderflow/snapshot` returns 3-minute
 *               footprint bars carrying BOTH the OHLC and the per-bar delta, so
 *               the price pane and the flow pane are on identical timestamps by
 *               construction — the strongest form of the alignment the shared
 *               crosshair needs.
 *   convergence the ~12 NSE / 8 MCX convergence names. `result.bars` (3-minute
 *               OHLCV) for price, `result.cvd.series` for flow, and the only
 *               source in the app for prior-session levels, TPO counts, HVNs
 *               and single prints.
 *   ohlc        everything else. `/api/charts/ohlc` gives price bars and
 *               NOTHING else, so the flow pane and the profile workbench render
 *               explicit unavailability rather than an empty frame.
 *
 * A tier is never claimed: if the tier's payload fails or comes back empty the
 * canvas falls to the next one and reports both facts.
 */
import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";

import { REFRESH_MS } from "@/components/desk-ui";
import type { FootprintBar } from "@/components/mpof";
import {
  describeApiError,
  getChartOHLC,
  getInstitutionalConvergenceDetail,
  getOrderflowSnapshot,
} from "@/lib/api";

import type { MarketKey } from "../context/schema";

import { toChartTime } from "./chart-time";

/* eslint-disable @typescript-eslint/no-explicit-any */

/** The backend's `SUPPORTED_SYMBOLS` for /api/orderflow/snapshot. */
export const ORDERFLOW_SYMBOLS = ["NIFTY", "BANKNIFTY", "SENSEX", "CRUDEOIL"] as const;

export function orderflowSymbolFor(symbol: string): string | null {
  const s = String(symbol || "").toUpperCase();
  return (ORDERFLOW_SYMBOLS as readonly string[]).includes(s) ? s : null;
}

export type CanvasTier = "orderflow" | "convergence" | "ohlc" | "none";

export type CanvasBar = {
  time: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
};

export type FlowPoint = {
  time: string;
  /** Cumulative delta at this bar. Buy/sell attribution is inferred. */
  cvd: number;
  /** Per-bar signed volume. Same derivation, same caveat. */
  delta: number;
  close: number;
};

export type CanvasLevels = {
  poc: number | null;
  vah: number | null;
  val: number | null;
  ibHigh: number | null;
  ibLow: number | null;
  dayHigh: number | null;
  dayLow: number | null;
  /**
   * The LAST vwap value, not a series. No payload in this stack carries a vwap
   * curve, so the price pane draws a labelled level and says "(last)". A drawn
   * curve would be fabricated.
   */
  vwapLast: number | null;
  prior: { vah: number | null; val: number | null; poc: number | null };
};

export type CanvasPlan = {
  entry: number | null;
  stop: number | null;
  target1: number | null;
  target2: number | null;
  rewardRisk: number | null;
  direction: string | null;
};

export type CanvasOptions = {
  expiry: string | null;
  callWall: number | null;
  putWall: number | null;
  topCallWalls: Array<{ strike: number; oi: number }>;
  topPutWalls: Array<{ strike: number; oi: number }>;
};

export type MarketCanvas = {
  tier: CanvasTier;
  /** Prose statement of which source served the price pane, and why. */
  tierNote: string;
  symbol: string;

  bars: CanvasBar[];
  barTimeframe: string | null;
  barSource: string | null;
  /** Non-null when there are no price bars at all — the view renders this. */
  barsUnavailable: string | null;

  flow: {
    points: FlowPoint[];
    source: string | null;
    timeframe: string | null;
    /** Non-null when the flow pane must NOT draw. */
    unavailable: string | null;
  };

  footprint: {
    bars: FootprintBar[];
    source: string | null;
    timeframe: string | null;
    unavailable: string | null;
  };

  levels: CanvasLevels;
  /**
   * Which payload served the CURRENT-session levels drawn on the price pane.
   * The workbench's distribution comes from `profileSource`, and the two can be
   * DIFFERENT payloads computing the same named level over different inputs, so
   * both are stated rather than silently reconciled.
   */
  levelsSource: string | null;
  /** Raw profile payload for the workbench, or null with a reason. */
  profile: any | null;
  profileSource: string | null;
  profileUnavailable: string | null;

  /** Live L1-derived microstructure metrics (orderflow tier only). */
  metrics: Record<string, any> | null;
  metricsSource: string | null;

  options: CanvasOptions | null;
  plan: CanvasPlan | null;

  /** Whether the served snapshot itself declared a synthetic quote path. */
  syntheticQuote: boolean | null;
  dataStatus: Record<string, any> | null;

  asOf: string | null;
  spot: number | null;

  isLoading: boolean;
  isFetching: boolean;
  errors: string[];
};

const num = (v: any): number | null => {
  const n = Number(v);
  return Number.isFinite(n) && n !== 0 ? n : null;
};
const numZ = (v: any): number => {
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
};

export function useMarketCanvas(
  symbol: string,
  market: MarketKey,
  enabled: boolean,
): MarketCanvas {
  const ofSymbol = orderflowSymbolFor(symbol);

  const ofQuery = useQuery({
    queryKey: ["ms-detail", "orderflow", ofSymbol],
    queryFn: async () =>
      // include_timeframes=false drops the multi-timeframe footprint history —
      // ~99% of the payload and unused here.
      (await getOrderflowSnapshot(ofSymbol as string, "3", 2, false)).data as any,
    enabled: enabled && !!ofSymbol,
    refetchInterval: REFRESH_MS.snapshot,
    refetchOnWindowFocus: false,
    retry: false,
  });

  // SAME key as the drawer's `useConvergenceDetail` — react-query dedupes, so
  // drawer + structure open together is one poll.
  const convQuery = useQuery({
    queryKey: ["ms-detail", "convergence", market, symbol],
    queryFn: async () =>
      (await getInstitutionalConvergenceDetail(symbol, market)).data as any,
    enabled: enabled && !!symbol,
    refetchInterval: REFRESH_MS.snapshot,
    refetchOnWindowFocus: false,
    retry: false,
  });

  // Only for instruments the two richer tiers cannot serve.
  const needsOhlc = enabled && !!symbol && !ofSymbol;
  const ohlcQuery = useQuery({
    queryKey: ["ms-detail", "ohlc", symbol],
    queryFn: async () => (await getChartOHLC(symbol, "30minute", 5)).data as any,
    enabled: needsOhlc,
    refetchInterval: REFRESH_MS.summary,
    refetchOnWindowFocus: false,
    retry: false,
  });

  return useMemo<MarketCanvas>(() => {
    const errors: string[] = [];
    if (ofQuery.error) errors.push(`order-flow snapshot: ${describeApiError(ofQuery.error)}`);
    if (convQuery.error) errors.push(`convergence detail: ${describeApiError(convQuery.error)}`);
    if (ohlcQuery.error) errors.push(`chart OHLC: ${describeApiError(ohlcQuery.error)}`);

    const instrument =
      (ofQuery.data?.instruments ?? []).find(
        (i: any) => String(i?.symbol ?? "").toUpperCase() === ofSymbol,
      ) ?? null;
    const ofFootprint: any[] = Array.isArray(instrument?.footprint) ? instrument.footprint : [];

    const convResult: any = convQuery.data?.result ?? null;
    const convBars: any[] = Array.isArray(convResult?.bars) ? convResult.bars : [];
    const convCvd: any[] = Array.isArray(convResult?.cvd?.series) ? convResult.cvd.series : [];
    const convFootprint: any[] = Array.isArray(convResult?.footprint?.bars)
      ? convResult.footprint.bars
      : [];

    const ohlcCandles: any[] = Array.isArray(ohlcQuery.data?.candles)
      ? ohlcQuery.data.candles
      : Array.isArray(ohlcQuery.data?.bars)
        ? ohlcQuery.data.bars
        : [];

    // ── Tier resolution ─────────────────────────────────────────────────────
    let tier: CanvasTier = "none";
    let tierNote = "";
    let bars: CanvasBar[] = [];
    let barTimeframe: string | null = null;
    let barSource: string | null = null;

    if (ofFootprint.length) {
      tier = "orderflow";
      tierNote =
        "price and flow both come from the SAME 3-minute footprint rows of /api/orderflow/snapshot, so the two panes share one bar clock by construction.";
      barTimeframe = "3m";
      barSource = instrument?.source?.history ?? instrument?.source?.quote ?? null;
      bars = ofFootprint.map((b: any) => ({
        time: String(b.timestamp ?? b.time ?? ""),
        open: numZ(b.open),
        high: numZ(b.high),
        low: numZ(b.low),
        close: numZ(b.close),
        volume: numZ(b.total_volume),
      }));
    } else if (convBars.length) {
      tier = "convergence";
      tierNote = ofSymbol
        ? "the order-flow snapshot returned no footprint rows, so price falls back to the convergence lane's own 3-minute bars."
        : "price comes from the institutional-convergence lane's 3-minute bars for this instrument.";
      barTimeframe = "3m";
      barSource = convResult?.footprint?.source ?? convResult?.cvd?.source ?? null;
      bars = convBars.map((b: any) => ({
        time: String(b.time ?? b.timestamp ?? ""),
        open: numZ(b.open),
        high: numZ(b.high),
        low: numZ(b.low),
        close: numZ(b.close),
        volume: numZ(b.volume),
      }));
    } else if (ohlcCandles.length) {
      tier = "ohlc";
      tierNote =
        "no lane serves 3-minute structure for this instrument, so price comes from /api/charts/ohlc at 30 minutes. Flow and profile have no source at this tier.";
      barTimeframe = "30m";
      barSource = ohlcQuery.data?.source ?? "charts_ohlc";
      bars = ohlcCandles.map((b: any) => ({
        time: String(b.timestamp ?? b.time ?? b.date ?? ""),
        open: numZ(b.open),
        high: numZ(b.high),
        low: numZ(b.low),
        close: numZ(b.close),
        volume: numZ(b.volume),
      }));
    }

    bars = bars.filter((b) => b.time && Number.isFinite(b.close) && b.close > 0);

    const anyLoading =
      (ofQuery.isLoading && !!ofSymbol) || convQuery.isLoading || (needsOhlc && ohlcQuery.isLoading);

    const barsUnavailable = bars.length
      ? null
      : anyLoading
        ? null
        : `no price bars for ${symbol} from any wired source (order-flow snapshot, convergence detail, charts OHLC)${
            errors.length ? ` — ${errors.join("; ")}` : ""
          }`;

    // ── Flow series ─────────────────────────────────────────────────────────
    let flowPoints: FlowPoint[] = [];
    let flowSource: string | null = null;
    let flowUnavailable: string | null = null;

    if (tier === "orderflow") {
      flowSource = instrument?.source?.order_flow ?? null;
      flowPoints = ofFootprint.map((b: any) => ({
        time: String(b.timestamp ?? b.time ?? ""),
        cvd: numZ(b.cumulative_delta),
        delta: numZ(b.delta),
        close: numZ(b.close),
      }));
    } else if (convCvd.length) {
      flowSource = convResult?.cvd?.source ?? null;
      let running = 0;
      flowPoints = convCvd.map((p: any, i: number) => {
        const cvd = numZ(p.cvd);
        const delta = i === 0 ? cvd : cvd - running;
        running = cvd;
        return { time: String(p.time ?? ""), cvd, delta, close: numZ(p.close) };
      });
    } else if (convFootprint.length) {
      flowSource = convResult?.footprint?.source ?? null;
      flowPoints = convFootprint.map((b: any) => ({
        time: String(b.time ?? ""),
        cvd: numZ(b.cumulative_delta),
        delta: numZ(b.delta),
        close: numZ(b.close),
      }));
    } else {
      flowUnavailable =
        tier === "ohlc"
          ? "no order-flow series exists for this instrument: it is outside /api/orderflow/snapshot's supported symbols and outside the convergence scan universe."
          : "the served payload carried no CVD or footprint series for this instrument in this cycle.";
    }
    flowPoints = flowPoints.filter((p) => p.time);

    // ── Footprint bars, mapped onto the shared FootprintGrid shape ──────────
    let footprintBars: FootprintBar[] = [];
    let footprintSource: string | null = null;
    let footprintUnavailable: string | null = null;
    if (ofFootprint.length) {
      footprintSource = instrument?.source?.order_flow ?? null;
      footprintBars = ofFootprint.map((b: any) => ({
        time: String(b.timestamp ?? b.time ?? ""),
        delta: numZ(b.delta),
        volume: numZ(b.total_volume),
        cumulative_delta: numZ(b.cumulative_delta),
        levels: (Array.isArray(b.levels) ? b.levels : []).map((l: any) => ({
          price: numZ(l.price),
          // The backend names these bid/ask; they ARE the inferred sell/buy
          // split of the bar's volume, so they are mapped, not relabelled.
          buy: numZ(l.ask_volume),
          sell: numZ(l.bid_volume),
        })),
      }));
    } else if (convFootprint.length) {
      footprintSource = convResult?.footprint?.source ?? null;
      footprintBars = convFootprint.map((b: any) => ({
        time: String(b.time ?? ""),
        delta: numZ(b.delta),
        volume: numZ(b.volume),
        cumulative_delta: numZ(b.cumulative_delta),
        levels: Array.isArray(b.levels) ? b.levels : [],
      }));
    } else {
      footprintUnavailable =
        "no footprint bars for this instrument — neither the order-flow snapshot nor the convergence lane emitted one this cycle.";
    }

    // ── Levels ──────────────────────────────────────────────────────────────
    const ofMp = instrument?.market_profile ?? null;
    const convProfile = convResult?.profile ?? null;
    const levels: CanvasLevels = {
      poc: num(ofMp?.poc) ?? num(convProfile?.poc),
      vah: num(ofMp?.vah) ?? num(convProfile?.vah),
      val: num(ofMp?.val) ?? num(convProfile?.val),
      ibHigh: num(ofMp?.initial_balance_high) ?? num(convProfile?.initial_balance_high),
      ibLow: num(ofMp?.initial_balance_low) ?? num(convProfile?.initial_balance_low),
      dayHigh: num(convProfile?.high_price),
      dayLow: num(convProfile?.low_price),
      vwapLast: num(instrument?.metrics?.vwap),
      prior: {
        vah: num(convProfile?.prior?.vah),
        val: num(convProfile?.prior?.val),
        poc: num(convProfile?.prior?.poc),
      },
    };

    // Which payload actually supplied the current-session levels above. The
    // order-flow snapshot and the convergence lane BOTH compute a POC/VAH/VAL,
    // over different inputs, so the same label can carry two different numbers
    // on one screen. That is not reconciled here — it is attributed.
    const ofServesLevels =
      num(ofMp?.poc) != null || num(ofMp?.vah) != null || num(ofMp?.val) != null;
    const levelsSource = ofServesLevels
      ? "/api/orderflow/snapshot · market_profile"
      : convProfile
        ? "/api/institutional-convergence/status/{symbol} · profile"
        : null;

    // ── Profile payload for the workbench ───────────────────────────────────
    const hasTpo =
      !!convProfile &&
      (Object.keys(convProfile.tpo_counts ?? {}).length > 0 ||
        (Array.isArray(convProfile.tpo_rows) && convProfile.tpo_rows.length > 0));
    const profile = convProfile ?? null;
    const profileUnavailable = hasTpo
      ? null
      : convProfile
        ? "the convergence lane served profile LEVELS for this instrument but no TPO distribution this cycle — the workbench renders levels only."
        : "no lane emits a market profile for this instrument: TPO counts, prior-session levels, HVNs and single prints come from /api/institutional-convergence/status/{symbol}, whose universe does not include it.";

    const risk = convResult?.risk ?? null;
    const plan: CanvasPlan | null = risk
      ? {
          entry: num(risk.entry),
          stop: num(risk.stop),
          target1: num(risk.target1),
          target2:
            num(risk.target2_long) ??
            num(risk.target2_short) ??
            null,
          rewardRisk: num(risk.reward_risk),
          direction: convResult?.preferred_direction ?? convResult?.action ?? null,
        }
      : null;

    const convOptions = convResult?.options ?? null;
    const options: CanvasOptions | null = convOptions
      ? {
          expiry: convOptions.expiry ?? null,
          callWall: num(convOptions.call_wall),
          putWall: num(convOptions.put_wall),
          topCallWalls: Array.isArray(convOptions.top_call_walls) ? convOptions.top_call_walls : [],
          topPutWalls: Array.isArray(convOptions.top_put_walls) ? convOptions.top_put_walls : [],
        }
      : null;

    return {
      tier,
      tierNote,
      symbol,
      bars,
      barTimeframe,
      barSource,
      barsUnavailable,
      flow: {
        points: flowPoints,
        source: flowSource,
        timeframe: barTimeframe,
        unavailable: flowUnavailable,
      },
      footprint: {
        bars: footprintBars,
        source: footprintSource,
        timeframe: barTimeframe,
        unavailable: footprintUnavailable,
      },
      levels,
      levelsSource,
      profile,
      profileSource: convProfile ? "institutional_convergence/status/{symbol}" : null,
      profileUnavailable,
      metrics: instrument?.metrics ?? null,
      metricsSource: instrument?.source?.order_flow ?? instrument?.source?.quote ?? null,
      options,
      plan,
      syntheticQuote:
        instrument?.synthetic_quote == null ? null : Boolean(instrument.synthetic_quote),
      dataStatus: instrument?.data_quality ?? null,
      asOf:
        instrument?.timestamp ??
        convResult?.footprint?.last_bar_time ??
        convResult?.cvd?.last_bar_time ??
        bars.at(-1)?.time ??
        null,
      spot: num(instrument?.price) ?? num(convResult?.spot) ?? num(bars.at(-1)?.close),
      isLoading: anyLoading && !bars.length,
      isFetching: ofQuery.isFetching || convQuery.isFetching || ohlcQuery.isFetching,
      errors,
    };
  }, [
    ofQuery.data,
    ofQuery.error,
    ofQuery.isLoading,
    ofQuery.isFetching,
    convQuery.data,
    convQuery.error,
    convQuery.isLoading,
    convQuery.isFetching,
    ohlcQuery.data,
    ohlcQuery.error,
    ohlcQuery.isLoading,
    ohlcQuery.isFetching,
    ofSymbol,
    needsOhlc,
    symbol,
  ]);
}

/** Chart-time index of the canvas bars, for the shared-crosshair lookup. */
export function priceIndexOf(bars: CanvasBar[]): Map<number, number> {
  const map = new Map<number, number>();
  for (const b of bars) map.set(toChartTime(b.time), b.close);
  return map;
}

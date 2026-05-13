"use client";

/**
 * Commodity desk · data-focused live view.
 *
 * Single screen, no tabs. Renders every piece of data the desk produces in
 * a dense layout: instruments table with indicators and bucket, three-list
 * classification, open positions, today's audit feed, data-quality summary.
 * Refreshes in place every few seconds — never blanks while waiting.
 */
import { useMemo } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";

import {
  api as apiClient,
  getCommodityOverview,
  getCommodityPositions,
  getCommodityOrders,
  getCommodityWatchlistSnapshot,
} from "@/lib/api";

const REFRESH_MS = 4_000;

type Bucket = "active" | "ready" | "favourable" | "drifting" | "neutral" | null;
type Trajectory = "improving" | "stalled" | "deteriorating" | null;

type WatchRow = {
  symbol?: string | null;
  underlying?: string | null;
  display_name?: string | null;
  price?: number | null;
  previous_close?: number | null;
  change_pct?: number | null;
  macd?: number | null;
  macd_signal?: number | null;
  macd_histogram?: number | null;
  atr?: number | null;
  regime?: string | null;
  mp_day_type?: string | null;
  mp_status?: string | null;
  bar_time?: string | null;
  signal_validation?: string | null;
  signal_validation_detail?: string | null;
  bucket?: Bucket;
  trajectory?: Trajectory;
  proximity_pct?: number | null;
  bucket_rationale?: string | null;
};

type CommoditySnapshotContract = {
  symbol?: string | null;
  underlying?: string | null;
  lookup_symbol?: string | null;
  active_lookup_symbol?: string | null;
  selected_lookup_symbol?: string | null;
  default_lookup_symbol?: string | null;
  active_expiry?: string | null;
  selected_expiry?: string | null;
  suggested_expiry?: string | null;
  lot_size?: number | null;
  has_options?: boolean | null;
  quote_unit_label?: string | null;
  contract_unit_label?: string | null;
  strategy_title?: string | null;
  selection_policy?: string | null;
  detail?: string | null;
};

type CommodityWatchlistSnapshot = {
  contract_catalog?: {
    contracts?: CommoditySnapshotContract[];
    source?: string | null;
    detail?: string | null;
    timestamp?: string | null;
  };
  atm_watchlist?: {
    rows?: WatchRow[];
    source?: string | null;
    detail?: string | null;
    timestamp?: string | null;
  };
};

type CommodityPosition = {
  position_key?: string;
  symbol?: string;
  live_symbol?: string;
  display_name?: string;
  action?: string;
  qty?: number;
  lots?: number;
  entry_price?: number;
  current_price?: number;
  stop_price?: number;
  target_price?: number;
  unrealized_pnl?: number | null;
  return_pct?: number | null;
  entered_at?: string;
  regime?: string | null;
  strategy_title?: string | null;
  option_type?: string | null;
  strike?: number | null;
  expiry?: string | null;
};

type AuditEvent = {
  created_at?: string;
  event_type?: string;
  severity?: string;
  message?: string;
  symbol?: string;
  underlying?: string;
  payload?: Record<string, unknown>;
};

type Order = {
  time?: string;
  flow?: string;
  symbol?: string;
  action?: string;
  qty?: number;
  reason?: string;
  fill_price?: number;
};

type DataQualitySymbol = {
  symbol: string;
  stale: boolean;
  flagged: boolean;
  freshest_age_seconds: number;
  freshest_source: string;
};

type DataQualitySnap = {
  overall?: string;
  market_state?: string;
  symbol_count?: number;
  stale_count?: number;
  flagged_count?: number;
  symbol_health?: DataQualitySymbol[];
};

const BUCKET_COLOR: Record<string, string> = {
  active: "bg-emerald-500/20 text-emerald-300 border-emerald-500/50",
  ready: "bg-emerald-500/30 text-emerald-200 border-emerald-500/60",
  favourable: "bg-amber-500/20 text-amber-200 border-amber-500/50",
  drifting: "bg-rose-500/20 text-rose-200 border-rose-500/50",
  neutral: "bg-slate-500/20 text-slate-300 border-slate-500/40",
};

function formatINR(n: number | null | undefined, decimals = 0): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return `₹${Number(n).toLocaleString("en-IN", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  })}`;
}

function formatNumber(n: number | null | undefined, decimals = 2): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return Number(n).toLocaleString("en-IN", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

function formatPct(n: number | null | undefined, decimals = 2): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  const sign = n > 0 ? "+" : "";
  return `${sign}${Number(n).toFixed(decimals)}%`;
}

function trajectoryGlyph(t: Trajectory): string {
  if (t === "improving") return "▲";
  if (t === "deteriorating") return "▼";
  if (t === "stalled") return "▬";
  return "·";
}

function trajectoryColor(t: Trajectory): string {
  if (t === "improving") return "text-emerald-400";
  if (t === "deteriorating") return "text-rose-400";
  return "text-slate-400";
}

function relativeAge(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—";
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rs = s % 60;
  if (m < 60) return `${m}m${rs ? ` ${rs}s` : ""}`;
  const h = Math.floor(m / 60);
  const rm = m % 60;
  return `${h}h${rm ? ` ${rm}m` : ""}`;
}

function formatIST(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    return d.toLocaleTimeString("en-IN", {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    });
  } catch {
    return iso;
  }
}

function severityColor(sev: string | undefined): string {
  switch ((sev || "").toLowerCase()) {
    case "error":
      return "text-rose-400";
    case "warning":
      return "text-amber-300";
    case "success":
      return "text-emerald-300";
    case "trade":
      return "text-sky-300";
    default:
      return "text-slate-400";
  }
}

function snapshotContractToWatchRow(contract: CommoditySnapshotContract): WatchRow {
  const symbol =
    contract.selected_lookup_symbol ||
    contract.active_lookup_symbol ||
    contract.lookup_symbol ||
    contract.default_lookup_symbol ||
    contract.symbol ||
    null;
  const expiry =
    contract.selected_expiry ||
    contract.active_expiry ||
    contract.suggested_expiry ||
    null;
  const detail = contract.detail || "Runtime scan rows are empty; showing the saved MCX contract catalog.";
  const unitParts = [contract.contract_unit_label, contract.quote_unit_label].filter(Boolean);
  return {
    symbol,
    underlying: contract.underlying || contract.symbol || null,
    display_name: [contract.underlying || contract.symbol, expiry].filter(Boolean).join(" · "),
    price: null,
    previous_close: null,
    change_pct: null,
    macd: null,
    macd_signal: null,
    macd_histogram: null,
    atr: null,
    regime: contract.selection_policy || "catalog",
    mp_day_type: unitParts.join(" · ") || null,
    mp_status: contract.has_options ? "options mapped" : "futures only",
    bar_time: null,
    signal_validation: contract.has_options ? "catalog_ready" : "catalog_only",
    signal_validation_detail: detail,
    bucket: "neutral",
    trajectory: "stalled",
    proximity_pct: null,
    bucket_rationale: detail,
  };
}

export default function CommodityLivePage() {
  const overviewQuery = useQuery({
    queryKey: ["commodity-live", "overview"],
    queryFn: async () => (await getCommodityOverview()).data,
    refetchInterval: REFRESH_MS,
    refetchIntervalInBackground: true,
  });

  const positionsQuery = useQuery({
    queryKey: ["commodity-live", "positions"],
    queryFn: async () => (await getCommodityPositions()).data,
    refetchInterval: REFRESH_MS,
    refetchIntervalInBackground: true,
  });

  const ordersQuery = useQuery({
    queryKey: ["commodity-live", "orders"],
    queryFn: async () => (await getCommodityOrders(40)).data,
    refetchInterval: REFRESH_MS * 2,
    refetchIntervalInBackground: true,
  });

  const watchlistSnapshotQuery = useQuery({
    queryKey: ["commodity-live", "watchlist-snapshot"],
    queryFn: async () =>
      (await getCommodityWatchlistSnapshot()).data as CommodityWatchlistSnapshot,
    refetchInterval: REFRESH_MS * 3,
    refetchIntervalInBackground: true,
  });

  const auditQuery = useQuery({
    queryKey: ["commodity-live", "audit"],
    queryFn: async () =>
      (
        await apiClient.get(
          "/api/audit/events?market=commodity&limit=30",
        )
      ).data,
    refetchInterval: REFRESH_MS * 2,
    refetchIntervalInBackground: true,
  });

  const dataQualityQuery = useQuery({
    queryKey: ["commodity-live", "data-quality"],
    queryFn: async () =>
      (await apiClient.get("/api/data-quality/snapshot")).data,
    refetchInterval: REFRESH_MS * 2,
    refetchIntervalInBackground: true,
  });

  const status = overviewQuery.data?.status ?? {};
  const summary = status?.summary ?? {};
  const runtimeFuturesWatchlist = useMemo(
    () => (status?.futures_watchlist ?? status?.watchlist ?? []) as WatchRow[],
    [status?.futures_watchlist, status?.watchlist],
  );
  const snapshotFuturesWatchlist = useMemo(
    () =>
      (
        watchlistSnapshotQuery.data?.contract_catalog?.contracts ?? []
      ).map(snapshotContractToWatchRow),
    [watchlistSnapshotQuery.data?.contract_catalog?.contracts],
  );
  const watchlist: WatchRow[] = useMemo(
    () =>
      runtimeFuturesWatchlist.length > 0
        ? runtimeFuturesWatchlist
        : snapshotFuturesWatchlist,
    [runtimeFuturesWatchlist, snapshotFuturesWatchlist],
  );
  const runtimeOptionWatchlist = useMemo(
    () => (status?.option_watchlist ?? []) as WatchRow[],
    [status?.option_watchlist],
  );
  const snapshotOptionWatchlist = useMemo(
    () => (watchlistSnapshotQuery.data?.atm_watchlist?.rows ?? []) as WatchRow[],
    [watchlistSnapshotQuery.data?.atm_watchlist?.rows],
  );
  const optionWatchlist: WatchRow[] = useMemo(
    () =>
      runtimeOptionWatchlist.length > 0
        ? runtimeOptionWatchlist
        : snapshotOptionWatchlist,
    [runtimeOptionWatchlist, snapshotOptionWatchlist],
  );
  const usingSnapshotFutures = runtimeFuturesWatchlist.length === 0 && snapshotFuturesWatchlist.length > 0;
  const usingSnapshotOptions = runtimeOptionWatchlist.length === 0 && snapshotOptionWatchlist.length > 0;

  const positions: CommodityPosition[] = useMemo(
    () => (positionsQuery.data as CommodityPosition[] | undefined) ?? [],
    [positionsQuery.data],
  );

  const orders: Order[] = useMemo(
    () => (ordersQuery.data as Order[] | undefined) ?? [],
    [ordersQuery.data],
  );

  const auditEvents: AuditEvent[] = useMemo(
    () => (auditQuery.data?.events ?? []) as AuditEvent[],
    [auditQuery.data],
  );

  const dataQuality: DataQualitySnap = useMemo(
    () => (dataQualityQuery.data ?? {}) as DataQualitySnap,
    [dataQualityQuery.data],
  );

  const mcxQuality = useMemo(
    () =>
      (dataQuality.symbol_health ?? []).filter((s) =>
        (s.symbol || "").startsWith("MCX:"),
      ),
    [dataQuality.symbol_health],
  );

  const met = watchlist.filter(
    (r) => r.bucket === "active" || r.bucket === "ready",
  );
  const favourable = watchlist.filter((r) => r.bucket === "favourable");
  const drifting = watchlist.filter((r) => r.bucket === "drifting");

  const totalEquity = Number(summary?.total_equity ?? 0);
  const realizedPnl = Number(summary?.realized_pnl ?? 0);
  const dayPnl = Number(summary?.day_pnl ?? 0);
  const totalTrades = Number(summary?.total_trades ?? 0);
  const winRate = Number(summary?.win_rate ?? 0);
  const profitFactor = Number(summary?.profit_factor ?? 0);
  const openPositions = Number(summary?.open_positions ?? 0);
  const initialCapital = Number(summary?.initial_capital ?? 1_000_000);
  const equityPct =
    initialCapital > 0 ? ((totalEquity - initialCapital) / initialCapital) * 100 : 0;
  const killActive = Boolean(status?.kill_switch_active);
  const running = Boolean(status?.running);
  const lastRunAt = status?.last_run_at as string | undefined;

  return (
    <div className="min-h-screen bg-bg-primary px-4 py-3 text-text-primary">
      <header className="mb-3 flex flex-wrap items-baseline justify-between gap-3 border-b border-bg-active/40 pb-2">
        <div className="flex items-baseline gap-3">
          <h1 className="text-base font-semibold tracking-tight">
            Commodity · Live Data View
          </h1>
          <span className="text-xs text-text-muted">
            equity {formatINR(totalEquity)}{" "}
            <span className={equityPct >= 0 ? "text-emerald-400" : "text-rose-400"}>
              ({formatPct(equityPct)})
            </span>
          </span>
          <span className="text-xs text-text-muted">
            realized <span className={realizedPnl >= 0 ? "text-emerald-400" : "text-rose-400"}>{formatINR(realizedPnl)}</span>
            {" · "}
            today <span className={dayPnl >= 0 ? "text-emerald-400" : "text-rose-400"}>{formatINR(dayPnl)}</span>
          </span>
          <span className="text-xs text-text-muted">
            trades {totalTrades} · win {(winRate * 100).toFixed(0)}% · PF{" "}
            {profitFactor ? profitFactor.toFixed(2) : "—"}
          </span>
        </div>
        <div className="flex items-baseline gap-2 text-[11px]">
          <span
            className={`rounded border px-2 py-0.5 ${
              killActive
                ? "border-rose-500/60 bg-rose-500/10 text-rose-300"
                : running
                  ? "border-emerald-500/50 bg-emerald-500/10 text-emerald-300"
                  : "border-amber-500/50 bg-amber-500/10 text-amber-200"
            }`}
          >
            {killActive ? "KILL SWITCH" : running ? "scanning" : "idle"}
          </span>
          <span className="text-text-muted">
            last scan {formatIST(lastRunAt)}
          </span>
          <Link
            href="/settings"
            className="rounded border border-bg-active/60 px-2 py-0.5 text-text-muted hover:text-text-primary"
          >
            settings →
          </Link>
        </div>
      </header>

      <div className="grid grid-cols-12 gap-3">
        {/* Instruments table */}
        <section className="col-span-12 rounded-lg border border-bg-active/40 bg-bg-secondary/20 p-2 xl:col-span-8">
          <div className="mb-1 flex items-baseline justify-between">
            <h2 className="text-[11px] font-semibold uppercase tracking-[0.18em] text-text-muted">
              Instruments · futures ({watchlist.length})
            </h2>
            <span className="text-[11px] text-text-muted">
              {usingSnapshotFutures ? "MCX · contract catalog fallback" : "MCX · 15m MACD + Market Profile"}
            </span>
          </div>
          <table className="w-full text-xs">
            <thead className="text-[10.5px] uppercase tracking-wide text-text-muted">
              <tr>
                <th className="py-1 text-left">Symbol</th>
                <th className="text-right">Price</th>
                <th className="text-right">Δ%</th>
                <th className="text-right">MACD</th>
                <th className="text-right">Hist</th>
                <th className="text-right">ATR</th>
                <th className="text-left">Regime</th>
                <th className="text-left">MP</th>
                <th className="text-left">Bucket</th>
                <th className="text-right">Bar</th>
              </tr>
            </thead>
            <tbody>
              {watchlist.length === 0 ? (
                <tr>
                  <td colSpan={10} className="py-3 text-center text-text-muted">
                    no futures rows
                  </td>
                </tr>
              ) : (
                watchlist.map((row) => (
                  <tr
                    key={row.symbol || row.underlying || "row"}
                    className="border-t border-bg-active/20"
                  >
                    <td className="py-1.5 font-medium">
                      {row.display_name || row.underlying || row.symbol}
                      <div className="text-[10px] text-text-muted">{row.symbol}</div>
                    </td>
                    <td className="text-right font-mono">{formatNumber(row.price, 2)}</td>
                    <td
                      className={`text-right font-mono ${
                        (row.change_pct ?? 0) >= 0 ? "text-emerald-400" : "text-rose-400"
                      }`}
                    >
                      {formatPct(row.change_pct, 2)}
                    </td>
                    <td className="text-right font-mono">{formatNumber(row.macd, 2)}</td>
                    <td
                      className={`text-right font-mono ${
                        (row.macd_histogram ?? 0) >= 0 ? "text-emerald-400" : "text-rose-400"
                      }`}
                    >
                      {formatNumber(row.macd_histogram, 2)}
                    </td>
                    <td className="text-right font-mono text-text-muted">
                      {formatNumber(row.atr, 1)}
                    </td>
                    <td className="text-text-muted">{row.regime || "—"}</td>
                    <td className="text-text-muted">{row.mp_day_type || "—"}</td>
                    <td>
                      <span
                        className={`rounded border px-1.5 py-0.5 text-[10px] ${
                          BUCKET_COLOR[row.bucket || "neutral"] || BUCKET_COLOR.neutral
                        }`}
                      >
                        {row.bucket || "—"}
                      </span>{" "}
                      <span className={trajectoryColor(row.trajectory ?? null)}>
                        {trajectoryGlyph(row.trajectory ?? null)}
                      </span>{" "}
                      <span className="text-[10px] text-text-muted">
                        {row.proximity_pct != null ? `${Math.round(row.proximity_pct)}%` : ""}
                      </span>
                      {row.signal_validation ? (
                        <div className="mt-0.5 text-[10px] text-text-muted">
                          {row.signal_validation}
                        </div>
                      ) : null}
                    </td>
                    <td className="text-right text-[10px] text-text-muted">
                      {formatIST(row.bar_time)}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
          {optionWatchlist.length > 0 ? (
            <div className="mt-3">
              <h2 className="mb-1 text-[11px] font-semibold uppercase tracking-[0.18em] text-text-muted">
                Options · ATM contracts ({optionWatchlist.length})
                {usingSnapshotOptions ? (
                  <span className="ml-2 normal-case tracking-normal text-text-muted">
                    · snapshot
                  </span>
                ) : null}
              </h2>
              <table className="w-full text-xs">
                <thead className="text-[10.5px] uppercase tracking-wide text-text-muted">
                  <tr>
                    <th className="py-1 text-left">Symbol</th>
                    <th className="text-left">CE</th>
                    <th className="text-right">CE LTP</th>
                    <th className="text-right">CE MACD</th>
                    <th className="text-left">PE</th>
                    <th className="text-right">PE LTP</th>
                    <th className="text-right">PE MACD</th>
                    <th className="text-left">Bucket</th>
                  </tr>
                </thead>
                <tbody>
                  {optionWatchlist.map((row) => {
                    const ce = (row as Record<string, unknown>).ce as
                      | Record<string, unknown>
                      | undefined;
                    const pe = (row as Record<string, unknown>).pe as
                      | Record<string, unknown>
                      | undefined;
                    return (
                      <tr
                        key={row.symbol || row.underlying || "row-opt"}
                        className="border-t border-bg-active/20"
                      >
                        <td className="py-1.5 font-medium">{row.underlying || row.symbol}</td>
                        <td className="font-mono text-[10.5px]">
                          {String((ce?.strike ?? "—") as number)}
                        </td>
                        <td className="text-right font-mono">{formatNumber(ce?.ltp as number, 2)}</td>
                        <td className="text-right font-mono">{formatNumber(ce?.macd as number, 2)}</td>
                        <td className="font-mono text-[10.5px]">
                          {String((pe?.strike ?? "—") as number)}
                        </td>
                        <td className="text-right font-mono">{formatNumber(pe?.ltp as number, 2)}</td>
                        <td className="text-right font-mono">{formatNumber(pe?.macd as number, 2)}</td>
                        <td>
                          <span
                            className={`rounded border px-1.5 py-0.5 text-[10px] ${
                              BUCKET_COLOR[row.bucket || "neutral"] || BUCKET_COLOR.neutral
                            }`}
                          >
                            {row.bucket || "—"}
                          </span>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          ) : null}
        </section>

        {/* Three-list classification */}
        <section className="col-span-12 rounded-lg border border-bg-active/40 bg-bg-secondary/20 p-2 xl:col-span-4">
          <h2 className="mb-1 text-[11px] font-semibold uppercase tracking-[0.18em] text-text-muted">
            Three-list view
          </h2>
          <div className="grid grid-cols-3 gap-2 text-xs">
            {[
              { label: "Met / Traded", rows: met, tone: "emerald" },
              { label: "Favourable", rows: favourable, tone: "amber" },
              { label: "Drifting", rows: drifting, tone: "rose" },
            ].map(({ label, rows, tone }) => (
              <div
                key={label}
                className={`rounded border px-2 py-1.5 ${
                  tone === "emerald"
                    ? "border-emerald-500/40 bg-emerald-500/5"
                    : tone === "amber"
                      ? "border-amber-500/40 bg-amber-500/5"
                      : "border-rose-500/40 bg-rose-500/5"
                }`}
              >
                <div className="flex items-baseline justify-between">
                  <span className="text-[10.5px] font-semibold uppercase tracking-wide">
                    {label}
                  </span>
                  <span className="text-[10px] text-text-muted">{rows.length}</span>
                </div>
                <ul className="mt-1 flex flex-col gap-0.5">
                  {rows.length === 0 ? (
                    <li className="italic text-text-muted">none</li>
                  ) : (
                    rows.map((r) => (
                      <li key={r.symbol} className="truncate">
                        <span className="font-medium">{r.underlying || r.symbol}</span>
                        <span className="text-[10px] text-text-muted">
                          {" · "}
                          {r.proximity_pct != null
                            ? `${Math.round(r.proximity_pct)}%`
                            : ""}
                        </span>
                      </li>
                    ))
                  )}
                </ul>
              </div>
            ))}
          </div>
        </section>

        {/* Open positions */}
        <section className="col-span-12 rounded-lg border border-bg-active/40 bg-bg-secondary/20 p-2 xl:col-span-6">
          <h2 className="mb-1 text-[11px] font-semibold uppercase tracking-[0.18em] text-text-muted">
            Open positions ({positions.length})
          </h2>
          {positions.length === 0 ? (
            <div className="px-2 py-3 text-xs text-text-muted">no open positions</div>
          ) : (
            <table className="w-full text-xs">
              <thead className="text-[10.5px] uppercase tracking-wide text-text-muted">
                <tr>
                  <th className="text-left">Sym</th>
                  <th className="text-left">Side</th>
                  <th className="text-right">Qty</th>
                  <th className="text-right">Entry</th>
                  <th className="text-right">Now</th>
                  <th className="text-right">Stop</th>
                  <th className="text-right">Tgt</th>
                  <th className="text-right">Unrl P&L</th>
                  <th className="text-right">Ret%</th>
                  <th className="text-left">Age</th>
                </tr>
              </thead>
              <tbody>
                {positions.map((p) => {
                  const ageSec = p.entered_at
                    ? (Date.now() - new Date(p.entered_at).getTime()) / 1000
                    : null;
                  return (
                    <tr
                      key={p.position_key || p.live_symbol}
                      className="border-t border-bg-active/20"
                    >
                      <td className="py-1 font-medium">
                        {p.display_name || p.symbol}
                        <div className="text-[10px] text-text-muted">{p.live_symbol}</div>
                      </td>
                      <td>{p.action}</td>
                      <td className="text-right font-mono">{p.qty}</td>
                      <td className="text-right font-mono">{formatNumber(p.entry_price, 2)}</td>
                      <td className="text-right font-mono">{formatNumber(p.current_price, 2)}</td>
                      <td className="text-right font-mono text-rose-300">{formatNumber(p.stop_price, 2)}</td>
                      <td className="text-right font-mono text-emerald-300">{formatNumber(p.target_price, 2)}</td>
                      <td
                        className={`text-right font-mono ${
                          (p.unrealized_pnl ?? 0) >= 0 ? "text-emerald-400" : "text-rose-400"
                        }`}
                      >
                        {formatINR(p.unrealized_pnl, 0)}
                      </td>
                      <td
                        className={`text-right font-mono ${
                          (p.return_pct ?? 0) >= 0 ? "text-emerald-400" : "text-rose-400"
                        }`}
                      >
                        {formatPct(p.return_pct, 1)}
                      </td>
                      <td className="text-[10.5px] text-text-muted">{relativeAge(ageSec)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </section>

        {/* Data quality */}
        <section className="col-span-12 rounded-lg border border-bg-active/40 bg-bg-secondary/20 p-2 xl:col-span-6">
          <div className="mb-1 flex items-baseline justify-between">
            <h2 className="text-[11px] font-semibold uppercase tracking-[0.18em] text-text-muted">
              Data quality · MCX feeds
            </h2>
            <span className="text-[10.5px] text-text-muted">
              overall{" "}
              <span
                className={
                  dataQuality.overall === "healthy"
                    ? "text-emerald-400"
                    : dataQuality.overall === "degraded"
                      ? "text-amber-300"
                      : dataQuality.overall === "critical"
                        ? "text-rose-400"
                        : "text-slate-400"
                }
              >
                {dataQuality.overall || "—"}
              </span>
              {" · "}stale {dataQuality.stale_count ?? 0}/{dataQuality.symbol_count ?? 0}
            </span>
          </div>
          <table className="w-full text-xs">
            <thead className="text-[10.5px] uppercase tracking-wide text-text-muted">
              <tr>
                <th className="text-left">Symbol</th>
                <th className="text-left">Source</th>
                <th className="text-right">Age</th>
                <th className="text-left">State</th>
              </tr>
            </thead>
            <tbody>
              {mcxQuality.length === 0 ? (
                <tr>
                  <td colSpan={4} className="py-2 text-center text-text-muted">
                    no MCX entries yet
                  </td>
                </tr>
              ) : (
                mcxQuality.map((s) => (
                  <tr key={s.symbol} className="border-t border-bg-active/20">
                    <td className="py-1 font-medium">{s.symbol}</td>
                    <td className="text-text-muted">{s.freshest_source}</td>
                    <td className="text-right font-mono">{relativeAge(s.freshest_age_seconds)}</td>
                    <td>
                      <span
                        className={
                          s.flagged
                            ? "text-rose-400"
                            : s.stale
                              ? "text-amber-300"
                              : "text-emerald-400"
                        }
                      >
                        {s.flagged ? "flagged" : s.stale ? "stale" : "fresh"}
                      </span>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </section>

        {/* Today's audit feed */}
        <section className="col-span-12 rounded-lg border border-bg-active/40 bg-bg-secondary/20 p-2">
          <div className="mb-1 flex items-baseline justify-between">
            <h2 className="text-[11px] font-semibold uppercase tracking-[0.18em] text-text-muted">
              Audit feed · last {auditEvents.length}
            </h2>
            <span className="text-[10.5px] text-text-muted">
              /api/audit/events?market=commodity
            </span>
          </div>
          {auditEvents.length === 0 ? (
            <div className="px-2 py-3 text-xs text-text-muted">no events yet</div>
          ) : (
            <ul className="space-y-0.5 text-[11.5px]">
              {auditEvents.map((e, idx) => (
                <li
                  key={`${e.created_at}-${idx}`}
                  className="flex items-baseline gap-2 border-b border-bg-active/15 py-1"
                >
                  <span className="w-[60px] font-mono text-[10.5px] text-text-muted">
                    {formatIST(e.created_at)}
                  </span>
                  <span className={`w-[80px] text-[10.5px] uppercase ${severityColor(e.severity)}`}>
                    {e.event_type || "—"}
                  </span>
                  {e.symbol ? (
                    <span className="w-[140px] truncate font-mono text-[10.5px] text-text-muted">
                      {e.symbol}
                    </span>
                  ) : (
                    <span className="w-[140px] text-text-muted">
                      {e.underlying || "—"}
                    </span>
                  )}
                  <span className="flex-1 truncate text-text-secondary">
                    {e.message || JSON.stringify(e.payload || {})}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </section>

        {/* Recent orders */}
        <section className="col-span-12 rounded-lg border border-bg-active/40 bg-bg-secondary/20 p-2">
          <div className="mb-1 flex items-baseline justify-between">
            <h2 className="text-[11px] font-semibold uppercase tracking-[0.18em] text-text-muted">
              Recent orders · last {orders.length}
            </h2>
            <span className="text-[10.5px] text-text-muted">
              entry + exit flow on the desk
            </span>
          </div>
          {orders.length === 0 ? (
            <div className="px-2 py-3 text-xs text-text-muted">no orders yet</div>
          ) : (
            <table className="w-full text-xs">
              <thead className="text-[10.5px] uppercase tracking-wide text-text-muted">
                <tr>
                  <th className="text-left">Time</th>
                  <th className="text-left">Flow</th>
                  <th className="text-left">Symbol</th>
                  <th className="text-left">Side</th>
                  <th className="text-right">Qty</th>
                  <th className="text-right">Fill</th>
                  <th className="text-left">Reason</th>
                </tr>
              </thead>
              <tbody>
                {orders.slice(0, 14).map((o, idx) => (
                  <tr
                    key={`${o.time}-${idx}`}
                    className="border-t border-bg-active/20"
                  >
                    <td className="py-1 font-mono text-[10.5px] text-text-muted">{formatIST(o.time)}</td>
                    <td>
                      <span
                        className={
                          o.flow === "entry"
                            ? "text-emerald-300"
                            : o.flow === "exit"
                              ? "text-rose-300"
                              : "text-text-muted"
                        }
                      >
                        {o.flow || "—"}
                      </span>
                    </td>
                    <td className="font-mono text-[10.5px]">{o.symbol}</td>
                    <td>{o.action}</td>
                    <td className="text-right font-mono">{o.qty}</td>
                    <td className="text-right font-mono">{formatNumber(o.fill_price, 2)}</td>
                    <td className="truncate text-[10.5px] text-text-muted">{o.reason || ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      </div>
    </div>
  );
}

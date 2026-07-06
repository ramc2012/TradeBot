"use client";

/**
 * Sniper desk — native v2.
 *
 * The Sniper is a trained LightGBM excursion estimator (~123 MP / HTF /
 * auction-state / VWAP / order-flow / option-chain features) that runs in
 * an ISOLATED sidecar container, reads 1-min bars from TimescaleDB, and
 * POSTs a reduced per-underlying directional call to the backend. The
 * backend caches it in-process and exposes it at
 *   GET /api/auction-intelligence/sniper-signal   -> { signals: { [SYM]: SniperSignal } }
 * (the scorer's per-horizon accuracy / IC live in the sidecar's
 *  sniper_metrics.json dashboard, not behind an HTTP endpoint — so there is
 *  no paper-position lane here and no native Performance tab.)
 *
 * Tabs:
 *   board     → live signal cards + KPI strip + excursion ladder
 *   quadrant  → direction × confidence conviction map (bespoke SVG)
 *   history   → rolling client-side capture of fired signals
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Activity, Crosshair, Gauge, History as HistoryIcon, Radar, Target } from "lucide-react";

import {
  DeskShell,
  MetricTile,
  REFRESH_MS,
  Section,
  StatusBadge,
  formatIST,
  formatNumber,
  formatTimestamp,
  tone,
  useUrlTab,
} from "@/components/desk-ui";
import { api as apiClient } from "@/lib/api";
import { SignalQualityTab } from "@/components/strategies/overview/SignalQualityTab";
import { StrategyLiveStream } from "@/components/strategies/shared/StrategyLiveStream";

import { SniperQuadrant } from "./SniperQuadrant";
import { MagnitudeLadder } from "./MagnitudeLadder";
import type { SniperRow, SniperSignalsResponse } from "./types";

const TABS = [
  { key: "board", label: "Signal board", icon: Crosshair },
  { key: "quadrant", label: "Conviction map", icon: Radar },
  { key: "history", label: "History", icon: HistoryIcon },
  { key: "signal-quality", label: "Signal quality", icon: Activity },
  { key: "live-stream", label: "Live stream", icon: Activity },
];

const STALE_SEC = 1800; // 30m — sidecar fires every 30m during market hours
const SNIPER_WATCHLIST = ["NIFTY", "BANKNIFTY", "SENSEX"];

type HistoryEntry = SniperRow & { captured_at: number; key: string };

function dirVariant(d?: string): "success" | "error" | "neutral" {
  const s = String(d || "").toUpperCase();
  if (s === "LONG" || s === "UP" || s === "BULLISH") return "success";
  if (s === "SHORT" || s === "DOWN" || s === "BEARISH") return "error";
  return "neutral";
}
function dirLabel(d?: string): string {
  const s = String(d || "").toUpperCase();
  if (s === "LONG") return "UP";
  if (s === "SHORT") return "DOWN";
  if (s === "FLAT" || !s) return "FLAT";
  return s;
}
function ageLabel(sec?: number | null): string {
  if (sec == null || !Number.isFinite(sec)) return "—";
  if (sec < 90) return `${Math.round(sec)}s`;
  if (sec < 5400) return `${Math.round(sec / 60)}m`;
  return `${(sec / 3600).toFixed(1)}h`;
}

export default function SniperDesk() {
  const [activeTab, setActiveTab] = useUrlTab("board");

  const summaryQuery = useQuery({
    queryKey: ["sniper", "ai-summary"],
    queryFn: async () => (await apiClient.get("/api/auction-intelligence/summary")).data as { description?: string },
    refetchInterval: REFRESH_MS.summary,
    refetchOnWindowFocus: false,
  });

  const signalsQuery = useQuery({
    queryKey: ["sniper", "signals"],
    queryFn: async () => (await apiClient.get("/api/auction-intelligence/sniper-signal")).data as SniperSignalsResponse,
    refetchInterval: REFRESH_MS.snapshot,
    refetchOnWindowFocus: false,
  });

  const rows = useMemo<SniperRow[]>(() => {
    const signals = signalsQuery.data?.signals || {};
    return Object.entries(signals)
      .map(([sym, sig]) => ({ ...sig, symbol: sig?.symbol || sym }))
      .sort((a, b) => (b.confidence || 0) - (a.confidence || 0));
  }, [signalsQuery.data]);

  // No scorer endpoint → accumulate a rolling client-side history keyed by
  // (symbol, decision_time) so a fresh fire is captured once.
  const [history, setHistory] = useState<HistoryEntry[]>([]);
  const seen = useRef<Set<string>>(new Set());
  useEffect(() => {
    if (!rows.length) return;
    setHistory((prev) => {
      let next = prev;
      for (const r of rows) {
        const key = `${r.symbol}|${r.decision_time || r.received_at || ""}|${r.horizon}`;
        if (seen.current.has(key)) continue;
        seen.current.add(key);
        next = [{ ...r, captured_at: Date.now(), key }, ...next];
      }
      return next === prev ? prev : next.slice(0, 200);
    });
  }, [rows]);

  const asOf = rows.length
    ? rows.reduce<string | undefined>((acc, r) => {
        const t = r.received_at || r.decision_time || undefined;
        return !acc || (t && t > acc) ? t || acc : acc;
      }, undefined)
    : undefined;

  // KPI rollups.
  const fresh = rows.filter((r) => (r.age_sec ?? 0) <= STALE_SEC);
  const longs = rows.filter((r) => dirLabel(r.direction) === "UP").length;
  const shorts = rows.filter((r) => dirLabel(r.direction) === "DOWN").length;
  const flats = rows.filter((r) => dirLabel(r.direction) === "FLAT").length;
  const top = rows[0];
  const avgConf = rows.length ? rows.reduce((s, r) => s + (r.confidence || 0), 0) / rows.length : 0;
  const modelName = rows.find((r) => r.model)?.model;

  return (
    <DeskShell
      title="Sniper Excursion Estimator"
      description={
        "LightGBM excursion-estimator alpha — an isolated sidecar rebuilds the full ~123-feature vector off TimescaleDB and posts a reduced directional call per underlying. Shadow / overlay only."
      }
      asOf={asOf}
      isFetching={signalsQuery.isFetching}
      isLive={fresh.length > 0}
      paperMode
      tabs={TABS}
      activeTab={activeTab}
      onTabChange={setActiveTab}
      rightSlot={
        <div className="flex items-center gap-2">
          <StatusBadge
            label={fresh.length ? `${fresh.length} live` : "idle"}
            variant={fresh.length ? "success" : "neutral"}
          />
          {modelName ? (
            <StatusBadge label={modelName.replace(/\.(joblib|pkl)$/i, "")} variant="info" />
          ) : null}
        </div>
      }
    >
      {activeTab === "board" ? (
        <div className="space-y-4">
          <section className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
            <MetricTile label="Live signals" value={String(rows.length)} detail={`${fresh.length} fresh · ${rows.length - fresh.length} stale`} />
            <MetricTile label="Long / Short" value={`${longs} / ${shorts}`} detail={`${flats} flat`} color={longs >= shorts ? "text-accent-green" : "text-accent-red"} />
            <MetricTile label="Top conviction" value={top ? top.symbol : "—"} detail={top ? `${dirLabel(top.direction)} · conf ${formatNumber(top.confidence, 2)}` : ""} color={top ? tone(dirLabel(top.direction) === "UP" ? 1 : dirLabel(top.direction) === "DOWN" ? -1 : 0) : undefined} />
            <MetricTile label="Top magnitude" value={top ? `${formatNumber(Math.abs(top.magnitude_atr), 2)} ATR` : "—"} detail={top ? `horizon ${top.horizon || "—"}` : ""} />
            <MetricTile label="Avg confidence" value={formatNumber(avgConf, 2)} detail="across live calls" />
            <MetricTile label="Freshest" value={ageLabel(rows.length ? Math.min(...rows.map((r) => r.age_sec ?? Infinity)) : null)} detail="since last fire" />
          </section>

          <Section
            title="Live sniper signals"
            icon={<Crosshair size={16} />}
            description="One reduced directional call per underlying — direction, magnitude (favorable excursion in ATR units), confidence and horizon."
            rightSlot={signalsQuery.isError ? <StatusBadge label="feed error" variant="error" /> : null}
          >
            {rows.length ? (
              <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
                {rows.map((r) => (
                  <SignalCard key={r.symbol} row={r} />
                ))}
              </div>
            ) : (
              <EmptyBoard loading={signalsQuery.isLoading} />
            )}
          </Section>

          <Section title="Excursion ladder" icon={<Target size={16} />} description="Predicted up vs down excursion (ATR) per underlying; the called side is highlighted.">
            <MagnitudeLadder rows={rows} />
          </Section>
        </div>
      ) : null}

      {activeTab === "quadrant" ? (
        <div className="space-y-4">
          <Section
            title="Conviction map"
            icon={<Radar size={16} />}
            description="Each underlying plotted by signed conviction (confidence × direction) against magnitude. Right half = long, left half = short, centre band = flat. Bubble size scales with magnitude."
          >
            <SniperQuadrant rows={rows} />
          </Section>
          <Section title="Ranked calls" icon={<Gauge size={16} />}>
            <SignalTable rows={rows} />
          </Section>
        </div>
      ) : null}

      {activeTab === "history" ? <HistoryPanel history={history} /> : null}
      {activeTab === "signal-quality" ? (
        <SignalQualityTab laneKeys={["auction_intelligence"]} title="Sniper input validation" />
      ) : null}
      {activeTab === "live-stream" ? (
        <StrategyLiveStream
          title="Sniper"
          watchlist={(rows.length ? rows.map((row) => row.symbol) : SNIPER_WATCHLIST).map((symbol) => ({ symbol }))}
          positionSources={["sniper"]}
        />
      ) : null}
    </DeskShell>
  );
}

function SignalCard({ row }: { row: SniperRow }) {
  const dir = dirLabel(row.direction);
  const variant = dirVariant(row.direction);
  const conf = Math.max(0, Math.min(1, row.confidence || 0));
  const mag = Math.abs(row.magnitude_atr || 0);
  const stale = (row.age_sec ?? 0) > STALE_SEC;
  const barColor =
    variant === "success" ? "rgb(var(--accent-green))" : variant === "error" ? "rgb(var(--accent-red))" : "rgb(var(--accent-amber))";
  const extras = (row.extras || {}) as Record<string, unknown>;
  const hasOpt = extras.has_options === true;
  const hasOf = extras.has_live_of === true;

  return (
    <div className={`rounded-2xl border bg-bg-secondary/24 p-3.5 ${stale ? "border-bg-border opacity-70" : "border-bg-border"}`}>
      <div className="flex items-start justify-between">
        <div>
          <div className="text-sm font-semibold text-text-primary">{row.symbol}</div>
          <div className="mt-0.5 text-[11px] text-text-muted">
            horizon {row.horizon || "—"} · {stale ? "stale" : "fresh"} · {ageLabel(row.age_sec)}
          </div>
        </div>
        <StatusBadge label={dir} variant={variant} />
      </div>

      <div className="mt-3 grid grid-cols-2 gap-2 text-center">
        <div className="rounded-lg border border-bg-border bg-bg-primary/15 px-2 py-1.5">
          <div className="text-[9.5px] uppercase tracking-[0.12em] text-text-muted">Magnitude</div>
          <div className="font-mono text-text-primary">{formatNumber(mag, 2)}<span className="text-[10px] text-text-muted"> ATR</span></div>
        </div>
        <div className="rounded-lg border border-bg-border bg-bg-primary/15 px-2 py-1.5">
          <div className="text-[9.5px] uppercase tracking-[0.12em] text-text-muted">Confidence</div>
          <div className="font-mono text-text-primary">{formatNumber(conf, 2)}</div>
        </div>
      </div>

      {/* confidence meter */}
      <div className="mt-3">
        <div className="flex items-center justify-between text-[10px] text-text-muted">
          <span>conviction</span>
          <span className="font-mono">{Math.round(conf * 100)}%</span>
        </div>
        <div className="mt-1 h-2 overflow-hidden rounded-full bg-bg-primary/40">
          <div className="h-full rounded-full" style={{ width: `${conf * 100}%`, background: barColor }} />
        </div>
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-1.5">
        {row.up_atr != null ? <span className="rounded bg-accent-green/10 px-1.5 py-0.5 text-[10px] text-accent-green">▲ {formatNumber(Math.abs(Number(row.up_atr)), 2)}</span> : null}
        {row.down_atr != null ? <span className="rounded bg-accent-red/10 px-1.5 py-0.5 text-[10px] text-accent-red">▼ {formatNumber(Math.abs(Number(row.down_atr)), 2)}</span> : null}
        {hasOpt ? <StatusBadge label="opt ✓" variant="info" /> : null}
        {hasOf ? <StatusBadge label="OF ✓" variant="info" /> : null}
      </div>

      <div className="mt-2 text-[10px] text-text-muted">
        decided {row.decision_time ? formatIST(row.decision_time) : "—"}
      </div>
    </div>
  );
}

function SignalTable({ rows }: { rows: SniperRow[] }) {
  return (
    <div className="-mx-2 overflow-x-auto">
      <table className="w-full border-collapse">
        <thead>
          <tr className="border-b border-bg-border/60">
            {["Symbol", "Dir", "Mag (ATR)", "Conf", "Up", "Down", "Horizon", "Age"].map((h, i) => (
              <th key={h} className={`px-2.5 py-1.5 text-[10px] uppercase tracking-[0.12em] text-text-muted font-semibold ${i === 0 ? "text-left" : "text-right"}`}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.length ? rows.map((r) => (
            <tr key={r.symbol} className="border-b border-bg-border/25 hover:bg-bg-primary/20">
              <td className="px-2.5 py-1.5 text-left text-[12px] font-mono text-text-primary">{r.symbol}</td>
              <td className="px-2.5 py-1.5 text-right"><StatusBadge label={dirLabel(r.direction)} variant={dirVariant(r.direction)} /></td>
              <td className="px-2.5 py-1.5 text-right text-[12px] font-mono text-text-secondary">{formatNumber(Math.abs(r.magnitude_atr || 0), 2)}</td>
              <td className="px-2.5 py-1.5 text-right text-[12px] font-mono text-text-secondary">{formatNumber(r.confidence, 2)}</td>
              <td className="px-2.5 py-1.5 text-right text-[12px] font-mono text-accent-green">{r.up_atr != null ? formatNumber(Math.abs(Number(r.up_atr)), 2) : "—"}</td>
              <td className="px-2.5 py-1.5 text-right text-[12px] font-mono text-accent-red">{r.down_atr != null ? formatNumber(Math.abs(Number(r.down_atr)), 2) : "—"}</td>
              <td className="px-2.5 py-1.5 text-right text-[12px] font-mono text-text-secondary">{r.horizon || "—"}</td>
              <td className="px-2.5 py-1.5 text-right text-[12px] font-mono text-text-muted">{ageLabel(r.age_sec)}</td>
            </tr>
          )) : (
            <tr><td colSpan={8} className="px-2.5 py-6 text-center text-sm text-text-muted">No live signals</td></tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

function HistoryPanel({ history }: { history: HistoryEntry[] }) {
  return (
    <div className="space-y-4">
      <Section
        title="Signal history"
        icon={<HistoryIcon size={16} />}
        description="Rolling capture of distinct sniper fires observed this session (keyed by symbol + decision time). The scorer's realized per-horizon accuracy / IC lives in the sidecar's metrics dashboard."
        rightSlot={<StatusBadge label={`${history.length} captured`} variant="neutral" />}
      >
        <div className="-mx-2 overflow-x-auto">
          <table className="w-full border-collapse">
            <thead>
              <tr className="border-b border-bg-border/60">
                {["Captured", "Symbol", "Dir", "Mag (ATR)", "Conf", "Horizon", "Decided"].map((h, i) => (
                  <th key={h} className={`px-2.5 py-1.5 text-[10px] uppercase tracking-[0.12em] text-text-muted font-semibold ${i === 1 || i === 0 ? "text-left" : "text-right"}`}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {history.length ? history.map((h) => (
                <tr key={h.key} className="border-b border-bg-border/25 hover:bg-bg-primary/20">
                  <td className="px-2.5 py-1.5 text-left text-[12px] font-mono text-text-muted whitespace-nowrap">{formatTimestamp(h.captured_at)}</td>
                  <td className="px-2.5 py-1.5 text-left text-[12px] font-mono text-text-primary">{h.symbol}</td>
                  <td className="px-2.5 py-1.5 text-right"><StatusBadge label={dirLabel(h.direction)} variant={dirVariant(h.direction)} /></td>
                  <td className="px-2.5 py-1.5 text-right text-[12px] font-mono text-text-secondary">{formatNumber(Math.abs(h.magnitude_atr || 0), 2)}</td>
                  <td className="px-2.5 py-1.5 text-right text-[12px] font-mono text-text-secondary">{formatNumber(h.confidence, 2)}</td>
                  <td className="px-2.5 py-1.5 text-right text-[12px] font-mono text-text-secondary">{h.horizon || "—"}</td>
                  <td className="px-2.5 py-1.5 text-right text-[12px] font-mono text-text-muted whitespace-nowrap">{h.decision_time ? formatIST(h.decision_time) : "—"}</td>
                </tr>
              )) : (
                <tr><td colSpan={7} className="px-2.5 py-8 text-center text-sm text-text-muted">No fires captured yet this session — signals appear here as the sidecar posts them.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </Section>
    </div>
  );
}

function EmptyBoard({ loading }: { loading: boolean }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 rounded-xl border border-dashed border-bg-border/60 py-12 text-center">
      <Crosshair size={22} className="text-text-muted" />
      <div className="text-sm text-text-secondary">{loading ? "Loading sniper feed…" : "No sniper signals cached"}</div>
      <div className="max-w-md text-[11.5px] text-text-muted">
        The isolated sidecar posts predictions every 30 minutes during market hours (09:30–15:30 IST).
        The in-process cache is empty when the market is closed or the backend was recently recreated.
      </div>
    </div>
  );
}

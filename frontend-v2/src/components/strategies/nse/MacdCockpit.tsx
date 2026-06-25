"use client";

/**
 * MACD desk · Split Cockpit — master/detail.
 *
 * Left: the open book (live positions) + the ATM MACD watchlist, both as
 * clickable legs. Right: the selected leg's 30m premium study (Bollinger +
 * KAMA + MACD + RSI via OptionStudyChart) plus entry/mark/stop/RSI tiles and
 * the signal·phase line. Reuses the live status + open-signals data the desk
 * already has, and the /api/charts/option-ohlc endpoint for the study panes.
 */
import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { clsx } from "clsx";

import { REFRESH_MS, Section, StatusBadge, formatIST, formatNumber, formatSignedMoney, tone } from "@/components/desk-ui";
import { CHART } from "@/components/strategies/shared";
import { OptionStudyChart, type StudyBar, type StudyLine } from "@/components/strategies/nse/OptionStudyChart";
import { type OptionChartContract } from "@/components/strategies/nse/OptionChartModal";
import { getOptionOHLC } from "@/lib/api";

type CockpitPosition = {
  underlying?: string | null; option_type?: string | null; strike?: number | null; expiry?: string | null;
  qty?: number | null; entry_price?: number | null; current_price?: number | null;
  unrealized_pnl?: number | null; return_pct?: number | null; phase?: string | null;
  trailing_stop?: number | null; latest_rsi?: number | null; signal_reason?: string | null;
};
type CockpitWatch = {
  underlying?: string | null; direction?: string | null; strike?: number | null; atm_strike?: number | null;
  expiry?: string | null; ltp?: number | null; rsi?: number | null; macd?: number | null;
  previous_macd?: number | null; status?: string | null; instrument_key?: string | null;
};

type Selected = {
  key: string;
  contract: OptionChartContract;
  pos?: CockpitPosition;
  watch?: CockpitWatch;
};

const sideOf = (d?: string | null) => (String(d || "").toUpperCase() === "PE" ? "PE" : "CE");

function posToSelected(p: CockpitPosition): Selected | null {
  const strike = p.strike;
  const expiry = (p.expiry || "").slice(0, 10);
  const side = sideOf(p.option_type);
  if (strike == null || !expiry) return null;
  return {
    key: `pos-${p.underlying}-${side}-${strike}`,
    contract: { underlying: p.underlying || "", direction: side, strike, expiry, ltp: p.current_price ?? null },
    pos: p,
  };
}
function watchToSelected(w: CockpitWatch): Selected | null {
  const strike = w.strike ?? w.atm_strike;
  const expiry = (w.expiry || "").slice(0, 10);
  const side = sideOf(w.direction);
  if (strike == null || !expiry) return null;
  return {
    key: `watch-${w.underlying}-${side}-${strike}`,
    contract: { underlying: w.underlying || "", direction: side, strike, expiry, instrumentKey: w.instrument_key ?? null, ltp: w.ltp ?? null },
    watch: w,
  };
}

export function MacdCockpit({ positions, watchlist }: { positions: CockpitPosition[]; watchlist: CockpitWatch[] }) {
  const posLegs = useMemo(() => positions.map(posToSelected).filter((x): x is Selected => x !== null), [positions]);
  const watchLegs = useMemo(() => watchlist.map(watchToSelected).filter((x): x is Selected => x !== null), [watchlist]);

  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const selected = useMemo(() => {
    const all = [...posLegs, ...watchLegs];
    return all.find((l) => l.key === selectedKey) ?? posLegs[0] ?? watchLegs[0] ?? null;
  }, [selectedKey, posLegs, watchLegs]);

  // Default-select the first available leg once data lands.
  useEffect(() => {
    if (!selectedKey && (posLegs[0] || watchLegs[0])) setSelectedKey((posLegs[0] ?? watchLegs[0]).key);
  }, [selectedKey, posLegs, watchLegs]);

  return (
    <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(0,1.15fr)]">
      {/* ── Left: open book + watchlist ── */}
      <div className="space-y-4">
        <Section title="Open book · click a leg" rightSlot={<span className="text-[11px] text-text-muted">{posLegs.length} open</span>}>
          {posLegs.length ? (
            <div className="-mx-2 overflow-x-auto">
              <table className="w-full border-collapse text-left">
                <thead>
                  <tr className="border-b border-bg-border/60 text-[10px] uppercase tracking-[0.12em] text-text-muted">
                    <th className="px-2.5 py-1.5 text-left">Leg</th>
                    <th className="px-2.5 py-1.5 text-right">Mark</th>
                    <th className="px-2.5 py-1.5 text-right">P&L</th>
                    <th className="px-2.5 py-1.5 text-right">RSI</th>
                  </tr>
                </thead>
                <tbody>
                  {posLegs.map((l) => {
                    const p = l.pos!;
                    const active = selected?.key === l.key;
                    return (
                      <tr key={l.key} onClick={() => setSelectedKey(l.key)} className={clsx("cursor-pointer border-b border-bg-border/25 transition-colors", active ? "bg-accent-blue/10" : "hover:bg-bg-primary/30")}>
                        <td className="px-2.5 py-2">
                          <div className="flex items-center gap-2"><SideBadge side={l.contract.direction} /><span className="font-semibold text-text-primary">{l.contract.underlying}</span></div>
                          <div className="font-mono text-[10px] text-text-muted">{l.contract.direction} {formatNumber(l.contract.strike, 0)}</div>
                        </td>
                        <td className="px-2.5 py-2 text-right font-mono text-text-primary">{formatNumber(p.current_price, 2)}</td>
                        <td className={clsx("px-2.5 py-2 text-right font-mono font-semibold", tone(p.unrealized_pnl))}>{formatSignedMoney(p.unrealized_pnl)}<div className="text-[10px] font-normal text-text-muted">{p.return_pct != null ? `${p.return_pct > 0 ? "+" : ""}${formatNumber(p.return_pct, 1)}%` : ""}</div></td>
                        <td className="px-2.5 py-2 text-right font-mono text-text-secondary">{formatNumber(p.latest_rsi, 1)}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          ) : (
            <div className="py-6 text-center text-sm text-text-muted">No open positions.</div>
          )}
        </Section>

        <Section title="ATM MACD watchlist" rightSlot={<span className="text-[11px] text-text-muted">{watchLegs.length} legs</span>}>
          <div className="max-h-[420px] space-y-0.5 overflow-y-auto pr-1">
            {watchLegs.map((l) => {
              const w = l.watch!;
              const active = selected?.key === l.key;
              const rising = w.previous_macd != null && w.macd != null && w.macd - w.previous_macd > 0;
              return (
                <button key={l.key} type="button" onClick={() => setSelectedKey(l.key)} className={clsx("flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left transition-colors", active ? "bg-accent-blue/10" : "hover:bg-bg-primary/30")}>
                  <SideBadge side={l.contract.direction} />
                  <span className="w-24 truncate text-[12px] font-semibold text-text-primary">{l.contract.underlying}</span>
                  <span className="w-16 text-right font-mono text-[11px] text-text-muted">{formatNumber(l.contract.strike, 0)}</span>
                  <span className={clsx("ml-auto font-mono text-[12px] font-semibold", (w.macd ?? 0) >= 0 ? "text-accent-green" : "text-accent-red")}>{(w.macd ?? 0) >= 0 ? "+" : ""}{formatNumber(w.macd, 3)}</span>
                  <span className={clsx("w-3 text-[10px]", rising ? "text-accent-green" : "text-accent-red")}>{rising ? "▲" : "▼"}</span>
                  <StatusBadge label={statusShort(w.status)} variant={statusVariant(w.status)} />
                </button>
              );
            })}
          </div>
        </Section>
      </div>

      {/* ── Right: selected leg study ── */}
      <div>
        {selected ? <LegDetail selected={selected} /> : <Section title="Detail"><div className="py-16 text-center text-sm text-text-muted">Select a leg to view its 30m premium study.</div></Section>}
      </div>
    </div>
  );
}

function LegDetail({ selected }: { selected: Selected }) {
  const c = selected.contract;
  const p = selected.pos;
  const w = selected.watch;
  const q = useQuery({
    queryKey: ["cockpit-ohlc", c.underlying, c.expiry, c.strike, c.direction],
    queryFn: async () =>
      (await getOptionOHLC({ underlying: c.underlying, expiry: c.expiry, strike: c.strike, optionType: c.direction, interval: "30minute", limit: 400, instrumentKey: c.instrumentKey ?? null })).data as OHLCResp,
    refetchInterval: REFRESH_MS.live * 8,
    refetchOnWindowFocus: false,
  });
  const d = q.data;
  const bars = useMemo<Bar[]>(() => d?.bars ?? [], [d?.bars]);
  const ind = useMemo<Ind>(() => d?.indicators ?? {}, [d?.indicators]);
  const studyBars = useMemo<StudyBar[]>(() => bars.map((b) => ({ time: b.time, open: b.open, high: b.high, low: b.low, close: b.close, volume: b.volume })), [bars]);
  const overlays = useMemo<StudyLine[]>(() => {
    if (!bars.length) return [];
    const line = (k: keyof Ind): StudyLine["data"] => bars.map((b, i) => ({ time: b.time, value: ind[k]?.[i] ?? null }));
    return [
      { id: "bb_upper", data: line("bb_upper"), color: CHART.blue, lineWidth: 1 },
      { id: "bb_middle", data: line("bb_middle"), color: CHART.muted, lineWidth: 1, dashed: true },
      { id: "bb_lower", data: line("bb_lower"), color: CHART.blue, lineWidth: 1 },
      { id: "kama", data: line("kama"), color: CHART.amber, lineWidth: 2 },
    ];
  }, [bars, ind]);

  const strikeLabel = c.strike != null && Number.isInteger(c.strike) ? String(c.strike) : formatNumber(c.strike, 2);
  const mark = p?.current_price ?? c.ltp ?? null;
  const entry = p?.entry_price ?? null;
  const rsi = p?.latest_rsi ?? w?.rsi ?? null;

  return (
    <Section
      title={
        <span className="flex items-center gap-2">
          <SideBadge side={c.direction} />
          <span className="text-text-primary">{c.underlying}</span>
          <span className="font-mono text-text-secondary">{c.direction} {strikeLabel}</span>
          <span className="text-[11px] font-normal text-text-muted">exp {c.expiry}</span>
        </span>
      }
      rightSlot={<span className="text-[10px] uppercase tracking-[0.12em] text-text-muted">30m premium · BB · KAMA · MACD · RSI</span>}
    >
      <OptionStudyChart bars={studyBars} overlays={overlays} macd={ind.macd} signal={ind.macd_signal} histogram={ind.macd_histogram} rsi={ind.rsi} height={460} />

      <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-4">
        <Tile label="Entry → Mark" value={`${formatNumber(entry, 2)} → ${formatNumber(mark, 2)}`} />
        <Tile label="Open P&L" value={p ? `${formatSignedMoney(p.unrealized_pnl)} · ${p.return_pct != null ? `${p.return_pct > 0 ? "+" : ""}${formatNumber(p.return_pct, 1)}%` : "—"}` : "—"} color={tone(p?.unrealized_pnl)} />
        <Tile label="Trailing stop" value={p?.trailing_stop != null ? formatNumber(p.trailing_stop, 2) : "—"} />
        <Tile label="RSI · qty" value={`${formatNumber(rsi, 1)}${p?.qty != null ? ` · ${p.qty}` : ""}`} />
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-2 rounded-lg border border-bg-border bg-bg-primary/15 px-3 py-2 text-[12px]">
        <span className="text-[10px] uppercase tracking-[0.12em] text-text-muted">Signal · phase</span>
        {p?.phase ? <StatusBadge label={String(p.phase).replaceAll("_", " ")} variant="info" /> : w?.status ? <StatusBadge label={statusShort(w.status)} variant={statusVariant(w.status)} /> : null}
        <span className="text-text-secondary">{(p?.signal_reason || w?.status || "").toString().replaceAll("_", " ") || "—"}</span>
        {bars.length ? <span className="ml-auto text-[10px] text-text-muted">as of {formatIST(bars[bars.length - 1].time)}</span> : null}
      </div>
    </Section>
  );
}

// ── small atoms ─────────────────────────────────────────────────────────────
function SideBadge({ side }: { side?: string | null }) {
  const isCE = String(side || "").toUpperCase() === "CE";
  return <span className={clsx("inline-flex rounded-md border px-1.5 py-0.5 text-[10px] font-bold", isCE ? "border-accent-green/40 bg-accent-green/10 text-accent-green" : "border-accent-red/40 bg-accent-red/10 text-accent-red")}>{isCE ? "CE" : "PE"}</span>;
}
function Tile({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div className="rounded-lg border border-bg-border bg-bg-primary/15 px-2.5 py-1.5">
      <div className="text-[9.5px] uppercase tracking-[0.12em] text-text-muted">{label}</div>
      <div className={clsx("mt-0.5 font-mono text-[12.5px] font-semibold", color || "text-text-primary")}>{value}</div>
    </div>
  );
}
function statusShort(s?: string | null): string {
  const t = String(s || "").toLowerCase();
  if (t.includes("entry-ready") || t.includes("ready")) return "entry-ready";
  if (t.includes("trend-aligned") || t.includes("aligned")) return "trend-aligned";
  if (t.includes("waiting")) return "waiting";
  if (t.includes("monitor") || t.includes("watching")) return "monitor";
  if (t.includes("missing")) return "no data";
  return t ? t.replaceAll("_", " ") : "—";
}
function statusVariant(s?: string | null): "neutral" | "success" | "warn" | "error" | "info" {
  const t = String(s || "").toLowerCase();
  if (t.includes("entry-ready") || t.includes("ready")) return "success";
  if (t.includes("trend-aligned") || t.includes("aligned") || t.includes("monitor")) return "info";
  if (t.includes("waiting") || t.includes("standby")) return "warn";
  if (t.includes("missing")) return "error";
  return "neutral";
}

type Bar = { time: string; open: number; high: number; low: number; close: number; volume: number };
type Ind = {
  macd?: (number | null)[]; macd_signal?: (number | null)[]; macd_histogram?: (number | null)[]; rsi?: (number | null)[];
  bb_upper?: (number | null)[]; bb_middle?: (number | null)[]; bb_lower?: (number | null)[]; kama?: (number | null)[];
};
type OHLCResp = { bars?: Bar[]; indicators?: Ind; detail?: string | null };

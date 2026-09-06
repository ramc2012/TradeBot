"use client";

import { useState } from "react";
import { Area, Bar, CartesianGrid, ComposedChart, Line, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { Activity, Layers3, ShieldCheck } from "lucide-react";
import { Section, StatusBadge, formatNumber } from "@/components/desk-ui";

export type AuctionInsightsData = {
  snapshot_id: string; as_of: string; session_date: string; source: string;
  bar_interval_minutes: number; flow_available: boolean; flow_label: string;
  entry_gate: string; readout: string; location: string;
  path: { time: string; close: number; high: number; low: number; poc: number; vah: number; val: number; volume: number; flow_proxy: number; cumulative_flow_proxy: number; location: string }[];
  intelligence: { day_type: string; range_over_ib: number; ib_width_pct: number };
};

const timeLabel = (value: string) => new Date(value).toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit", hour12: false });
const colours = { up: "#34d399", down: "#fb7185", value: "#818cf8", poc: "#fbbf24", line: "#cbd5e1" };
const tooltip = { background: "#101827", border: "1px solid #334155", borderRadius: 10, fontSize: 12, color: "#e2e8f0" };

export function AuctionInsights({ data, decisions = [], riskReasons = [] }: {
  data?: AuctionInsightsData;
  decisions?: { agent_name?: string; action?: string; confidence?: number | null; rationale?: string[]; metadata?: Record<string, unknown> | null }[];
  riskReasons?: string[];
}) {
  const [selected, setSelected] = useState<number | null>(null);
  if (!data) return <Section title="Auction map"><p className="py-8 text-sm text-text-muted">Preparing the shared auction snapshot. Missing history is shown as unavailable; no synthetic prices are used.</p></Section>;
  const rows = data.path.map((p) => ({ ...p, valueBand: [p.val, p.vah], clock: timeLabel(p.time) }));
  const focus = rows[selected ?? rows.length - 1];
  return <div className="space-y-4">
    <section className="overflow-hidden rounded-xl border border-indigo-400/20 bg-gradient-to-br from-indigo-500/10 via-slate-900/20 to-emerald-500/5 p-5">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div><p className="text-[10px] font-semibold uppercase tracking-[0.2em] text-indigo-300">Auction lens · {data.session_date}</p><h2 className="mt-2 text-lg font-medium text-text-primary">{data.readout}</h2><p className="mt-2 text-xs text-text-muted">{data.source} · {data.bar_interval_minutes}-minute source bars · one snapshot shared with MP Intelligence</p></div>
        <div className="flex flex-wrap gap-2"><StatusBadge label="PAPER ONLY" variant="info" /><StatusBadge label={data.entry_gate.replaceAll("_", " ")} variant={data.entry_gate === "historical_replay" ? "neutral" : "warn"} /></div>
      </div>
      <div className="mt-5 grid grid-cols-2 gap-4 md:grid-cols-4">
        {[["Developing day type", data.intelligence.day_type.replaceAll("_", " ")], ["Range / initial balance", `${formatNumber(data.intelligence.range_over_ib, 2)}×`], ["Initial balance width", `${formatNumber(data.intelligence.ib_width_pct, 2)}%`], ["Snapshot", data.snapshot_id.slice(0, 8)]].map(([label, value]) => <div key={label} className="border-l border-slate-500/30 pl-3"><p className="text-[10px] uppercase tracking-wide text-text-muted">{label}</p><p className="mt-1 text-sm font-medium tabular-nums text-text-primary">{value}</p></div>)}
      </div>
    </section>
    <div className="grid gap-4 xl:grid-cols-[minmax(0,2fr)_minmax(280px,1fr)]">
      <Section title="Price × developing value" icon={<Layers3 size={16} />} description="Each point uses only bars available then. Hover both charts to compare; select a period below to inspect its value area.">
        <div className="flex flex-wrap gap-4 pb-3 text-[11px] text-text-muted"><span className="text-slate-200">● Price</span><span className="text-amber-300">● Developing POC</span><span className="text-indigo-300">● 70% TPO value area</span><span>IST · bar start times</span></div>
        <div className="h-[290px] w-full">
          <ResponsiveContainer><ComposedChart data={rows} syncId={`auction-${data.snapshot_id}`} margin={{ top: 8, right: 12, bottom: 0, left: 10 }}>
            <CartesianGrid stroke="#334155" strokeOpacity={0.35} vertical={false} />
            <XAxis dataKey="clock" tick={{ fill: "#94a3b8", fontSize: 10 }} minTickGap={25} />
            <YAxis domain={["auto", "auto"]} tick={{ fill: "#94a3b8", fontSize: 10 }} width={62} tickFormatter={(n) => formatNumber(n, 0)} />
            <Tooltip contentStyle={tooltip} formatter={(value: number | number[]) => Array.isArray(value) ? value.map(v => formatNumber(v, 2)).join(" – ") : formatNumber(value, 2)} />
            <Area dataKey="valueBand" name="Value area" stroke="none" fill={colours.value} fillOpacity={0.18} isAnimationActive={false} />
            <Line dataKey="poc" name="POC" stroke={colours.poc} dot={false} strokeDasharray="4 3" isAnimationActive={false} />
            <Line dataKey="close" name="Price" stroke={colours.line} strokeWidth={2} dot={{ r: 2 }} isAnimationActive={false} />
            {selected !== null && focus ? <ReferenceLine x={focus.clock} stroke="#94a3b8" strokeDasharray="2 2" /> : null}
          </ComposedChart></ResponsiveContainer>
        </div>
        <div className="mt-2 flex gap-1 overflow-x-auto pb-2" aria-label="Auction periods">{rows.map((p, i) => <button key={p.time} aria-pressed={selected === i} onClick={() => setSelected(selected === i ? null : i)} title={`${p.clock}: ${p.location} value`} className={`min-w-[42px] flex-1 rounded border px-1 py-2 text-[10px] tabular-nums ${selected === i ? "border-white/70" : "border-transparent"} ${p.location === "above" ? "bg-emerald-400/15 text-emerald-300" : p.location === "below" ? "bg-rose-400/15 text-rose-300" : "bg-indigo-400/15 text-indigo-300"}`}>{p.clock}</button>)}</div>
        {focus ? <p className="mt-2 text-xs text-text-muted">{focus.clock} · {focus.location} value · VAL {formatNumber(focus.val, 2)} · POC {formatNumber(focus.poc, 2)} · VAH {formatNumber(focus.vah, 2)}</p> : null}
      </Section>
      <Section title="Decision desk" icon={<ShieldCheck size={16} />} description="Setup proposals remain subject to freshness, exact-contract quotes and paper portfolio limits.">
        <div className="space-y-3">{decisions.map((d) => <div key={d.agent_name} className="rounded-lg border border-border-subtle bg-bg-primary/40 p-3"><div className="flex items-center justify-between"><span className="text-xs font-medium capitalize">{d.agent_name}</span><StatusBadge label={d.action ?? "FLAT"} variant={d.action === "LONG" ? "success" : d.action === "SHORT" ? "error" : "neutral"} /></div><div className="my-3 h-1 overflow-hidden rounded bg-slate-700/50"><div className="h-full rounded bg-indigo-400" style={{ width: `${Math.max(0, Math.min(1, d.confidence ?? 0)) * 100}%` }} /></div><p className="text-xs leading-relaxed text-text-muted">{String(d.metadata?.flat_reason ?? d.metadata?.setup_name ?? d.rationale?.[0] ?? "Waiting for a qualified setup").replaceAll("_", " ")}</p><p className="mt-2 text-[10px] text-text-muted">Confidence {formatNumber((d.confidence ?? 0) * 100, 0)}% · model score, not win probability</p></div>)}</div>
        <p className="mt-4 text-xs leading-relaxed text-text-muted">{riskReasons.join(" ") || "No executable contract confirmed."}</p>
      </Section>
    </div>
    <Section title="Participation pressure" icon={<Activity size={16} />} description={data.flow_label}>
      {data.flow_available ? <div className="h-[170px]"><ResponsiveContainer><ComposedChart data={rows} syncId={`auction-${data.snapshot_id}`} margin={{ top: 5, right: 20, left: 10, bottom: 0 }}><CartesianGrid stroke="#334155" strokeOpacity={0.35} vertical={false} /><XAxis dataKey="clock" tick={{ fill: "#94a3b8", fontSize: 10 }} minTickGap={25} /><YAxis tick={{ fill: "#94a3b8", fontSize: 10 }} width={62} tickFormatter={(n) => Intl.NumberFormat("en", { notation: "compact" }).format(n)} /><Tooltip contentStyle={tooltip} formatter={(v: number) => formatNumber(v, 0)} /><ReferenceLine y={0} stroke="#64748b" /><Bar dataKey="flow_proxy" name="Period pressure proxy" fill="#818cf8" opacity={0.7} isAnimationActive={false} /><Line dataKey="cumulative_flow_proxy" name="Cumulative proxy" stroke="#34d399" dot={false} isAnimationActive={false} /></ComposedChart></ResponsiveContainer></div> : <p className="py-7 text-center text-sm text-text-muted">This source has no traded volume. Pressure and cumulative flow are unavailable.</p>}
      <p className="mt-2 text-[11px] text-text-muted">Volume × (close − open) / bar range. This describes candle pressure; it cannot identify buyers, sellers or absorption. A developing day type is context, not a forecast.</p>
    </Section>
  </div>;
}

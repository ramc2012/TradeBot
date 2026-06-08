"use client";

/**
 * Dealer-positioning analytics panel for the long-premium (directional) desk.
 *
 * Black-76 GEX engine ported from the fyers-webapp options-analytics module:
 * per-expiry GEX-by-strike profile, Net GEX (₹Cr), gamma flip (zero-gamma spot),
 * gamma density, DEX, max-pain, call/put walls, IV smile — plus a term structure
 * across the nearest expiries and a 30-min net-GEX / OI progression with
 * strike×time heatmaps.
 *
 * Data: GET /api/directional-options/gex (per-expiry + term) and
 *       GET /api/directional-options/gex-progression (lazy, per expiry).
 * Additive to /chain-analytics (which still feeds the RL policy).
 */
import { useState } from "react";
import { clsx } from "clsx";
import { useQuery } from "@tanstack/react-query";
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  Cell,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { Activity, ChevronDown, Layers, Sigma, TrendingUp } from "lucide-react";

import { MetricTile, REFRESH_MS, Section, formatNumber, tone } from "@/components/desk-ui";
import { api as apiClient } from "@/lib/api";
import GexHeatmap from "./GexHeatmap";

const POS = "#00d4a3";
const NEG = "#ff4757";
const CHART_TT = { background: "#0f1724", border: "1px solid #1e2d45", borderRadius: 8, fontSize: 12 } as const;

type Meta = {
  expiry: string;
  days: number | null;
  fp: number | null;
  spot: number | null;
  basis: number | null;
  pcr: number | null;
  atm: number | null;
  atm_iv: number | null;
  max_pain: number | null;
  call_wall: number | null;
  put_wall: number | null;
  net_gex: number | null;
  net_dex: number | null;
  gamma_flip: number | null;
};
type Row = {
  strike: number;
  ce_iv: number | null;
  pe_iv: number | null;
  ce_oi: number;
  pe_oi: number;
  gex: number;
  dex: number;
  gdens: number;
};
type PerExpiry = { meta: Meta; rows: Row[] };
type Term = {
  labels: (string | null)[];
  days: (number | null)[];
  pcr: (number | null)[];
  atm_iv: (number | null)[];
  net_gex: (number | null)[];
  max_pain: (number | null)[];
  tot_oi: (number | null)[];
};
type GexPayload = {
  available?: boolean;
  underlying?: string;
  spot?: number | null;
  per_expiry?: PerExpiry[];
  term?: Term | null;
};
type Progression = {
  times: string[];
  strikes: number[];
  idx: (number | null)[];
  gdens: (number | null)[][];
  oi_call: (number | null)[][];
  oi_put: (number | null)[][];
  gex: (number | null)[];
  regime: (string | null)[];
  atm: number | null;
};
type ProgPayload = { available?: boolean; expiry?: string; atm?: number | null; progression?: Progression };

function compact(n: number | null | undefined, unit = ""): string {
  if (n == null || Number.isNaN(n)) return "—";
  const abs = Math.abs(n);
  let s: string;
  if (abs >= 1e7) s = `${(n / 1e7).toFixed(2)}Cr`;
  else if (abs >= 1e5) s = `${(n / 1e5).toFixed(2)}L`;
  else if (abs >= 1e3) s = `${(n / 1e3).toFixed(1)}K`;
  else s = n.toFixed(abs < 10 && abs > 0 ? 2 : 0);
  return unit ? `${s}${unit}` : s;
}
function pcrTone(p?: number | null): string {
  if (p == null) return "text-text-muted";
  if (p > 1.2) return "text-accent-green";
  if (p < 0.8) return "text-accent-red";
  return "text-text-secondary";
}

export default function OptionAnalyticsPanel({ underlying, expiry }: { underlying: string; expiry?: string | null }) {
  const { data, isLoading } = useQuery({
    queryKey: ["directional", "gex", underlying],
    queryFn: async () =>
      (await apiClient.get("/api/directional-options/gex", { params: { underlying } })).data as GexPayload,
    refetchInterval: REFRESH_MS.live,
    refetchOnWindowFocus: false,
  });

  const expiries = data?.per_expiry?.map((p) => p.meta.expiry) ?? [];
  const [selected, setSelected] = useState<string | null>(null);
  const activeExpiry = selected ?? (expiry && expiries.includes(expiry) ? expiry : expiries[0] ?? null);
  const current = data?.per_expiry?.find((p) => p.meta.expiry === activeExpiry) ?? null;

  if (isLoading) {
    return (
      <Section title="Dealer positioning · GEX" icon={<Sigma size={16} />}>
        <div className="text-sm text-text-muted">Loading option chain…</div>
      </Section>
    );
  }
  if (!data?.available || !current) {
    return (
      <Section title="Dealer positioning · GEX" icon={<Sigma size={16} />}>
        <div className="rounded-xl border border-bg-border bg-bg-primary/15 p-3 text-sm text-text-muted">
          No chain cached for <span className="font-mono">{underlying}</span>. GEX analytics populate once the broker
          websocket fills the option chain. Open Market → Option Chain once for this underlying to prime it.
        </div>
      </Section>
    );
  }

  const m = current.meta;
  const gexData = current.rows.map((r) => ({ strike: r.strike, gex: r.gex, gdens: r.gdens }));
  const smileData = current.rows
    .map((r) => ({ strike: r.strike, ce_iv: r.ce_iv, pe_iv: r.pe_iv }))
    .filter((r) => r.ce_iv != null || r.pe_iv != null);

  return (
    <div className="space-y-4">
      {/* Expiry tabs */}
      <div className="flex flex-wrap items-center gap-2">
        {expiries.map((e) => (
          <button
            key={e}
            onClick={() => setSelected(e)}
            className={clsx(
              "rounded-full border px-3 py-1 text-xs font-medium transition",
              e === activeExpiry
                ? "border-accent-blue/60 bg-accent-blue/15 text-text-primary"
                : "border-bg-border bg-bg-secondary/30 text-text-muted hover:text-text-secondary",
            )}
          >
            {e}
          </button>
        ))}
        <span className="ml-auto text-[11px] text-text-muted">
          spot {formatNumber(m.spot, 1)} · fwd {formatNumber(m.fp, 1)} · {m.days != null ? `${m.days}d` : "—"}
        </span>
      </div>

      {/* KPI row */}
      <Section title={`Dealer positioning · ${underlying} ${activeExpiry ?? ""}`} icon={<Sigma size={16} />}>
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-6">
          <MetricTile
            label="Net GEX"
            value={`${m.net_gex != null && m.net_gex >= 0 ? "+" : ""}${compact(m.net_gex)} Cr`}
            color={tone(m.net_gex)}
            detail={m.net_gex != null ? (m.net_gex >= 0 ? "long-gamma · stabilizing" : "short-gamma · trending") : ""}
          />
          <MetricTile
            label="Gamma flip"
            value={m.gamma_flip != null ? formatNumber(m.gamma_flip, 0) : "—"}
            color={m.gamma_flip != null && m.spot != null ? (m.spot >= m.gamma_flip ? "text-accent-green" : "text-accent-red") : undefined}
            detail={m.gamma_flip != null && m.spot != null ? `${m.spot >= m.gamma_flip ? "above" : "below"} flip` : "no crossing"}
          />
          <MetricTile label="ATM IV" value={m.atm_iv != null ? `${m.atm_iv.toFixed(2)}%` : "—"} />
          <MetricTile label="PCR (OI)" value={m.pcr != null ? m.pcr.toFixed(3) : "—"} color={pcrTone(m.pcr)} />
          <MetricTile
            label="Max pain"
            value={formatNumber(m.max_pain, 0)}
            detail={m.spot != null && m.max_pain != null ? `${(((m.max_pain - m.spot) / m.spot) * 100).toFixed(2)}% vs spot` : ""}
          />
          <MetricTile
            label="Net DEX"
            value={`${m.net_dex != null && m.net_dex >= 0 ? "+" : ""}${compact(m.net_dex)} Cr`}
            color={tone(m.net_dex)}
            detail={`walls C${formatNumber(m.call_wall, 0)} / P${formatNumber(m.put_wall, 0)}`}
          />
        </div>
      </Section>

      {/* GEX-by-strike profile */}
      <Section title="GEX by strike · ₹Cr per 1% move" icon={<Layers size={16} />}>
        <div className="h-72">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={gexData} margin={{ top: 6, right: 8, bottom: 4, left: 4 }}>
              <XAxis dataKey="strike" type="number" domain={["dataMin", "dataMax"]} tick={{ fill: "#94a3b8", fontSize: 11 }} />
              <YAxis tick={{ fill: "#94a3b8", fontSize: 11 }} tickFormatter={(v) => compact(v)} />
              <Tooltip formatter={(v: number) => [`${compact(v)} Cr`, "GEX"]} labelFormatter={(s) => `Strike ${s}`} contentStyle={CHART_TT} />
              <ReferenceLine y={0} stroke="#1e2d45" />
              {m.spot != null ? (
                <ReferenceLine x={m.spot} stroke="#3b82f6" strokeDasharray="3 3" label={{ value: "spot", fill: "#3b82f6", fontSize: 10, position: "top" }} />
              ) : null}
              {m.gamma_flip != null ? (
                <ReferenceLine x={m.gamma_flip} stroke="#f5c842" strokeDasharray="4 2" label={{ value: "flip", fill: "#f5c842", fontSize: 10, position: "top" }} />
              ) : null}
              <Bar dataKey="gex">
                {gexData.map((d) => (
                  <Cell key={d.strike} fill={d.gex >= 0 ? POS : NEG} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
        <p className="mt-2 text-[11px] text-text-muted">
          Positive (green) = dealers long gamma at that strike (mean-reverting / pin); negative (red) = short gamma
          (move-amplifying). The yellow line is the zero-gamma flip — spot above it is the stabilizing regime.
        </p>
      </Section>

      <div className="grid gap-4 lg:grid-cols-2">
        {/* Gamma density */}
        <Section title="Gamma density · γ·OI concentration" icon={<Activity size={16} />}>
          <div className="h-56">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={gexData} margin={{ top: 6, right: 8, bottom: 4, left: 4 }}>
                <defs>
                  <linearGradient id="gd" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="#3b82f6" stopOpacity={0.55} />
                    <stop offset="100%" stopColor="#3b82f6" stopOpacity={0.04} />
                  </linearGradient>
                </defs>
                <XAxis dataKey="strike" type="number" domain={["dataMin", "dataMax"]} tick={{ fill: "#94a3b8", fontSize: 11 }} />
                <YAxis tick={{ fill: "#94a3b8", fontSize: 11 }} />
                <Tooltip formatter={(v: number) => [v.toFixed(2), "γ·OI (M)"]} labelFormatter={(s) => `Strike ${s}`} contentStyle={CHART_TT} />
                {m.atm != null ? <ReferenceLine x={m.atm} stroke="#3b82f6" strokeDasharray="3 3" /> : null}
                <Area type="monotone" dataKey="gdens" stroke="#3b82f6" fill="url(#gd)" />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        </Section>

        {/* IV smile */}
        <Section title="IV smile / skew" icon={<TrendingUp size={16} />}>
          <div className="h-56">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={smileData} margin={{ top: 6, right: 8, bottom: 4, left: 4 }}>
                <XAxis dataKey="strike" type="number" domain={["dataMin", "dataMax"]} tick={{ fill: "#94a3b8", fontSize: 11 }} />
                <YAxis tick={{ fill: "#94a3b8", fontSize: 11 }} tickFormatter={(v) => `${v}%`} />
                <Tooltip formatter={(v: number, n: string) => [`${v?.toFixed?.(2)}%`, n === "ce_iv" ? "Call IV" : "Put IV"]} labelFormatter={(s) => `Strike ${s}`} contentStyle={CHART_TT} />
                {m.atm != null ? <ReferenceLine x={m.atm} stroke="#3b82f6" strokeDasharray="3 3" /> : null}
                <Line type="monotone" dataKey="ce_iv" stroke={POS} dot={false} connectNulls />
                <Line type="monotone" dataKey="pe_iv" stroke={NEG} dot={false} connectNulls />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </Section>
      </div>

      {/* Term structure */}
      {data.term && data.term.labels.length > 1 ? <TermStructure term={data.term} /> : null}

      {/* Progression (lazy) */}
      <ProgressionSection underlying={underlying} expiry={activeExpiry} />
    </div>
  );
}

function TermStructure({ term }: { term: Term }) {
  const rows = term.labels.map((label, i) => ({
    label: label ?? "—",
    days: term.days[i],
    pcr: term.pcr[i],
    atm_iv: term.atm_iv[i],
    net_gex: term.net_gex[i],
    max_pain: term.max_pain[i],
  }));
  return (
    <Section title="Term structure" icon={<Layers size={16} />}>
      <div className="grid gap-4 lg:grid-cols-[1.1fr,1fr]">
        <table className="w-full text-[12px]">
          <thead className="text-[10px] uppercase tracking-wide text-text-muted">
            <tr className="border-b border-bg-border/40">
              <th className="px-2 py-1.5 text-left">Expiry</th>
              <th className="px-2 py-1.5 text-right">Days</th>
              <th className="px-2 py-1.5 text-right">ATM IV</th>
              <th className="px-2 py-1.5 text-right">PCR</th>
              <th className="px-2 py-1.5 text-right">Net GEX</th>
              <th className="px-2 py-1.5 text-right">Max pain</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.label} className="border-b border-bg-border/20">
                <td className="px-2 py-1.5 font-mono">{r.label}</td>
                <td className="px-2 py-1.5 text-right font-mono">{r.days ?? "—"}</td>
                <td className="px-2 py-1.5 text-right font-mono">{r.atm_iv != null ? `${r.atm_iv}%` : "—"}</td>
                <td className={clsx("px-2 py-1.5 text-right font-mono", pcrTone(r.pcr))}>{r.pcr ?? "—"}</td>
                <td className={clsx("px-2 py-1.5 text-right font-mono", tone(r.net_gex))}>{compact(r.net_gex)}</td>
                <td className="px-2 py-1.5 text-right font-mono">{formatNumber(r.max_pain, 0)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="h-48">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={rows} margin={{ top: 6, right: 8, bottom: 4, left: 4 }}>
              <XAxis dataKey="label" tick={{ fill: "#94a3b8", fontSize: 11 }} />
              <YAxis yAxisId="l" tick={{ fill: "#94a3b8", fontSize: 11 }} />
              <YAxis yAxisId="r" orientation="right" tick={{ fill: "#94a3b8", fontSize: 11 }} tickFormatter={(v) => `${v}%`} />
              <Tooltip contentStyle={CHART_TT} />
              <ReferenceLine yAxisId="l" y={0} stroke="#1e2d45" />
              <Line yAxisId="l" type="monotone" dataKey="net_gex" name="Net GEX" stroke="#f5c842" dot />
              <Line yAxisId="r" type="monotone" dataKey="atm_iv" name="ATM IV" stroke="#3b82f6" dot />
            </LineChart>
          </ResponsiveContainer>
        </div>
      </div>
    </Section>
  );
}

function ProgressionSection({ underlying, expiry }: { underlying: string; expiry: string | null }) {
  const [open, setOpen] = useState(false);
  const { data, isLoading } = useQuery({
    queryKey: ["directional", "gex-prog", underlying, expiry],
    queryFn: async () =>
      (await apiClient.get("/api/directional-options/gex-progression", { params: { underlying, expiry } })).data as ProgPayload,
    enabled: open && !!expiry,
    refetchInterval: open ? REFRESH_MS.summary : false,
    refetchOnWindowFocus: false,
  });
  const prog = data?.progression;
  const gexSeries = prog?.times.map((t, i) => ({ t, gex: prog.gex[i], regime: prog.regime[i] })) ?? [];

  return (
    <Section
      title="Intraday GEX progression · 30-min"
      icon={<Activity size={16} />}
      rightSlot={
        <button onClick={() => setOpen((v) => !v)} className="flex items-center gap-1 text-[11px] text-text-muted hover:text-text-secondary">
          {open ? "hide" : "load"}
          <ChevronDown size={14} className={clsx("transition", open && "rotate-180")} />
        </button>
      }
    >
      {!open ? (
        <div className="text-[11px] text-text-muted">Net-GEX / OI over the day + strike×time heatmaps (history fetch — click load).</div>
      ) : isLoading ? (
        <div className="text-sm text-text-muted">Loading 30-min history…</div>
      ) : !data?.available || !prog ? (
        <div className="rounded-xl border border-bg-border bg-bg-primary/15 p-3 text-[12px] text-text-muted">
          Not enough 30-min option history for {expiry}. The progression needs option_premium_candles coverage across
          the strike band (ingest is episodic).
        </div>
      ) : (
        <div className="space-y-4">
          <div className="h-56">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={gexSeries} margin={{ top: 6, right: 8, bottom: 4, left: 4 }}>
                <XAxis dataKey="t" tick={{ fill: "#94a3b8", fontSize: 10 }} />
                <YAxis tick={{ fill: "#94a3b8", fontSize: 11 }} tickFormatter={(v) => compact(v)} />
                <Tooltip formatter={(v: number) => [`${compact(v)} Cr`, "Net GEX"]} contentStyle={CHART_TT} />
                <ReferenceLine y={0} stroke="#1e2d45" />
                <Bar dataKey="gex">
                  {gexSeries.map((d, i) => (
                    <Cell key={i} fill={(d.gex ?? 0) >= 0 ? POS : NEG} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
          <div className="grid gap-4 lg:grid-cols-2">
            <GexHeatmap title="Gamma density (strike × time)" strikes={prog.strikes} times={prog.times} matrix={prog.gdens} atm={prog.atm} diverging />
            <GexHeatmap title="Call OI (strike × time)" strikes={prog.strikes} times={prog.times} matrix={prog.oi_call} atm={prog.atm} />
          </div>
        </div>
      )}
    </Section>
  );
}

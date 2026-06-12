"use client";

/**
 * Options Analytics Dashboard — implements the approved HTML prototype
 * (2026-06-11) for the long-premium desk on top of the Black-76 GEX engine.
 *
 * Sections (matching the prototype): expiry selector + spot/VIX header, 8-KPI
 * dealer-positioning strip, OI by strike, OI change (gradient build/unwind),
 * IV smile (OTM composite + per-side), Greeks profile (Δ sweep + Γ), GEX and
 * DEX by strike with spot/flip markers, market-read narrative, enhanced
 * option chain (OI bars + gradient OIΔ + ATM highlight), term structure
 * (ATM IV / PCR / Net GEX / Total OI), and the intraday progression suite
 * (single-strike CE+PE OI with Δ-colored markers, regime-shaded Net-GEX,
 * gamma-density + OI-change strike×time heat grids).
 *
 * Data: GET /api/directional-options/gex (per-expiry rows carry ltp/oi/oiΔ/
 * IV/Δ/Γ/θ per side + gex/dex/gdens) and GET /gex-progression (lazy).
 */
import { useMemo, useState } from "react";
import { clsx } from "clsx";
import { useQuery } from "@tanstack/react-query";
import {
  Bar,
  BarChart,
  ComposedChart,
  Line,
  LineChart,
  Cell,
  ReferenceArea,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { ChevronDown, Crosshair, Flame, Gauge, Layers, ListTree, Magnet, Sigma, TrendingUp, Activity } from "lucide-react";

import { REFRESH_MS, Section, formatNumber } from "@/components/desk-ui";
import { api as apiClient } from "@/lib/api";

const POS = "#27c08a";
const NEG = "#ff5470";
const CALL = "#3b82f6";
const PUT = "#f59e0b";
const ACCENT = "#7c5cff";
const CHART_TT = { background: "#0f1724", border: "1px solid #1e2d45", borderRadius: 8, fontSize: 12 } as const;
const TICK = { fill: "#8a97ad", fontSize: 10 } as const;

type Row = {
  strike: number;
  ce_ltp: number | null;
  pe_ltp: number | null;
  ce_oi: number;
  pe_oi: number;
  ce_oich: number | null;
  pe_oich: number | null;
  ce_iv: number | null;
  pe_iv: number | null;
  ce_delta: number | null;
  pe_delta: number | null;
  ce_gamma: number | null;
  pe_gamma: number | null;
  ce_theta: number | null;
  pe_theta: number | null;
  gex: number;
  dex: number;
  gdens: number;
};
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
  as_of?: string | null;
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
  vix?: number | null;
  as_of?: string | null;
  per_expiry?: PerExpiry[];
  term?: Term | null;
};
type Progression = {
  times: string[];
  strikes: number[];
  idx: (number | null)[];
  gdens: (number | null)[][];
  netgex?: (number | null)[][];
  oi_call: (number | null)[][];
  oi_put: (number | null)[][];
  oi_change?: (number | null)[];
  gex: (number | null)[];
  regime: (string | null)[];
  atm: number | null;
};
type ProgPayload = {
  available?: boolean;
  expiry?: string;
  atm?: number | null;
  degraded?: boolean;
  data_sources?: Record<string, number>;
  progression?: Progression;
};

const lakh = (n: number | null | undefined) =>
  n == null || Number.isNaN(n) ? "—" : `${(n / 100000).toFixed(1)}L`;
const fmtCr = (v: number | null | undefined) =>
  v == null || Number.isNaN(v) ? "—" : `${v >= 0 ? "+" : ""}${Math.round(v).toLocaleString("en-IN")}`;
const inr = (v: number | null | undefined, d = 0) =>
  v == null || Number.isNaN(v) ? "—" : v.toLocaleString("en-IN", { maximumFractionDigits: d });

function nearestIdx(values: number[], target: number): number {
  return values.reduce((b, v, i) => (Math.abs(v - target) < Math.abs(values[b] - target) ? i : b), 0);
}

function AsOfBadge({ asOf }: { asOf: string }) {
  // The chain cache is re-stamped every ~30s even after market close, so the
  // timestamp alone always looks fresh — combine age with the NSE session
  // window to honestly label EOD-frozen data.
  // The chain cache stamps a NAIVE UTC timestamp (no offset) — parse it as
  // UTC, not browser-local, or the badge reads 5.5h stale all session in IST.
  const normalized = /[zZ]|[+-]\d{2}:?\d{2}$/.test(asOf) ? asOf : `${asOf}Z`;
  const ts = new Date(normalized);
  if (Number.isNaN(ts.getTime())) return null;
  const ageMin = (Date.now() - ts.getTime()) / 60000;
  const ist = new Date(Date.now() + (330 + new Date().getTimezoneOffset()) * 60000);
  const mins = ist.getHours() * 60 + ist.getMinutes();
  const inSession = ist.getDay() >= 1 && ist.getDay() <= 5 && mins >= 9 * 60 + 15 && mins <= 15 * 60 + 30;
  const stale = ageMin > 5 || !inSession;
  return (
    <span
      className={clsx(
        "ml-2 rounded-full border px-2 py-0.5 text-[10px]",
        stale ? "border-amber-500/40 text-amber-300" : "border-emerald-500/30 text-emerald-300",
      )}
    >
      {stale ? "EOD" : "live"} ·{" "}
      {ts.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", timeZone: "Asia/Kolkata" })}
    </span>
  );
}

function Kpi({ label, value, meta, toneClass }: { label: string; value: string; meta?: string; toneClass?: string }) {
  return (
    <div className="rounded-xl border border-bg-border bg-bg-secondary/40 px-3 py-2.5">
      <div className="text-[10px] font-semibold uppercase tracking-[0.14em] text-text-muted">{label}</div>
      <div className={clsx("mt-1 font-mono text-[17px] font-bold tabular-nums", toneClass ?? "text-text-primary")}>{value}</div>
      {meta ? <div className="mt-0.5 text-[10px] text-text-muted">{meta}</div> : null}
    </div>
  );
}

/** Gradient cell color for OIΔ: green build / red unwind, alpha ∝ |Δ|/max. */
function oichColor(v: number | null | undefined, maxAbs: number): string | undefined {
  if (v == null || Number.isNaN(v) || maxAbs <= 0) return undefined;
  const a = 0.18 + 0.72 * Math.min(1, Math.abs(v) / maxAbs);
  return v >= 0 ? `rgba(39,192,138,${a})` : `rgba(255,84,112,${a})`;
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
      <Section title="Options analytics" icon={<Sigma size={16} />}>
        <div className="text-sm text-text-muted">Loading option chain…</div>
      </Section>
    );
  }
  if (!data?.available || !current) {
    return (
      <Section title="Options analytics" icon={<Sigma size={16} />}>
        <div className="rounded-xl border border-bg-border bg-bg-primary/15 p-3 text-[12px] text-text-muted">
          No cached option chain yet for {underlying}. The chain poller warms up within ~1 min of the desk opening.
        </div>
      </Section>
    );
  }

  const m = current.meta;
  const rows = current.rows;
  const spot = data.spot ?? m.spot ?? 0;
  const strikes = rows.map((r) => r.strike);
  const spotIdx = nearestIdx(strikes, spot);
  const flipIdx = m.gamma_flip != null ? nearestIdx(strikes, m.gamma_flip) : null;

  const maxOich = Math.max(1, ...rows.flatMap((r) => [Math.abs(r.ce_oich ?? 0), Math.abs(r.pe_oich ?? 0)]));
  const hasOich = rows.some((r) => r.ce_oich != null || r.pe_oich != null);

  const chartRows = rows.map((r, i) => ({
    ...r,
    label: String(r.strike),
    otm_iv: r.strike < spot ? r.pe_iv : r.ce_iv,
    gamma_any: r.ce_gamma ?? r.pe_gamma,
    isSpot: i === spotIdx,
  }));

  const sentiment = (m.pcr ?? 0) > 1 ? "put-heavy" : (m.pcr ?? 0) > 0.8 ? "balanced" : "call-heavy";
  const flipText =
    m.gamma_flip != null
      ? `gamma flip at ${inr(m.gamma_flip)} (spot ${spot > m.gamma_flip ? "above — stabilizing" : "below — unstable"})`
      : "no gamma flip inside the band";

  const spotRef = (
    <ReferenceLine
      x={String(strikes[spotIdx])}
      stroke="rgba(255,255,255,0.6)"
      strokeDasharray="4 4"
      label={{ value: "Spot", fill: "#cbd5e1", fontSize: 10, position: "insideTopRight" }}
    />
  );
  const flipRef =
    flipIdx != null ? (
      <ReferenceLine
        x={String(strikes[flipIdx])}
        stroke="#ffd591"
        strokeDasharray="2 2"
        label={{ value: "Flip", fill: "#ffd591", fontSize: 10, position: "insideTopLeft" }}
      />
    ) : null;

  return (
    <div className="space-y-4">
      {/* Header: expiry pills + spot / fwd / VIX / freshness */}
      <div className="flex flex-wrap items-center gap-2">
        {expiries.map((e) => {
          const days = data?.per_expiry?.find((p) => p.meta.expiry === e)?.meta.days;
          return (
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
              {days != null ? <span className="ml-1 text-[10px] text-text-muted">({formatNumber(days, 1)}d)</span> : null}
            </button>
          );
        })}
        <span className="ml-auto text-[11px] text-text-muted">
          spot <b className="text-text-primary">{inr(spot, 1)}</b> · fwd {inr(m.fp, 1)}
          {data?.vix != null ? (
            <>
              {" "}
              · VIX <b className="text-text-primary">{formatNumber(data.vix, 2)}</b>
            </>
          ) : null}
          {data?.as_of ? <AsOfBadge asOf={data.as_of} /> : null}
        </span>
      </div>

      {/* KPI strip — the prototype's 8 tiles */}
      <div className="grid grid-cols-2 gap-2.5 sm:grid-cols-4 xl:grid-cols-8">
        <Kpi label="ATM IV" value={m.atm_iv != null ? `${m.atm_iv}%` : "—"} meta={`strike ${inr(m.atm)}`} />
        <Kpi
          label="PCR (OI)"
          value={formatNumber(m.pcr, 3)}
          meta={sentiment}
          toneClass={(m.pcr ?? 0) > 1.2 ? "text-accent-green" : (m.pcr ?? 1) < 0.8 ? "text-accent-red" : undefined}
        />
        <Kpi label="Max Pain" value={inr(m.max_pain)} meta="expiry magnet" />
        <Kpi label="Call Wall" value={inr(m.call_wall)} meta="resistance" />
        <Kpi label="Put Wall" value={inr(m.put_wall)} meta="support" />
        <Kpi
          label="Net GEX"
          value={fmtCr(m.net_gex)}
          meta={`₹Cr/1% · ${(m.net_gex ?? 0) >= 0 ? "long γ" : "short γ"}`}
          toneClass={(m.net_gex ?? 0) >= 0 ? "text-accent-green" : "text-accent-red"}
        />
        <Kpi label="Net DEX" value={fmtCr(m.net_dex)} meta="₹Cr delta" />
        <Kpi
          label="Gamma Flip"
          value={m.gamma_flip != null ? inr(m.gamma_flip) : "—"}
          meta={m.gamma_flip != null ? (spot > m.gamma_flip ? "spot above" : "spot below") : "no cross"}
          toneClass={m.gamma_flip != null && spot > m.gamma_flip ? "text-accent-green" : undefined}
        />
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        {/* OI by strike */}
        <Section title="Open interest by strike" icon={<Layers size={16} />} description="Tall call bars above spot = resistance; tall put bars below = support.">
          <div className="h-64">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={chartRows} margin={{ top: 6, right: 8, bottom: 4, left: 4 }}>
                <XAxis dataKey="label" tick={TICK} minTickGap={24} />
                <YAxis tick={{ ...TICK, fontSize: 11 }} tickFormatter={(v) => lakh(v)} />
                <Tooltip contentStyle={CHART_TT} formatter={(v: number, name: string) => [lakh(v), name]} />
                <Bar dataKey="ce_oi" name="Call OI" fill="rgba(59,130,246,0.75)" />
                <Bar dataKey="pe_oi" name="Put OI" fill="rgba(245,158,11,0.75)" />
                {spotRef}
              </BarChart>
            </ResponsiveContainer>
          </div>
        </Section>

        {/* OI change by strike (gradient) */}
        <Section
          title="OI change by strike"
          icon={<TrendingUp size={16} />}
          description="Day's OI change, gradient-shaded by magnitude. Green = build, red = unwind."
          rightSlot={!hasOich ? <span className="text-[10.5px] text-text-muted">(near-expiry only)</span> : undefined}
        >
          <div className="h-64">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={chartRows} margin={{ top: 6, right: 8, bottom: 4, left: 4 }}>
                <XAxis dataKey="label" tick={TICK} minTickGap={24} />
                <YAxis tick={{ ...TICK, fontSize: 11 }} tickFormatter={(v) => lakh(v)} />
                <Tooltip contentStyle={CHART_TT} formatter={(v: number, name: string) => [`${v >= 0 ? "+" : ""}${lakh(v)}`, name]} />
                <ReferenceLine y={0} stroke="#1e2d45" />
                <Bar dataKey="ce_oich" name="Call OIΔ">
                  {chartRows.map((r, i) => (
                    <Cell key={i} fill={oichColor(r.ce_oich, maxOich) ?? "rgba(148,163,184,0.15)"} />
                  ))}
                </Bar>
                <Bar dataKey="pe_oich" name="Put OIΔ">
                  {chartRows.map((r, i) => (
                    <Cell key={i} fill={oichColor(r.pe_oich, maxOich) ?? "rgba(148,163,184,0.15)"} />
                  ))}
                </Bar>
                {spotRef}
              </BarChart>
            </ResponsiveContainer>
          </div>
        </Section>

        {/* IV smile */}
        <Section title="Volatility smile / skew" icon={<Activity size={16} />} description="OTM composite IV with per-side detail. Left-up tilt = put skew.">
          <div className="h-64">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={chartRows} margin={{ top: 6, right: 8, bottom: 4, left: 4 }}>
                <XAxis dataKey="label" tick={TICK} minTickGap={24} />
                <YAxis tick={{ ...TICK, fontSize: 11 }} domain={["auto", "auto"]} unit="%" />
                <Tooltip contentStyle={CHART_TT} formatter={(v: number, name: string) => [v != null ? `${v}%` : "—", name]} />
                <Line type="monotone" dataKey="otm_iv" name="OTM IV" stroke={ACCENT} strokeWidth={2.5} dot={false} connectNulls />
                <Line type="monotone" dataKey="ce_iv" name="Call IV" stroke="rgba(59,130,246,0.5)" strokeWidth={1} strokeDasharray="4 3" dot={false} connectNulls />
                <Line type="monotone" dataKey="pe_iv" name="Put IV" stroke="rgba(245,158,11,0.5)" strokeWidth={1} strokeDasharray="4 3" dot={false} connectNulls />
                {spotRef}
              </LineChart>
            </ResponsiveContainer>
          </div>
        </Section>

        {/* Greeks profile */}
        <Section title="Greeks profile — delta & gamma" icon={<Gauge size={16} />} description="Delta sweep 0→±1 and ATM-peaked gamma for the selected expiry.">
          <div className="h-64">
            <ResponsiveContainer width="100%" height="100%">
              <ComposedChart data={chartRows} margin={{ top: 6, right: 8, bottom: 4, left: 4 }}>
                <XAxis dataKey="label" tick={TICK} minTickGap={24} />
                <YAxis yAxisId="d" domain={[-1, 1]} tick={{ ...TICK, fontSize: 11 }} />
                <YAxis yAxisId="g" orientation="right" tick={{ ...TICK, fontSize: 11 }} tickFormatter={(v) => (v ? v.toExponential(0) : "0")} />
                <Tooltip contentStyle={CHART_TT} />
                <Bar yAxisId="g" dataKey="gamma_any" name="Gamma" fill="rgba(124,92,255,0.45)" />
                <Line yAxisId="d" type="monotone" dataKey="ce_delta" name="Call Δ" stroke={CALL} strokeWidth={2} dot={false} connectNulls />
                <Line yAxisId="d" type="monotone" dataKey="pe_delta" name="Put Δ" stroke={PUT} strokeWidth={2} dot={false} connectNulls />
                {/* This chart uses explicit axis ids, so the shared spotRef
                    (default yAxisId "0") would crash recharts' axis lookup. */}
                <ReferenceLine
                  yAxisId="d"
                  x={String(strikes[spotIdx])}
                  stroke="rgba(255,255,255,0.6)"
                  strokeDasharray="4 4"
                  label={{ value: "Spot", fill: "#cbd5e1", fontSize: 10, position: "insideTopRight" }}
                />
              </ComposedChart>
            </ResponsiveContainer>
          </div>
        </Section>

        {/* GEX by strike */}
        <Section title="GEX by strike" icon={<Magnet size={16} />} description="Dealer gamma per strike (₹Cr / 1%). Green walls dampen; spot & flip marked.">
          <div className="h-64">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={chartRows} margin={{ top: 6, right: 8, bottom: 4, left: 4 }}>
                <XAxis dataKey="label" tick={TICK} minTickGap={24} />
                <YAxis tick={{ ...TICK, fontSize: 11 }} />
                <Tooltip contentStyle={CHART_TT} formatter={(v: number) => [`${fmtCr(v)} Cr`, "GEX"]} />
                <ReferenceLine y={0} stroke="#1e2d45" />
                <Bar dataKey="gex" name="GEX">
                  {chartRows.map((r, i) => (
                    <Cell key={i} fill={r.gex >= 0 ? "rgba(39,192,138,0.8)" : "rgba(255,84,112,0.8)"} />
                  ))}
                </Bar>
                {spotRef}
                {flipRef}
              </BarChart>
            </ResponsiveContainer>
          </div>
        </Section>

        {/* DEX by strike */}
        <Section title="DEX by strike" icon={<Crosshair size={16} />} description="Delta-weighted OI notional (₹Cr) — directional hedging pressure by strike.">
          <div className="h-64">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={chartRows} margin={{ top: 6, right: 8, bottom: 4, left: 4 }}>
                <XAxis dataKey="label" tick={TICK} minTickGap={24} />
                <YAxis tick={{ ...TICK, fontSize: 11 }} />
                <Tooltip contentStyle={CHART_TT} formatter={(v: number) => [`${fmtCr(v)} Cr`, "DEX"]} />
                <ReferenceLine y={0} stroke="#1e2d45" />
                <Bar dataKey="dex" name="DEX">
                  {chartRows.map((r, i) => (
                    <Cell key={i} fill={r.dex >= 0 ? "rgba(59,130,246,0.8)" : "rgba(245,158,11,0.8)"} />
                  ))}
                </Bar>
                {spotRef}
              </BarChart>
            </ResponsiveContainer>
          </div>
        </Section>
      </div>

      {/* Market read */}
      <Section title={`Market read · ${activeExpiry ?? ""}`} icon={<ListTree size={16} />}>
        <p className="text-[12.5px] leading-relaxed text-text-secondary">
          For <b className="text-text-primary">{activeExpiry}</b> ({formatNumber(m.days, 1)}d): PCR{" "}
          <b className="text-text-primary">{formatNumber(m.pcr, 3)}</b> ({sentiment}), ATM IV{" "}
          <b className="text-text-primary">{m.atm_iv}%</b>. Max pain <b className="text-text-primary">{inr(m.max_pain)}</b>;
          heaviest call OI at <b className="text-text-primary">{inr(m.call_wall)}</b> (resistance), put OI at{" "}
          <b className="text-text-primary">{inr(m.put_wall)}</b> (support). Net GEX{" "}
          <b className={clsx((m.net_gex ?? 0) >= 0 ? "text-accent-green" : "text-accent-red")}>{fmtCr(m.net_gex)} Cr/1%</b> — dealers{" "}
          {(m.net_gex ?? 0) >= 0 ? "net long gamma (move-dampening)" : "net short gamma (move-amplifying)"}, {flipText}.
        </p>
      </Section>

      {/* Enhanced option chain */}
      <Section
        title={`Enhanced option chain · ${activeExpiry ?? ""}`}
        icon={<Sigma size={16} />}
        description="Calls · Strike · Puts. OIΔ cells gradient-shaded green (build) / red (unwind); ATM row highlighted."
      >
        <ChainTable rows={rows} spotIdx={spotIdx} maxOich={maxOich} />
      </Section>

      {/* Term structure */}
      {data?.term && (data.term.labels?.length ?? 0) > 1 ? <TermSection term={data.term} /> : null}

      {/* Intraday progression */}
      <ProgressionSection underlying={underlying} expiry={activeExpiry} />

      <p className="text-[10.5px] leading-relaxed text-text-muted">
        IV solved by Black-76 inversion per premium; Greeks, GEX (dealer convention: long calls +, short puts −, ₹Cr/1%),
        DEX (₹Cr delta-notional), gamma density (γ·OI) and the zero-gamma flip derived from it. Term PCR uses full-chain
        OI. Progression uses real 30-min (3m-resampled) candles for the ATM band; deep-ITM IV shows “—” near expiry.
      </p>
    </div>
  );
}

function ChainTable({ rows, spotIdx, maxOich }: { rows: Row[]; spotIdx: number; maxOich: number }) {
  const maxCE = Math.max(1, ...rows.map((x) => x.ce_oi));
  const maxPE = Math.max(1, ...rows.map((x) => x.pe_oi));
  const chCell = (v: number | null, key: string) => (
    <td key={key} className="px-2 py-1" style={{ background: oichColor(v, maxOich) }}>
      {v == null ? "—" : `${v >= 0 ? "+" : ""}${lakh(v)}`}
    </td>
  );
  return (
    <div className="max-h-[430px] overflow-auto rounded-lg border border-bg-border">
      <table className="w-full min-w-[980px] border-collapse text-right font-mono text-[11.5px] tabular-nums">
        <thead className="sticky top-0 z-10 bg-bg-secondary text-[10px] uppercase tracking-wide text-text-muted">
          <tr>
            <th className="px-2 py-1.5">Call OI</th>
            <th className="px-2 py-1.5">OIΔ</th>
            <th className="px-2 py-1.5">IV</th>
            <th className="px-2 py-1.5">Δ</th>
            <th className="px-2 py-1.5">θ</th>
            <th className="px-2 py-1.5">LTP</th>
            <th className="px-2 py-1.5 text-center">Strike</th>
            <th className="px-2 py-1.5">LTP</th>
            <th className="px-2 py-1.5">θ</th>
            <th className="px-2 py-1.5">Δ</th>
            <th className="px-2 py-1.5">IV</th>
            <th className="px-2 py-1.5">OIΔ</th>
            <th className="px-2 py-1.5">Put OI</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => {
            const isAtm = i === spotIdx;
            return (
              <tr key={r.strike} className={clsx("border-b border-bg-border/30", isAtm && "bg-accent-blue/10")}>
                <td className="relative px-2 py-1 text-sky-300">
                  <span
                    className="pointer-events-none absolute inset-y-0.5 right-0 rounded-sm opacity-20"
                    style={{ width: `${((r.ce_oi / maxCE) * 100).toFixed(0)}%`, background: CALL }}
                  />
                  {lakh(r.ce_oi)}
                </td>
                {chCell(r.ce_oich, "ce")}
                <td className="px-2 py-1 text-sky-300">{r.ce_iv ?? "—"}</td>
                <td className="px-2 py-1 text-sky-300">{r.ce_delta ?? "—"}</td>
                <td className="px-2 py-1 text-sky-300">{r.ce_theta ?? "—"}</td>
                <td className="px-2 py-1 font-bold text-sky-200">{r.ce_ltp ?? "—"}</td>
                <td className={clsx("px-2 py-1 text-center font-bold", isAtm ? "bg-accent-blue/25 text-white" : "bg-bg-primary/40 text-text-primary")}>
                  {inr(r.strike)}
                </td>
                <td className="px-2 py-1 font-bold text-amber-200">{r.pe_ltp ?? "—"}</td>
                <td className="px-2 py-1 text-amber-300">{r.pe_theta ?? "—"}</td>
                <td className="px-2 py-1 text-amber-300">{r.pe_delta ?? "—"}</td>
                <td className="px-2 py-1 text-amber-300">{r.pe_iv ?? "—"}</td>
                {chCell(r.pe_oich, "pe")}
                <td className="relative px-2 py-1 text-amber-300">
                  <span
                    className="pointer-events-none absolute inset-y-0.5 right-0 rounded-sm opacity-20"
                    style={{ width: `${((r.pe_oi / maxPE) * 100).toFixed(0)}%`, background: PUT }}
                  />
                  {lakh(r.pe_oi)}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function TermSection({ term }: { term: Term }) {
  const data = (term.labels ?? []).map((l, i) => ({
    label: `${l ?? "—"} (${formatNumber(term.days?.[i], 1)}d)`,
    pcr: term.pcr?.[i],
    atm_iv: term.atm_iv?.[i],
    net_gex: term.net_gex?.[i],
    tot_oi: term.tot_oi?.[i],
  }));
  return (
    <Section title="Term structure" icon={<Layers size={16} />} description="How positioning & vol vary across the expiry curve.">
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <div>
          <div className="mb-1 text-[10.5px] uppercase tracking-[0.16em] text-text-muted">ATM IV</div>
          <div className="h-48">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={data} margin={{ top: 6, right: 8, bottom: 4, left: 0 }}>
                <XAxis dataKey="label" tick={TICK} interval={0} angle={-15} height={36} />
                <YAxis tick={{ ...TICK, fontSize: 11 }} unit="%" domain={["auto", "auto"]} />
                <Tooltip contentStyle={CHART_TT} />
                <Line type="monotone" dataKey="atm_iv" stroke={ACCENT} strokeWidth={2.5} dot={{ r: 4 }} />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </div>
        <div>
          <div className="mb-1 text-[10.5px] uppercase tracking-[0.16em] text-text-muted">PCR by expiry</div>
          <div className="h-48">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={data} margin={{ top: 6, right: 8, bottom: 4, left: 0 }}>
                <XAxis dataKey="label" tick={TICK} interval={0} angle={-15} height={36} />
                <YAxis tick={{ ...TICK, fontSize: 11 }} />
                <Tooltip contentStyle={CHART_TT} />
                <ReferenceLine y={1} stroke="#8a97ad" strokeDasharray="4 4" />
                <Bar dataKey="pcr">
                  {data.map((d, i) => (
                    <Cell key={i} fill={(d.pcr ?? 0) >= 1 ? "rgba(245,158,11,0.8)" : "rgba(59,130,246,0.8)"} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>
        <div>
          <div className="mb-1 text-[10.5px] uppercase tracking-[0.16em] text-text-muted">Net GEX by expiry</div>
          <div className="h-48">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={data} margin={{ top: 6, right: 8, bottom: 4, left: 0 }}>
                <XAxis dataKey="label" tick={TICK} interval={0} angle={-15} height={36} />
                <YAxis tick={{ ...TICK, fontSize: 11 }} />
                <Tooltip contentStyle={CHART_TT} formatter={(v: number) => [`${fmtCr(v)} Cr`, "Net GEX"]} />
                <ReferenceLine y={0} stroke="#1e2d45" />
                <Bar dataKey="net_gex">
                  {data.map((d, i) => (
                    <Cell key={i} fill={(d.net_gex ?? 0) >= 0 ? "rgba(39,192,138,0.8)" : "rgba(255,84,112,0.8)"} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>
        <div>
          <div className="mb-1 text-[10.5px] uppercase tracking-[0.16em] text-text-muted">Total OI by expiry</div>
          <div className="h-48">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={data} margin={{ top: 6, right: 8, bottom: 4, left: 0 }}>
                <XAxis dataKey="label" tick={TICK} interval={0} angle={-15} height={36} />
                <YAxis tick={{ ...TICK, fontSize: 11 }} />
                <Tooltip contentStyle={CHART_TT} formatter={(v: number) => [`${formatNumber(v, 2)} Cr`, "Total OI"]} />
                <Bar dataKey="tot_oi" fill="rgba(124,92,255,0.75)" />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>
      </div>
    </Section>
  );
}

/** Δ-colored point for OI progression lines (green = OI added vs prior bucket). */
function deltaDot(series: (number | null)[], stroke: string) {
  const DotComponent = (props: { cx?: number; cy?: number; index?: number }) => {
    const { cx, cy, index } = props;
    if (cx == null || cy == null || index == null) return <g />;
    const prev = index > 0 ? series[index - 1] : null;
    const cur = series[index];
    const fill = prev == null || cur == null ? "#888" : cur - prev >= 0 ? POS : NEG;
    return <circle cx={cx} cy={cy} r={4} fill={fill} stroke={stroke} strokeWidth={1.5} />;
  };
  DotComponent.displayName = "DeltaDot";
  return DotComponent;
}

function HeatGrid({
  title,
  strikes,
  times,
  matrix,
  mode,
  regime,
  fmtCell,
}: {
  title?: string;
  strikes: number[];
  times: string[];
  matrix: (number | null)[][];
  mode: "sequential" | "diverging";
  regime?: (string | null)[];
  fmtCell: (v: number | null) => string;
}) {
  const flat = matrix.flat().filter((v): v is number => v != null && !Number.isNaN(v));
  const mx = flat.length ? Math.max(...flat) : 1;
  const mn = flat.length ? Math.min(...flat) : 0;
  const maxAbs = Math.max(1e-9, ...flat.map(Math.abs));
  const cellBg = (v: number | null): string => {
    if (v == null || Number.isNaN(v)) return "rgba(148,163,184,0.06)";
    if (mode === "diverging") {
      const a = 0.12 + 0.88 * Math.min(1, Math.abs(v) / maxAbs);
      return v >= 0 ? `rgba(39,192,138,${a})` : `rgba(255,84,112,${a})`;
    }
    const t = mx > mn ? (v - mn) / (mx - mn) : 0.5;
    return `rgb(${Math.round(20 + t * 104)},${Math.round(24 + t * 68)},${Math.round(40 + t * 215)})`;
  };
  const ordered = strikes.map((s, i) => ({ s, i })).sort((a, b) => b.s - a.s);
  const cols = `60px repeat(${times.length}, 1fr)`;
  return (
    <div>
      {title ? <div className="mb-1 text-[10.5px] uppercase tracking-[0.16em] text-text-muted">{title}</div> : null}
      <div className="space-y-0.5">
        {regime ? (
          <div className="grid items-center gap-0.5" style={{ gridTemplateColumns: cols }}>
            <div className="pr-1.5 text-right text-[9.5px] text-text-muted">regime</div>
            {regime.map((r, j) => (
              <div
                key={j}
                className="flex h-3.5 items-center justify-center rounded-sm text-[8.5px] font-bold text-bg-primary"
                style={{ background: r === "pos" ? "rgba(39,192,138,0.7)" : r === "neg" ? "rgba(255,84,112,0.7)" : "rgba(148,163,184,0.2)" }}
              >
                {r === "pos" ? "S" : r === "neg" ? "T" : "·"}
              </div>
            ))}
          </div>
        ) : null}
        <div className="grid items-center gap-0.5" style={{ gridTemplateColumns: cols }}>
          <div />
          {times.map((t) => (
            <div key={t} className="text-center text-[9.5px] text-text-muted">
              {t}
            </div>
          ))}
        </div>
        {ordered.map(({ s, i }) => (
          <div key={s} className="grid items-center gap-0.5" style={{ gridTemplateColumns: cols }}>
            <div className="pr-1.5 text-right font-mono text-[9.5px] text-text-muted">{s.toLocaleString("en-IN")}</div>
            {times.map((_, j) => {
              const v = matrix[i]?.[j] ?? null;
              return (
                <div
                  key={j}
                  title={`${s} @ ${times[j]}: ${fmtCell(v)}`}
                  className="flex h-6 items-center justify-center rounded-sm text-[8.5px] font-semibold text-text-primary/80"
                  style={{ background: cellBg(v) }}
                >
                  {fmtCell(v)}
                </div>
              );
            })}
          </div>
        ))}
      </div>
    </div>
  );
}

/** Bucket-over-bucket Δ of an OI matrix (first column = null). */
function toDelta(matrix: (number | null)[][]): (number | null)[][] {
  return matrix.map((row) =>
    row.map((v, j) => (j === 0 || v == null || row[j - 1] == null ? null : v - (row[j - 1] as number))),
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
  const [strikeSel, setStrikeSel] = useState<number | null>(null);
  const activeStrike = strikeSel ?? prog?.atm ?? prog?.strikes?.[0] ?? null;

  const gexSeries = useMemo(
    () => prog?.times.map((t, i) => ({ t, gex: prog.gex[i], regime: prog.regime[i] })) ?? [],
    [prog],
  );
  const singleSeries = useMemo(() => {
    if (!prog || activeStrike == null) {
      return { rows: [] as { t: string; ce: number | null; pe: number | null }[], ce: [] as (number | null)[], pe: [] as (number | null)[] };
    }
    const ki = prog.strikes.indexOf(activeStrike);
    const ce = prog.oi_call[ki] ?? [];
    const pe = prog.oi_put[ki] ?? [];
    return { rows: prog.times.map((t, i) => ({ t, ce: ce[i], pe: pe[i] })), ce, pe };
  }, [prog, activeStrike]);

  return (
    <Section
      title="Intraday progression · 30-min"
      icon={<Flame size={16} />}
      rightSlot={
        <button onClick={() => setOpen((v) => !v)} className="flex items-center gap-1 text-[11px] text-text-muted hover:text-text-secondary">
          {open ? "hide" : "load"}
          <ChevronDown size={14} className={clsx("transition", open && "rotate-180")} />
        </button>
      }
    >
      {!open ? (
        <div className="text-[11px] text-text-muted">
          Single-strike CE/PE OI, regime-shaded Net-GEX, gamma-density and OI-change heatmaps (history fetch — click load).
        </div>
      ) : isLoading ? (
        <div className="text-sm text-text-muted">Loading 30-min history…</div>
      ) : !data?.available || !prog ? (
        <div className="rounded-xl border border-bg-border bg-bg-primary/15 p-3 text-[12px] text-text-muted">
          Not enough 30-min option history for {expiry}. The progression needs option_premium_candles coverage across the strike band.
        </div>
      ) : (
        <div className="space-y-5">
          {data?.degraded ? (
            <div className="rounded-xl border border-amber-500/30 bg-amber-500/10 p-2 text-[11px] text-amber-300">
              Most of this grid is snapshot-derived (LTP pseudo-candles) — real 30-min candles are missing for the strike band. Treat levels as approximate.
            </div>
          ) : null}

          <div className="grid gap-4 lg:grid-cols-2">
            {/* Single-strike CE+PE OI */}
            <div>
              <div className="mb-1 flex flex-wrap items-center gap-2 text-[10.5px] uppercase tracking-[0.16em] text-text-muted">
                CE &amp; PE OI — single strike
                <select
                  value={activeStrike ?? undefined}
                  onChange={(e) => setStrikeSel(Number(e.target.value))}
                  className="rounded-md border border-bg-border bg-bg-secondary px-2 py-0.5 font-mono text-[11px] text-text-primary"
                >
                  {prog.strikes.map((s) => (
                    <option key={s} value={s}>
                      {s.toLocaleString("en-IN")}
                    </option>
                  ))}
                </select>
                <span className="normal-case tracking-normal">markers colored by Δ vs prior bucket</span>
              </div>
              <div className="h-56">
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={singleSeries.rows} margin={{ top: 6, right: 8, bottom: 4, left: 4 }}>
                    <XAxis dataKey="t" tick={TICK} minTickGap={24} />
                    <YAxis tick={{ ...TICK, fontSize: 11 }} tickFormatter={(v) => lakh(v)} />
                    <Tooltip contentStyle={CHART_TT} formatter={(v: number, name: string) => [lakh(v), name]} />
                    <Line type="monotone" dataKey="ce" name="Call OI" stroke={CALL} strokeWidth={2.5} dot={deltaDot(singleSeries.ce, CALL)} connectNulls />
                    <Line type="monotone" dataKey="pe" name="Put OI" stroke={PUT} strokeWidth={2.5} dot={deltaDot(singleSeries.pe, PUT)} connectNulls />
                  </LineChart>
                </ResponsiveContainer>
              </div>
            </div>

            {/* Net GEX progression, regime-shaded */}
            <div>
              <div className="mb-1 text-[10.5px] uppercase tracking-[0.16em] text-text-muted">
                Net GEX progression <span className="normal-case tracking-normal">— green band stabilizing · red trending</span>
              </div>
              <div className="h-56">
                <ResponsiveContainer width="100%" height="100%">
                  <ComposedChart data={gexSeries} margin={{ top: 6, right: 8, bottom: 4, left: 4 }}>
                    <XAxis dataKey="t" tick={TICK} minTickGap={24} />
                    <YAxis tick={{ ...TICK, fontSize: 11 }} tickFormatter={(v) => fmtCr(v)} />
                    <Tooltip contentStyle={CHART_TT} formatter={(v: number) => [`${fmtCr(v)} Cr`, "Net GEX"]} />
                    {gexSeries.map((d, i) =>
                      d.regime ? (
                        <ReferenceArea
                          key={i}
                          x1={d.t}
                          x2={gexSeries[Math.min(i + 1, gexSeries.length - 1)].t}
                          fill={d.regime === "pos" ? "rgba(39,192,138,0.10)" : "rgba(255,84,112,0.10)"}
                          strokeOpacity={0}
                        />
                      ) : null,
                    )}
                    <ReferenceLine y={0} stroke="rgba(138,151,173,0.6)" strokeDasharray="5 4" />
                    <Line type="monotone" dataKey="gex" stroke="#e6edf7" strokeWidth={2.5} dot={deltaDot(gexSeries.map((d) => d.gex), "#e6edf7")} connectNulls />
                  </ComposedChart>
                </ResponsiveContainer>
              </div>
            </div>
          </div>

          {/* Gamma density heat with regime row */}
          <HeatGrid
            title="Gamma density progression (strike × time, regime-marked)"
            strikes={prog.strikes}
            times={prog.times}
            matrix={prog.gdens}
            mode="sequential"
            regime={prog.regime}
            fmtCell={(v) => (v == null ? "·" : v.toFixed(2))}
          />

          {/* Signed Net-GEX heat (spec L71) */}
          {prog.netgex?.length ? (
            <HeatGrid
              title="Net GEX (strike × time, signed)"
              strikes={prog.strikes}
              times={prog.times}
              matrix={prog.netgex}
              mode="diverging"
              fmtCell={(v) => (v == null ? "·" : Math.round(v).toString())}
            />
          ) : null}

          {/* OI change progression heatmaps */}
          <div className="flex flex-wrap gap-5">
            <div className="min-w-[340px] flex-1">
              <HeatGrid
                title="Call OI Δ (bucket-over-bucket)"
                strikes={prog.strikes}
                times={prog.times}
                matrix={toDelta(prog.oi_call)}
                mode="diverging"
                fmtCell={(v) => (v == null ? "·" : `${v >= 0 ? "+" : ""}${Math.abs(v) >= 100000 ? lakh(v) : `${Math.round(v / 1000)}k`}`)}
              />
            </div>
            <div className="min-w-[340px] flex-1">
              <HeatGrid
                title="Put OI Δ (bucket-over-bucket)"
                strikes={prog.strikes}
                times={prog.times}
                matrix={toDelta(prog.oi_put)}
                mode="diverging"
                fmtCell={(v) => (v == null ? "·" : `${v >= 0 ? "+" : ""}${Math.abs(v) >= 100000 ? lakh(v) : `${Math.round(v / 1000)}k`}`)}
              />
            </div>
          </div>
        </div>
      )}
    </Section>
  );
}

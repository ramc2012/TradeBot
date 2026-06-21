"use client";

/**
 * Option-chain analytics panel for the v1 directional-options workspace.
 *
 * Mirrors the v2 desk-ui panel but uses local format helpers (no
 * `desk-ui` module in v1). Pulls from
 * `/api/directional-options/chain-analytics?underlying=…&expiry=…`.
 *
 * Shows everything the RL policy now consumes as chain context:
 * PCR (OI/volume/Δ), ATM IV, IV skew (25-delta), max-pain, GEX, DEX
 * calls/puts/net, ATM CE/PE activity, chain-wide OI build matrix,
 * gamma curve around ATM (recharts stacked bar), top-3 OI strikes
 * per side.
 */
import { clsx } from "clsx";
import { useQuery } from "@tanstack/react-query";
import {
  Bar,
  BarChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { Activity, Layers, TrendingDown, TrendingUp } from "lucide-react";

import { api as apiClient } from "@/lib/api";

const REFRESH_MS = 15_000;

type ChainAnalytics = {
  available?: boolean;
  underlying?: string;
  expiry?: string | null;
  spot?: number | null;
  atm_strike?: number | null;
  atm_iv?: number | null;
  pcr_oi?: number | null;
  pcr_volume?: number | null;
  pcr_oi_change?: number | null;
  iv_skew_25d?: number | null;
  iv_skew_25d_norm?: number | null;
  risk_reversal_25d?: number | null;
  iv_25d_call?: number | null;
  iv_25d_put?: number | null;
  gex_total?: number | null;
  dealer_gex_total?: number | null;
  dex_calls?: number | null;
  dex_puts?: number | null;
  dex_net?: number | null;
  total_ce_oi?: number | null;
  total_pe_oi?: number | null;
  total_ce_oi_change?: number | null;
  total_pe_oi_change?: number | null;
  atm_call_oi_change?: number | null;
  atm_put_oi_change?: number | null;
  atm_call_ltp_change_pct?: number | null;
  atm_put_ltp_change_pct?: number | null;
  max_pain?: number | null;
  top_ce_oi?: Array<{ strike: number; oi: number; moneyness_pct: number }>;
  top_pe_oi?: Array<{ strike: number; oi: number; moneyness_pct: number }>;
  oi_build_ce?: Record<string, number>;
  oi_build_pe?: Record<string, number>;
  gamma_curve?: Array<{
    strike: number;
    moneyness_pct: number;
    ce_gamma_oi: number;
    pe_gamma_oi: number;
    total_gamma_oi: number;
  }>;
  key_levels?: {
    call_wall?: { strike: number; distance_pct: number; net_gamma_exposure: number } | null;
    put_wall?: { strike: number; distance_pct: number; net_gamma_exposure: number } | null;
    abs_gamma?: { strike: number; distance_pct: number; net_gamma_exposure: number } | null;
    zero_gamma?: number | null;
    dealer_gex_total?: number | null;
    gamma_regime?: string | null;
  };
  trace_exposures?: Array<{
    strike: number;
    moneyness_pct: number;
    net_delta_exposure: number;
    net_gamma_exposure: number;
    net_vanna_exposure: number;
    net_charm_exposure: number;
    net_volga_exposure: number;
  }>;
  unusual_activity?: Array<{
    strike: number;
    option_type: string;
    oi_change: number;
    oi_change_pct?: number | null;
    ltp_change_pct?: number | null;
    volume_to_oi: number;
    score: number;
    flags: string[];
  }>;
  expiry_state?: {
    days_to_expiry?: number | null;
    is_expiry_day?: boolean;
    expiry_mode?: string;
    theta_clock_pct?: number | null;
  };
};

function fmtNumber(value?: number | null, digits = 2): string {
  if (value == null || Number.isNaN(value)) return "—";
  return value.toFixed(digits);
}

function fmtPct(value?: number | null, digits = 2): string {
  if (value == null || Number.isNaN(value)) return "—";
  return `${(value * 100).toFixed(digits)}%`;
}

function compact(n: number | null | undefined): string {
  if (n == null || Number.isNaN(n)) return "—";
  const abs = Math.abs(n);
  if (abs >= 1e10) return `${(n / 1e10).toFixed(2)} kCr`;
  if (abs >= 1e7) return `${(n / 1e7).toFixed(2)} Cr`;
  if (abs >= 1e5) return `${(n / 1e5).toFixed(2)} L`;
  if (abs >= 1e3) return `${(n / 1e3).toFixed(1)} K`;
  return n.toFixed(0);
}

function tone(value?: number | null): string {
  if (value == null || Number.isNaN(value)) return "text-text-muted";
  if (value > 0) return "text-accent-green";
  if (value < 0) return "text-accent-red";
  return "text-text-secondary";
}

function pcrTone(pcr: number | null | undefined): string {
  if (pcr == null) return "text-text-muted";
  if (pcr > 1.2) return "text-accent-green";
  if (pcr < 0.8) return "text-accent-red";
  return "text-text-secondary";
}

function formatStrike(value?: number | null): string {
  return value == null || Number.isNaN(value) ? "—" : fmtNumber(value, 0);
}

function fmtPctFromPercent(value?: number | null, digits = 1): string {
  if (value == null || Number.isNaN(value)) return "—";
  return `${value.toFixed(digits)}%`;
}

function levelDetail(level?: { distance_pct?: number | null; net_gamma_exposure?: number | null } | null): string | undefined {
  if (!level) return undefined;
  return `${fmtPct(level.distance_pct, 2)} · ${compact(level.net_gamma_exposure)}`;
}

function MetricTile({
  label,
  value,
  detail,
  color,
}: {
  label: string;
  value: string;
  detail?: string;
  color?: string;
}) {
  return (
    <div className="rounded-2xl border border-bg-border bg-bg-secondary/35 px-4 py-3">
      <div className="text-[11px] uppercase tracking-[0.16em] text-text-muted">{label}</div>
      <div className={clsx("mt-2 font-mono text-lg font-semibold text-text-primary", color)}>
        {value}
      </div>
      {detail ? <div className="mt-1 text-[11px] text-text-muted">{detail}</div> : null}
    </div>
  );
}

function Section({
  title,
  icon,
  rightSlot,
  children,
}: {
  title: string;
  icon?: React.ReactNode;
  rightSlot?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section className="rounded-[26px] border border-bg-border bg-bg-secondary/24 p-5">
      <div className="mb-3 flex items-start justify-between gap-3">
        <div className="flex items-center gap-2 text-sm font-semibold text-text-primary">
          {icon}
          {title}
        </div>
        {rightSlot}
      </div>
      {children}
    </section>
  );
}

export default function OptionAnalyticsPanel({
  underlying,
  expiry,
}: {
  underlying: string;
  expiry?: string | null;
}) {
  const { data, isLoading } = useQuery({
    queryKey: ["directional-options-chain-analytics", underlying, expiry || ""],
    queryFn: async () =>
      (
        await apiClient.get("/api/directional-options/chain-analytics", {
          params: { underlying, expiry: expiry || undefined },
        })
      ).data as ChainAnalytics,
    refetchInterval: REFRESH_MS,
    refetchOnWindowFocus: false,
  });

  if (isLoading) {
    return (
      <Section title="Option-chain analytics" icon={<Layers size={16} />}>
        <div className="text-sm text-text-muted">Loading chain…</div>
      </Section>
    );
  }

  if (!data?.available) {
    return (
      <Section title="Option-chain analytics" icon={<Layers size={16} />}>
        <div className="rounded-2xl border border-bg-border bg-bg-primary/16 p-4 text-sm text-text-muted">
          No chain cached for <span className="font-mono">{underlying}</span>
          {expiry ? ` · ${expiry}` : ""}. Chain analytics populate once the
          broker websocket has filled the option chain. Hit the Market &gt;
          Option Chain page once for this underlying to prime it.
        </div>
      </Section>
    );
  }

  const dexDenom = Math.max(Math.abs(data.dex_calls ?? 0) + Math.abs(data.dex_puts ?? 0), 1);
  const dexRatio = (data.dex_net ?? 0) / dexDenom;
  const totalCeOiChg = data.total_ce_oi_change ?? 0;
  const totalPeOiChg = data.total_pe_oi_change ?? 0;
  const keyLevels = data.key_levels || {};
  const traceRows = [...(data.trace_exposures || [])]
    .sort((a, b) => Math.abs(a.moneyness_pct) - Math.abs(b.moneyness_pct))
    .slice(0, 9);
  const unusualRows = data.unusual_activity || [];
  const dealerGex = data.dealer_gex_total ?? keyLevels.dealer_gex_total ?? null;

  return (
    <div className="space-y-5">
      <Section
        title={`Option-chain analytics · ${data.underlying || underlying}`}
        icon={<Layers size={16} />}
        rightSlot={
          <span className="text-[11px] text-text-muted">
            {data.expiry ? `expiry ${data.expiry}` : ""}
            {data.atm_strike ? ` · ATM ${data.atm_strike}` : ""}
          </span>
        }
      >
        <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
          <MetricTile
            label="PCR (OI)"
            value={fmtNumber(data.pcr_oi, 2)}
            color={pcrTone(data.pcr_oi)}
            detail={
              data.pcr_oi_change != null
                ? `Δ ${(data.pcr_oi_change >= 0 ? "+" : "") + data.pcr_oi_change.toFixed(3)}`
                : undefined
            }
          />
          <MetricTile label="PCR (Vol)" value={fmtNumber(data.pcr_volume, 2)} color={pcrTone(data.pcr_volume)} />
          <MetricTile
            label="ATM IV"
            value={data.atm_iv != null ? fmtPct(data.atm_iv, 2) : "—"}
            detail={
              data.iv_skew_25d_norm != null
                ? `skew ${(data.iv_skew_25d_norm * 100).toFixed(1)}%`
                : undefined
            }
          />
          <MetricTile
            label="IV skew (25Δ)"
            value={
              data.iv_skew_25d != null
                ? (data.iv_skew_25d >= 0 ? "+" : "") + data.iv_skew_25d.toFixed(2)
                : "—"
            }
            color={tone(data.iv_skew_25d)}
            detail="put IV − call IV"
          />
          <MetricTile
            label="Max pain"
            value={data.max_pain != null ? fmtNumber(data.max_pain, 0) : "—"}
            detail={
              data.spot != null && data.max_pain != null
                ? `${(((data.max_pain - data.spot) / data.spot) * 100).toFixed(2)}% from spot`
                : undefined
            }
          />
          <MetricTile label="GEX" value={compact(data.gex_total)} color={tone(data.gex_total)} detail="γ × OI × side" />
        </div>
      </Section>

      <Section
        title="Key levels"
        icon={<TrendingDown size={16} />}
        rightSlot={
          <span className={clsx("text-[11px] uppercase tracking-[0.14em]", tone(dealerGex))}>
            {(keyLevels.gamma_regime || "unknown").replaceAll("_", " ")}
            {data.expiry_state?.expiry_mode ? ` · ${data.expiry_state.expiry_mode.replaceAll("_", " ")}` : ""}
          </span>
        }
      >
        <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
          <MetricTile
            label="Dealer GEX"
            value={compact(dealerGex)}
            color={tone(dealerGex)}
            detail={dealerGex != null && dealerGex >= 0 ? "pinning" : dealerGex != null ? "trend amp" : undefined}
          />
          <MetricTile
            label="Gamma flip"
            value={formatStrike(keyLevels.zero_gamma)}
            detail={keyLevels.zero_gamma && data.spot ? `${fmtPct((keyLevels.zero_gamma - data.spot) / data.spot, 2)} from spot` : undefined}
          />
          <MetricTile label="Call wall" value={formatStrike(keyLevels.call_wall?.strike)} detail={levelDetail(keyLevels.call_wall)} color="text-accent-green" />
          <MetricTile label="Put wall" value={formatStrike(keyLevels.put_wall?.strike)} detail={levelDetail(keyLevels.put_wall)} color="text-accent-red" />
          <MetricTile label="Abs gamma" value={formatStrike(keyLevels.abs_gamma?.strike)} detail={levelDetail(keyLevels.abs_gamma)} />
          <MetricTile
            label="Theta clock"
            value={data.expiry_state?.theta_clock_pct != null ? fmtPct(data.expiry_state.theta_clock_pct, 0) : "—"}
            detail={data.expiry_state?.days_to_expiry != null ? `${data.expiry_state.days_to_expiry} DTE` : undefined}
            color={data.expiry_state?.is_expiry_day ? "text-accent-amber" : undefined}
          />
        </div>
      </Section>

      <div className="grid gap-5 lg:grid-cols-2">
        <Section title="Delta exposure (DEX)" icon={<Activity size={16} />}>
          <div className="grid grid-cols-3 gap-3">
            <MetricTile label="Calls" value={compact(data.dex_calls)} color="text-accent-green" />
            <MetricTile label="Puts" value={compact(data.dex_puts)} color="text-accent-red" />
            <MetricTile
              label="Net (ratio)"
              value={(dexRatio >= 0 ? "+" : "") + dexRatio.toFixed(2)}
              color={tone(dexRatio)}
              detail="(C − P) / (|C| + |P|)"
            />
          </div>
          <div className="mt-4 grid grid-cols-2 gap-3 text-[11px] text-text-muted">
            <div className="rounded-xl border border-bg-border bg-bg-primary/16 p-3">
              <div className="uppercase tracking-[0.14em]">CE OI / Δ</div>
              <div className="mt-1 font-mono text-text-primary">{compact(data.total_ce_oi)}</div>
              <div className={clsx("mt-0.5 font-mono", tone(totalCeOiChg))}>Δ {compact(totalCeOiChg)}</div>
            </div>
            <div className="rounded-xl border border-bg-border bg-bg-primary/16 p-3">
              <div className="uppercase tracking-[0.14em]">PE OI / Δ</div>
              <div className="mt-1 font-mono text-text-primary">{compact(data.total_pe_oi)}</div>
              <div className={clsx("mt-0.5 font-mono", tone(totalPeOiChg))}>Δ {compact(totalPeOiChg)}</div>
            </div>
          </div>
        </Section>

        <Section title="ATM activity (this bar)" icon={<TrendingUp size={16} />}>
          <div className="grid grid-cols-2 gap-3">
            <MetricTile
              label="ATM CE  Δ LTP%"
              value={
                data.atm_call_ltp_change_pct != null
                  ? (data.atm_call_ltp_change_pct >= 0 ? "+" : "") +
                    data.atm_call_ltp_change_pct.toFixed(2) +
                    "%"
                  : "—"
              }
              color={tone(data.atm_call_ltp_change_pct)}
              detail={`Δ OI ${compact(data.atm_call_oi_change)}`}
            />
            <MetricTile
              label="ATM PE  Δ LTP%"
              value={
                data.atm_put_ltp_change_pct != null
                  ? (data.atm_put_ltp_change_pct >= 0 ? "+" : "") +
                    data.atm_put_ltp_change_pct.toFixed(2) +
                    "%"
                  : "—"
              }
              color={tone(data.atm_put_ltp_change_pct)}
              detail={`Δ OI ${compact(data.atm_put_oi_change)}`}
            />
          </div>
          {(data.oi_build_ce || data.oi_build_pe) && (
            <div className="mt-4">
              <div className="mb-1 text-[10.5px] uppercase tracking-[0.16em] text-text-muted">
                OI build classification (chain-wide)
              </div>
              <table className="w-full text-[11.5px]">
                <thead className="text-[10px] uppercase tracking-wide text-text-muted">
                  <tr>
                    <th className="text-left pb-1">Side</th>
                    <th>Long Bld</th>
                    <th>Short Bld</th>
                    <th>Long Unw</th>
                    <th>Short Cov</th>
                  </tr>
                </thead>
                <tbody>
                  <tr className="border-t border-bg-border/30">
                    <td className="py-1 text-accent-green font-semibold">CE</td>
                    <td className="text-center font-mono">{data.oi_build_ce?.long_buildup ?? 0}</td>
                    <td className="text-center font-mono">{data.oi_build_ce?.short_buildup ?? 0}</td>
                    <td className="text-center font-mono">{data.oi_build_ce?.long_unwind ?? 0}</td>
                    <td className="text-center font-mono">{data.oi_build_ce?.short_cover ?? 0}</td>
                  </tr>
                  <tr className="border-t border-bg-border/30">
                    <td className="py-1 text-accent-red font-semibold">PE</td>
                    <td className="text-center font-mono">{data.oi_build_pe?.long_buildup ?? 0}</td>
                    <td className="text-center font-mono">{data.oi_build_pe?.short_buildup ?? 0}</td>
                    <td className="text-center font-mono">{data.oi_build_pe?.long_unwind ?? 0}</td>
                    <td className="text-center font-mono">{data.oi_build_pe?.short_cover ?? 0}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          )}
        </Section>
      </div>

      {data.gamma_curve && data.gamma_curve.length > 0 && (
        <Section title="Gamma curve · γ × OI around ATM" icon={<TrendingDown size={16} />}>
          <div className="h-64">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={data.gamma_curve}>
                <XAxis dataKey="strike" tick={{ fill: "#94a3b8", fontSize: 11 }} />
                <YAxis tick={{ fill: "#94a3b8", fontSize: 11 }} tickFormatter={(v) => compact(v)} />
                <Tooltip
                  formatter={(value: number, name: string) => [
                    compact(value),
                    name === "ce_gamma_oi" ? "CE γ·OI" : "PE γ·OI",
                  ]}
                  labelFormatter={(strike) => `Strike ${strike}`}
                  contentStyle={{ background: "#0f1724", border: "1px solid #1e2d45" }}
                />
                {data.atm_strike ? (
                  <ReferenceLine
                    x={data.atm_strike}
                    stroke="#3b82f6"
                    strokeDasharray="3 3"
                    label={{ value: "ATM", fill: "#94a3b8", fontSize: 10 }}
                  />
                ) : null}
                <Bar dataKey="ce_gamma_oi" stackId="g" fill="#00d4a3" />
                <Bar dataKey="pe_gamma_oi" stackId="g" fill="#ff4757" />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </Section>
      )}

      {traceRows.length > 0 && (
        <Section title="TRACE exposures" icon={<Activity size={16} />}>
          <div className="overflow-x-auto">
            <table className="min-w-full text-[11.5px]">
              <thead className="text-[10px] uppercase tracking-wide text-text-muted">
                <tr className="border-b border-bg-border/40">
                  <th className="px-2 py-1.5 text-left">Strike</th>
                  <th className="px-2 py-1.5 text-right">Δ exp</th>
                  <th className="px-2 py-1.5 text-right">Γ exp</th>
                  <th className="px-2 py-1.5 text-right">Vanna</th>
                  <th className="px-2 py-1.5 text-right">Charm</th>
                  <th className="px-2 py-1.5 text-right">Volga</th>
                  <th className="px-2 py-1.5 text-right">% spot</th>
                </tr>
              </thead>
              <tbody>
                {traceRows.map((r) => (
                  <tr key={`trace-${r.strike}`} className="border-b border-bg-border/20">
                    <td className="px-2 py-1.5 font-mono text-text-primary">{formatStrike(r.strike)}</td>
                    <td className={clsx("px-2 py-1.5 text-right font-mono", tone(r.net_delta_exposure))}>{compact(r.net_delta_exposure)}</td>
                    <td className={clsx("px-2 py-1.5 text-right font-mono", tone(r.net_gamma_exposure))}>{compact(r.net_gamma_exposure)}</td>
                    <td className={clsx("px-2 py-1.5 text-right font-mono", tone(r.net_vanna_exposure))}>{compact(r.net_vanna_exposure)}</td>
                    <td className={clsx("px-2 py-1.5 text-right font-mono", tone(r.net_charm_exposure))}>{compact(r.net_charm_exposure)}</td>
                    <td className={clsx("px-2 py-1.5 text-right font-mono", tone(r.net_volga_exposure))}>{compact(r.net_volga_exposure)}</td>
                    <td className={clsx("px-2 py-1.5 text-right font-mono", tone(r.moneyness_pct))}>{fmtPct(r.moneyness_pct, 2)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Section>
      )}

      {unusualRows.length > 0 && (
        <Section title="Unusual activity" icon={<TrendingUp size={16} />}>
          <div className="overflow-x-auto">
            <table className="min-w-full text-[11.5px]">
              <thead className="text-[10px] uppercase tracking-wide text-text-muted">
                <tr className="border-b border-bg-border/40">
                  <th className="px-2 py-1.5 text-left">Strike</th>
                  <th className="px-2 py-1.5 text-left">Side</th>
                  <th className="px-2 py-1.5 text-right">Vol/OI</th>
                  <th className="px-2 py-1.5 text-right">ΔOI</th>
                  <th className="px-2 py-1.5 text-right">ΔLTP</th>
                  <th className="px-2 py-1.5 text-left">Flags</th>
                </tr>
              </thead>
              <tbody>
                {unusualRows.map((r) => (
                  <tr key={`ua-${r.option_type}-${r.strike}`} className="border-b border-bg-border/20">
                    <td className="px-2 py-1.5 font-mono text-text-primary">{formatStrike(r.strike)}</td>
                    <td className={clsx("px-2 py-1.5 font-semibold", r.option_type === "CE" ? "text-accent-green" : "text-accent-red")}>{r.option_type}</td>
                    <td className="px-2 py-1.5 text-right font-mono">{fmtNumber(r.volume_to_oi, 2)}</td>
                    <td className={clsx("px-2 py-1.5 text-right font-mono", tone(r.oi_change))}>
                      {compact(r.oi_change)}
                      {r.oi_change_pct != null ? ` (${fmtPctFromPercent(r.oi_change_pct, 1)})` : ""}
                    </td>
                    <td className={clsx("px-2 py-1.5 text-right font-mono", tone(r.ltp_change_pct))}>
                      {r.ltp_change_pct != null ? fmtPctFromPercent(r.ltp_change_pct, 1) : "—"}
                    </td>
                    <td className="px-2 py-1.5 text-text-muted">{r.flags.join(" · ") || fmtNumber(r.score, 1)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Section>
      )}

      {data.top_ce_oi && data.top_pe_oi && (
        <div className="grid gap-5 lg:grid-cols-2">
          <Section title="Top CE strikes by OI">
            <table className="w-full text-[12px]">
              <thead className="text-[10.5px] uppercase tracking-wide text-text-muted">
                <tr className="border-b border-bg-border/40">
                  <th className="px-2 py-1.5 text-left">Strike</th>
                  <th className="px-2 py-1.5 text-right">OI</th>
                  <th className="px-2 py-1.5 text-right">% from spot</th>
                </tr>
              </thead>
              <tbody>
                {data.top_ce_oi.map((r) => (
                  <tr key={`ce-${r.strike}`} className="border-b border-bg-border/20">
                    <td className="px-2 py-1.5 font-mono">{r.strike}</td>
                    <td className="px-2 py-1.5 text-right font-mono">{compact(r.oi)}</td>
                    <td className={clsx("px-2 py-1.5 text-right font-mono", tone(r.moneyness_pct))}>
                      {((r.moneyness_pct ?? 0) * 100).toFixed(2)}%
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Section>
          <Section title="Top PE strikes by OI">
            <table className="w-full text-[12px]">
              <thead className="text-[10.5px] uppercase tracking-wide text-text-muted">
                <tr className="border-b border-bg-border/40">
                  <th className="px-2 py-1.5 text-left">Strike</th>
                  <th className="px-2 py-1.5 text-right">OI</th>
                  <th className="px-2 py-1.5 text-right">% from spot</th>
                </tr>
              </thead>
              <tbody>
                {data.top_pe_oi.map((r) => (
                  <tr key={`pe-${r.strike}`} className="border-b border-bg-border/20">
                    <td className="px-2 py-1.5 font-mono">{r.strike}</td>
                    <td className="px-2 py-1.5 text-right font-mono">{compact(r.oi)}</td>
                    <td className={clsx("px-2 py-1.5 text-right font-mono", tone(-(r.moneyness_pct ?? 0)))}>
                      {((r.moneyness_pct ?? 0) * 100).toFixed(2)}%
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Section>
        </div>
      )}
    </div>
  );
}

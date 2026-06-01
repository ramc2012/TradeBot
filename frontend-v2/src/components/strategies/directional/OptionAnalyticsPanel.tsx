"use client";

/**
 * Option-chain analytics panel — the new rich-data view the RL policy
 * is now consuming.
 *
 * Shows everything the bandit sees as chain context: PCR, ATM IV,
 * IV skew (25-delta), max pain, DEX, GEX, gamma curve around ATM, top
 * OI strikes on each side, OI-build classification. All metrics
 * tolerate nulls and degrade to "—" when the chain isn't cached.
 */
import { clsx } from "clsx";
import { useQuery } from "@tanstack/react-query";
import {
  Bar,
  BarChart,
  Cell,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { Activity, Layers, TrendingDown, TrendingUp } from "lucide-react";

import {
  MetricTile,
  REFRESH_MS,
  Section,
  StatusBadge,
  formatNumber,
  formatPct,
  tone,
} from "@/components/desk-ui";
import { api as apiClient } from "@/lib/api";

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
  gex_total?: number | null;
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
};

function compact(n: number | null | undefined): string {
  if (n == null || Number.isNaN(n)) return "—";
  const abs = Math.abs(n);
  if (abs >= 1e10) return `${(n / 1e10).toFixed(2)} kCr`;
  if (abs >= 1e7) return `${(n / 1e7).toFixed(2)} Cr`;
  if (abs >= 1e5) return `${(n / 1e5).toFixed(2)} L`;
  if (abs >= 1e3) return `${(n / 1e3).toFixed(1)} K`;
  return n.toFixed(0);
}

function pcrTone(pcr: number | null | undefined): string {
  // PCR > 1.2 = bullish (more puts, contrarian); PCR < 0.8 = bearish.
  if (pcr == null) return "text-text-muted";
  if (pcr > 1.2) return "text-accent-green";
  if (pcr < 0.8) return "text-accent-red";
  return "text-text-secondary";
}

export default function OptionAnalyticsPanel({
  underlying,
  expiry,
}: {
  underlying: string;
  expiry?: string | null;
}) {
  const { data, isLoading } = useQuery({
    queryKey: ["directional", "chain-analytics", underlying, expiry || ""],
    queryFn: async () =>
      (
        await apiClient.get("/api/directional-options/chain-analytics", {
          params: { underlying, expiry: expiry || undefined },
        })
      ).data as ChainAnalytics,
    refetchInterval: REFRESH_MS.live,
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
        <div className="rounded-xl border border-bg-border bg-bg-primary/15 p-3 text-sm text-text-muted">
          No chain cached for <span className="font-mono">{underlying}</span>
          {expiry ? ` · ${expiry}` : ""}. Chain analytics show up once the
          broker websocket has filled the option chain.
        </div>
      </Section>
    );
  }

  const pcrOi = data.pcr_oi;
  const pcrOiChg = data.pcr_oi_change;
  const ivSkewNorm = data.iv_skew_25d_norm;
  const dexDenom = Math.max(Math.abs(data.dex_calls ?? 0) + Math.abs(data.dex_puts ?? 0), 1);
  const dexRatio = (data.dex_net ?? 0) / dexDenom;
  const totalCeOiChg = data.total_ce_oi_change ?? 0;
  const totalPeOiChg = data.total_pe_oi_change ?? 0;

  return (
    <div className="space-y-4">
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
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-6">
          <MetricTile
            label="PCR (OI)"
            value={formatNumber(pcrOi, 2)}
            color={pcrTone(pcrOi)}
            detail={pcrOiChg != null ? `Δ ${(pcrOiChg >= 0 ? "+" : "") + pcrOiChg.toFixed(3)}` : ""}
          />
          <MetricTile
            label="PCR (Vol)"
            value={formatNumber(data.pcr_volume, 2)}
            color={pcrTone(data.pcr_volume)}
          />
          <MetricTile
            label="ATM IV"
            value={data.atm_iv != null ? formatPct(data.atm_iv, 2) : "—"}
            detail={ivSkewNorm != null ? `skew ${(ivSkewNorm * 100).toFixed(1)}%` : ""}
          />
          <MetricTile
            label="IV skew (25Δ)"
            value={data.iv_skew_25d != null ? (data.iv_skew_25d >= 0 ? "+" : "") + data.iv_skew_25d.toFixed(2) : "—"}
            color={tone(data.iv_skew_25d)}
            detail="put IV − call IV"
          />
          <MetricTile
            label="Max pain"
            value={data.max_pain != null ? formatNumber(data.max_pain, 0) : "—"}
            detail={data.spot != null && data.max_pain != null ? `${(((data.max_pain - data.spot) / data.spot) * 100).toFixed(2)}% from spot` : ""}
          />
          <MetricTile
            label="GEX"
            value={compact(data.gex_total)}
            color={tone(data.gex_total)}
            detail="γ × OI × side"
          />
        </div>
      </Section>

      <div className="grid gap-4 lg:grid-cols-2">
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
          <div className="mt-3 grid grid-cols-2 gap-2 text-[11px] text-text-muted">
            <div className="rounded border border-bg-border bg-bg-primary/10 p-2">
              <div className="uppercase tracking-[0.14em]">CE OI / Δ</div>
              <div className="mt-1 font-mono text-text-primary">{compact(data.total_ce_oi)}</div>
              <div className={clsx("mt-0.5 font-mono", tone(totalCeOiChg))}>Δ {compact(totalCeOiChg)}</div>
            </div>
            <div className="rounded border border-bg-border bg-bg-primary/10 p-2">
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
              value={data.atm_call_ltp_change_pct != null ? (data.atm_call_ltp_change_pct >= 0 ? "+" : "") + data.atm_call_ltp_change_pct.toFixed(2) + "%" : "—"}
              color={tone(data.atm_call_ltp_change_pct)}
              detail={`Δ OI ${compact(data.atm_call_oi_change)}`}
            />
            <MetricTile
              label="ATM PE  Δ LTP%"
              value={data.atm_put_ltp_change_pct != null ? (data.atm_put_ltp_change_pct >= 0 ? "+" : "") + data.atm_put_ltp_change_pct.toFixed(2) + "%" : "—"}
              color={tone(data.atm_put_ltp_change_pct)}
              detail={`Δ OI ${compact(data.atm_put_oi_change)}`}
            />
          </div>
          {data.oi_build_ce || data.oi_build_pe ? (
            <div className="mt-3">
              <div className="mb-1 text-[10.5px] uppercase tracking-[0.16em] text-text-muted">
                OI build classification (chain-wide)
              </div>
              <table className="w-full text-[11px]">
                <thead className="text-[10px] uppercase tracking-wide text-text-muted">
                  <tr><th className="text-left pb-1">Side</th><th>Long Bld</th><th>Short Bld</th><th>Long Unw</th><th>Short Cov</th></tr>
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
          ) : null}
        </Section>
      </div>

      {data.gamma_curve && data.gamma_curve.length > 0 ? (
        <Section title="Gamma curve · γ × OI around ATM" icon={<TrendingDown size={16} />}>
          <div className="h-56">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={data.gamma_curve}>
                <XAxis dataKey="strike" tick={{ fill: "#94a3b8", fontSize: 11 }} />
                <YAxis tick={{ fill: "#94a3b8", fontSize: 11 }} tickFormatter={(v) => compact(v)} />
                <Tooltip
                  formatter={(value: number, name: string) => [compact(value), name === "ce_gamma_oi" ? "CE γ·OI" : "PE γ·OI"]}
                  labelFormatter={(strike) => `Strike ${strike}`}
                  contentStyle={{ background: "#0f1724", border: "1px solid #1e2d45" }}
                />
                {data.spot ? <ReferenceLine x={data.atm_strike ?? undefined} stroke="#3b82f6" strokeDasharray="3 3" label={{ value: "ATM", fill: "#94a3b8", fontSize: 10 }} /> : null}
                <Bar dataKey="ce_gamma_oi" stackId="g" fill="#00d4a3" />
                <Bar dataKey="pe_gamma_oi" stackId="g" fill="#ff4757" />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </Section>
      ) : null}

      {data.top_ce_oi && data.top_pe_oi ? (
        <div className="grid gap-4 lg:grid-cols-2">
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
      ) : null}
    </div>
  );
}

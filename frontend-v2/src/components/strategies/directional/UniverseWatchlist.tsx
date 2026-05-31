"use client";

/**
 * Universe watchlist — one row per symbol, always rendered, click to
 * select. Slimmer than v1's MultiSymbolWatch — uses desk-ui formatters
 * + tones instead of local helpers, and shares useQueries result shape.
 */
import { clsx } from "clsx";
import { useQueries } from "@tanstack/react-query";
import { Activity, Eye } from "lucide-react";

import {
  REFRESH_MS,
  Section,
  StatusBadge,
  directionTone,
  formatNumber,
  formatPct,
  regimeTone,
} from "@/components/desk-ui";
import { api as apiClient } from "@/lib/api";

type Snap = {
  spot_price?: number | null;
  feature_snapshot?: { adx?: number; ema_spread_pct?: number; rv_percentile?: number } | null;
  regime?: { label?: string; confidence?: number } | null;
  signal?: { direction?: string; confidence?: number } | null;
  policy?: { act?: boolean; size_multiplier?: number; sampled_value?: number } | null;
  selected_contract?: { strike?: number; option_type?: string; delta?: number; contract_score?: number } | null;
  contract_candidates?: unknown[];
  data_status?: { execution_ready?: boolean; degraded_reason?: string | null };
};

export default function UniverseWatchlist({
  symbols,
  timeframe,
  lookback,
  selected,
  onSelect,
}: {
  symbols: string[];
  timeframe: string;
  lookback: number;
  selected?: string;
  onSelect?: (s: string) => void;
}) {
  const queries = useQueries({
    queries: symbols.map((symbol) => ({
      queryKey: ["directional", "watch", symbol, timeframe, lookback],
      queryFn: async () =>
        (
          await apiClient.get("/api/directional-options/live-snapshot", {
            params: { underlying: symbol, timeframe, lookback_sessions: lookback },
          })
        ).data?.snapshot as Snap,
      refetchInterval: REFRESH_MS.live,
      refetchOnWindowFocus: false,
    })),
  });

  return (
    <Section
      title="Universe watchlist"
      icon={<Eye size={16} />}
      description="What the engine is watching — features, regime, signal, policy verdict per symbol. Click a row to re-centre the desk on that symbol."
      rightSlot={
        <span className="inline-flex items-center gap-1.5 text-[11px] text-text-muted">
          <Activity size={12} />
          {REFRESH_MS.live / 1000}s refresh
        </span>
      }
    >
      <div className="overflow-x-auto">
        <table className="w-full text-[12px]">
          <thead className="text-[10.5px] uppercase tracking-wide text-text-muted">
            <tr className="border-b border-bg-border/60">
              <th className="px-2 py-2 text-left">Symbol</th>
              <th className="px-2 py-2 text-right">Spot</th>
              <th className="px-2 py-2 text-right">ADX</th>
              <th className="px-2 py-2 text-right">EMA Δ</th>
              <th className="px-2 py-2 text-left">Regime</th>
              <th className="px-2 py-2 text-left">Signal</th>
              <th className="px-2 py-2 text-right">Conf</th>
              <th className="px-2 py-2 text-left">Pick</th>
              <th className="px-2 py-2 text-left">Policy</th>
              <th className="px-2 py-2 text-right">Sampled R</th>
              <th className="px-2 py-2 text-right">Cand.</th>
            </tr>
          </thead>
          <tbody>
            {symbols.map((sym, i) => {
              const d = queries[i].data || {};
              const feat = d.feature_snapshot || {};
              const reg = d.regime || {};
              const sig = d.signal || {};
              const pol = d.policy || null;
              const c = d.selected_contract || null;
              const ds = d.data_status || {};
              const isSelected = sym === selected;
              const fresh = ds.execution_ready;
              return (
                <tr
                  key={sym}
                  onClick={() => onSelect?.(sym)}
                  className={clsx(
                    "border-b border-bg-border/30 transition-colors",
                    onSelect && "cursor-pointer hover:bg-bg-primary/15",
                    isSelected && "bg-accent-blue/8",
                  )}
                >
                  <td className="px-2 py-2 font-semibold text-text-primary">
                    {sym}
                    {!fresh ? (
                      <StatusBadge label={(ds.degraded_reason || "stale").replaceAll("_", " ")} variant="warn" className="ml-2" />
                    ) : null}
                  </td>
                  <td className="px-2 py-2 text-right font-mono">{formatNumber(d.spot_price, 2)}</td>
                  <td className="px-2 py-2 text-right font-mono">{formatNumber(feat.adx, 1)}</td>
                  <td className={clsx("px-2 py-2 text-right font-mono", (feat.ema_spread_pct ?? 0) >= 0 ? "text-accent-green" : "text-accent-red")}>
                    {formatPct(feat.ema_spread_pct, 3)}
                  </td>
                  <td className="px-2 py-2">
                    {reg.label ? (
                      <StatusBadge label={`${reg.label} · ${formatNumber(reg.confidence, 2)}`} tone={regimeTone(reg.label)} />
                    ) : (
                      <span className="text-text-muted">—</span>
                    )}
                  </td>
                  <td className={clsx("px-2 py-2 font-mono font-semibold", directionTone(sig.direction))}>
                    {sig.direction || "—"}
                  </td>
                  <td className="px-2 py-2 text-right font-mono">{formatNumber(sig.confidence, 3)}</td>
                  <td className="px-2 py-2 font-mono text-[11px]">
                    {c ? `${c.option_type ?? ""} ${c.strike ?? ""}` : "—"}
                  </td>
                  <td className="px-2 py-2">
                    {pol ? (
                      <StatusBadge
                        label={pol.act ? `ACT ${pol.size_multiplier?.toFixed(1) ?? "1.0"}×` : "SKIP"}
                        variant={pol.act ? "success" : "warn"}
                      />
                    ) : <span className="text-text-muted text-[10.5px]">—</span>}
                  </td>
                  <td className={clsx("px-2 py-2 text-right font-mono", (pol?.sampled_value ?? 0) >= 0 ? "text-accent-green" : "text-accent-red")}>
                    {pol?.sampled_value != null ? (pol.sampled_value >= 0 ? "+" : "") + pol.sampled_value.toFixed(2) : "—"}
                  </td>
                  <td className="px-2 py-2 text-right font-mono text-text-secondary">{d.contract_candidates?.length ?? 0}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </Section>
  );
}

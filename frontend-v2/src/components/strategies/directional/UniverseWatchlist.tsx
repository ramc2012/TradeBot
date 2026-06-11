"use client";

/**
 * Universe watchlist — one row per symbol, always rendered, click to
 * select. Slimmer than v1's MultiSymbolWatch — uses desk-ui formatters
 * + tones instead of local helpers, and shares useQueries result shape.
 */
import { useMemo, useState } from "react";
import { clsx } from "clsx";
import { useQueries } from "@tanstack/react-query";
import { Activity, ArrowDown, ArrowUp, Eye, Search } from "lucide-react";

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

  // Search + column sort. Rows are materialized (symbol + latest snapshot)
  // so numeric sorts work on live values; nulls always sink to the bottom.
  const [search, setSearch] = useState("");
  const [sortKey, setSortKey] = useState<string>("");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");
  const onSort = (key: string) => {
    if (key === sortKey) setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    else {
      setSortKey(key);
      setSortDir("desc");
    }
  };
  const rows = useMemo(() => {
    const all = symbols.map((sym, i) => ({ sym, d: queries[i].data || ({} as Snap) }));
    const q = search.trim().toUpperCase();
    const filtered = q
      ? all.filter(({ sym, d }) =>
          sym.toUpperCase().includes(q) ||
          (d.regime?.label ?? "").toUpperCase().includes(q) ||
          (d.signal?.direction ?? "").toUpperCase().includes(q),
        )
      : all;
    if (!sortKey) return filtered;
    const val = ({ d }: { d: Snap }): number | string | null => {
      switch (sortKey) {
        case "symbol": return null;
        case "spot": return d.spot_price ?? null;
        case "adx": return d.feature_snapshot?.adx ?? null;
        case "ema": return d.feature_snapshot?.ema_spread_pct ?? null;
        case "regime": return d.regime?.label ?? null;
        case "signal": return d.signal?.direction ?? null;
        case "conf": return d.signal?.confidence ?? null;
        case "policy": return d.policy ? (d.policy.act ? 1 : 0) : null;
        case "sampled": return d.policy?.sampled_value ?? null;
        case "cand": return d.contract_candidates?.length ?? null;
        default: return null;
      }
    };
    const dir = sortDir === "asc" ? 1 : -1;
    return [...filtered].sort((a, b) => {
      if (sortKey === "symbol") return a.sym.localeCompare(b.sym) * dir;
      const va = val(a);
      const vb = val(b);
      if (va == null && vb == null) return 0;
      if (va == null) return 1;
      if (vb == null) return -1;
      if (typeof va === "string" || typeof vb === "string") return String(va).localeCompare(String(vb)) * dir;
      return (va - (vb as number)) * dir;
    });
  }, [symbols, queries, search, sortKey, sortDir]);

  const Th = ({ k, label, align = "right" }: { k: string; label: string; align?: "left" | "right" }) => (
    <th
      onClick={() => onSort(k)}
      className={clsx("cursor-pointer select-none px-2 py-2 hover:text-text-secondary", align === "left" ? "text-left" : "text-right")}
    >
      <span className="inline-flex items-center gap-0.5">
        {label}
        {sortKey === k ? (sortDir === "asc" ? <ArrowUp size={10} /> : <ArrowDown size={10} />) : null}
      </span>
    </th>
  );

  return (
    <Section
      title="Universe watchlist"
      icon={<Eye size={16} />}
      description="What the engine is watching — features, regime, signal, policy verdict per symbol. Click a row to re-centre the desk on that symbol."
      rightSlot={
        <span className="inline-flex items-center gap-2 text-[11px] text-text-muted">
          <span className="relative">
            <Search size={11} className="pointer-events-none absolute left-2 top-1/2 -translate-y-1/2 text-text-muted" />
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="filter symbol / regime / signal"
              className="w-48 rounded-md border border-bg-border bg-bg-secondary py-1 pl-6 pr-2 text-[11px] text-text-primary placeholder:text-text-muted focus:outline-none"
            />
          </span>
          <Activity size={12} />
          {REFRESH_MS.live / 1000}s refresh
        </span>
      }
    >
      <div className="overflow-x-auto">
        <table className="w-full text-[12px]">
          <thead className="text-[10.5px] uppercase tracking-wide text-text-muted">
            <tr className="border-b border-bg-border/60">
              <Th k="symbol" label="Symbol" align="left" />
              <Th k="spot" label="Spot" />
              <Th k="adx" label="ADX" />
              <Th k="ema" label="EMA Δ" />
              <Th k="regime" label="Regime" align="left" />
              <Th k="signal" label="Signal" align="left" />
              <Th k="conf" label="Conf" />
              <th className="px-2 py-2 text-left">Pick</th>
              <Th k="policy" label="Policy" align="left" />
              <Th k="sampled" label="Sampled R" />
              <Th k="cand" label="Cand." />
            </tr>
          </thead>
          <tbody>
            {rows.map(({ sym, d }) => {
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

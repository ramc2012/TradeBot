"use client";

/**
 * Always-on watchlist for every symbol in the engine's universe.
 *
 * One row per symbol with the live calculation: spot, regime label +
 * confidence, signal direction + confidence, policy verdict + size
 * multiplier, top candidate strike + score, data freshness.
 *
 * The point: even when the policy chooses to skip — or when the data
 * pipeline is stale — the user can see WHAT the engine is watching and
 * WHAT it's computing, instead of an empty panel that looks dead.
 *
 * Fetches all symbols in parallel via useQueries; refresh cadence is
 * tied to the main workspace's live cadence (15s).
 */
import { clsx } from "clsx";
import { useQueries } from "@tanstack/react-query";
import { Activity, Eye } from "lucide-react";

import { getDirectionalOptionsLiveSnapshot } from "@/lib/api";

type SymbolRow = {
  underlying?: string;
  spot_price?: number | null;
  feature_snapshot?: {
    adx?: number;
    ema_spread_pct?: number;
    atr?: number;
    rv_percentile?: number;
  } | null;
  regime?: { label?: string; confidence?: number; trade_allowed?: boolean } | null;
  signal?: { direction?: string; confidence?: number; expected_horizon_bars?: number; jump_score?: number } | null;
  policy?: { act?: boolean; size_multiplier?: number; sampled_value?: number; posterior_mean?: number } | null;
  selected_contract?: { strike?: number; option_type?: string; delta?: number; contract_score?: number; option_price?: number } | null;
  contract_candidates?: Array<unknown>;
  data_status?: { execution_ready?: boolean; spot_age_seconds?: number | null; degraded_reason?: string | null };
  as_of?: string | null;
  selection_reason?: string;
};

function fmt(v: number | null | undefined, digits = 2): string {
  if (v == null || Number.isNaN(v)) return "—";
  return v.toFixed(digits);
}

function fmtPct(v: number | null | undefined, digits = 2): string {
  if (v == null || Number.isNaN(v)) return "—";
  return `${(v * 100).toFixed(digits)}%`;
}

function regimeTone(label: string | undefined): string {
  switch (label) {
    case "breakout":
    case "trend":
      return "text-emerald-300 border-emerald-500/30 bg-emerald-500/10";
    case "micro_trend":
    case "exploration":
      return "text-sky-300 border-sky-500/30 bg-sky-500/10";
    case "chop":
      return "text-amber-300 border-amber-500/30 bg-amber-500/10";
    case "risk_off":
      return "text-rose-300 border-rose-500/30 bg-rose-500/10";
    default:
      return "text-text-muted border-bg-border bg-bg-primary/15";
  }
}

function dirTone(d: string | undefined): string {
  if (d === "CE") return "text-emerald-300";
  if (d === "PE") return "text-rose-300";
  return "text-text-muted";
}

function policyChip(p: SymbolRow["policy"]): { label: string; cls: string } {
  if (!p) return { label: "—", cls: "border-bg-border text-text-muted" };
  if (p.act) {
    return {
      label: `ACT ${p.size_multiplier?.toFixed(1) ?? "1.0"}×`,
      cls: "border-emerald-500/40 bg-emerald-500/10 text-emerald-300",
    };
  }
  return { label: "SKIP", cls: "border-amber-500/40 bg-amber-500/10 text-amber-300" };
}

export default function MultiSymbolWatch({
  symbols,
  timeframe,
  lookbackSessions,
  onSelect,
  selectedSymbol,
}: {
  symbols: string[];
  timeframe: string;
  lookbackSessions: number;
  onSelect?: (sym: string) => void;
  selectedSymbol?: string;
}) {
  const queries = useQueries({
    queries: symbols.map((symbol) => ({
      queryKey: ["do-watch", symbol, timeframe, lookbackSessions],
      queryFn: async () => {
        const r = await getDirectionalOptionsLiveSnapshot(symbol, timeframe, lookbackSessions);
        return ((r.data as { snapshot?: SymbolRow })?.snapshot ?? {}) as SymbolRow;
      },
      refetchInterval: 15_000,
      refetchOnWindowFocus: false,
    })),
  });

  const rows = symbols.map((s, i) => ({ _key: s, _state: queries[i], data: queries[i].data || {} }));

  return (
    <section className="rounded-[26px] border border-bg-border bg-bg-secondary/24 p-5">
      <div className="mb-3 flex items-center justify-between gap-3">
        <div>
          <div className="flex items-center gap-2 text-sm font-semibold text-text-primary">
            <Eye size={16} />
            Universe watchlist
          </div>
          <div className="mt-1 text-xs text-text-muted">
            What the engine is watching right now — features, regime, signal, and
            policy verdict per symbol. Click a row to pull it into the detail panels.
          </div>
        </div>
        <span className="inline-flex items-center gap-1.5 text-[11px] text-text-muted">
          <Activity size={12} />
          15s refresh
        </span>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-[12px]">
          <thead className="text-[10.5px] uppercase tracking-wide text-text-muted">
            <tr className="border-b border-bg-border/60">
              <th className="px-2 py-2 text-left">Symbol</th>
              <th className="px-2 py-2 text-right">Spot</th>
              <th className="px-2 py-2 text-right">ADX</th>
              <th className="px-2 py-2 text-right">EMA Δ</th>
              <th className="px-2 py-2 text-right">RV%</th>
              <th className="px-2 py-2 text-left">Regime</th>
              <th className="px-2 py-2 text-left">Signal</th>
              <th className="px-2 py-2 text-right">Conf</th>
              <th className="px-2 py-2 text-left">Picked contract</th>
              <th className="px-2 py-2 text-right">Score</th>
              <th className="px-2 py-2 text-left">Policy</th>
              <th className="px-2 py-2 text-right">Sampled R</th>
              <th className="px-2 py-2 text-right">Cand.</th>
              <th className="px-2 py-2 text-left">Data</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(({ _key, _state, data }) => {
              const feat = data.feature_snapshot || {};
              const reg = data.regime || {};
              const sig = data.signal || {};
              const pol = data.policy || null;
              const c = data.selected_contract || null;
              const ds = data.data_status || {};
              const chip = policyChip(pol);
              const fresh = ds.execution_ready && (ds.spot_age_seconds ?? Infinity) < 600;
              const isLoading = _state.isLoading;
              const isSelected = _key === selectedSymbol;
              return (
                <tr
                  key={_key}
                  onClick={() => onSelect?.(_key)}
                  className={clsx(
                    "border-b border-bg-border/30 transition-colors",
                    onSelect && "cursor-pointer hover:bg-bg-primary/15",
                    isSelected && "bg-accent-blue/8",
                  )}
                >
                  <td className="px-2 py-2 font-semibold text-text-primary">{_key}</td>
                  <td className="px-2 py-2 text-right font-mono">
                    {isLoading ? "…" : fmt(data.spot_price, 2)}
                  </td>
                  <td className="px-2 py-2 text-right font-mono">{fmt(feat.adx, 1)}</td>
                  <td className={clsx("px-2 py-2 text-right font-mono", (feat.ema_spread_pct ?? 0) >= 0 ? "text-emerald-200" : "text-rose-200")}>
                    {fmtPct(feat.ema_spread_pct, 3)}
                  </td>
                  <td className="px-2 py-2 text-right font-mono text-text-secondary">
                    {feat.rv_percentile != null ? `${(feat.rv_percentile * 100).toFixed(0)}` : "—"}
                  </td>
                  <td className="px-2 py-2">
                    {reg.label ? (
                      <span className={clsx("rounded-full border px-2 py-0.5 text-[10.5px] font-semibold", regimeTone(reg.label))}>
                        {reg.label} · {fmt(reg.confidence, 2)}
                      </span>
                    ) : (
                      <span className="text-text-muted">—</span>
                    )}
                  </td>
                  <td className={clsx("px-2 py-2 font-mono font-semibold", dirTone(sig.direction))}>
                    {sig.direction || "—"}
                  </td>
                  <td className="px-2 py-2 text-right font-mono">{fmt(sig.confidence, 3)}</td>
                  <td className="px-2 py-2 font-mono text-[11px]">
                    {c ? `${c.option_type || ""} ${c.strike ?? ""}` : "—"}
                    <span className="ml-1 text-text-muted">
                      {c?.delta != null ? `Δ${c.delta.toFixed(2)}` : ""}
                    </span>
                  </td>
                  <td className="px-2 py-2 text-right font-mono text-text-secondary">
                    {fmt(c?.contract_score, 1)}
                  </td>
                  <td className="px-2 py-2">
                    <span className={clsx("rounded-full border px-2 py-0.5 text-[10.5px] font-semibold uppercase tracking-[0.12em]", chip.cls)}>
                      {chip.label}
                    </span>
                  </td>
                  <td className={clsx("px-2 py-2 text-right font-mono", (pol?.sampled_value ?? 0) >= 0 ? "text-emerald-300" : "text-rose-300")}>
                    {pol?.sampled_value != null ? (pol.sampled_value >= 0 ? "+" : "") + pol.sampled_value.toFixed(2) : "—"}
                  </td>
                  <td className="px-2 py-2 text-right font-mono text-text-secondary">
                    {data.contract_candidates?.length ?? 0}
                  </td>
                  <td className="px-2 py-2 text-[10.5px]">
                    <span className={clsx(
                      "rounded border px-1.5 py-0.5",
                      fresh
                        ? "border-emerald-500/40 bg-emerald-500/8 text-emerald-200"
                        : "border-amber-500/30 bg-amber-500/8 text-amber-300",
                    )}>
                      {fresh ? "live" : (ds.degraded_reason || "stale").replaceAll("_", " ")}
                    </span>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}

"use client";

/**
 * SelectedBarInspector — the footprint of the ONE bar the trader clicked.
 *
 * The click arrives from `LinkedChartProvider` (any pane can originate it), so
 * clicking the flow pane inspects the same bar as clicking price. If no
 * footprint exists for that timestamp the panel says so; it never falls back to
 * "the latest bar", which would silently answer a question nobody asked.
 *
 * Imbalance is rendered through the shared `imbalanceOf` contract: a bounded
 * 0-100% share plus the raw volumes, and "one-sided" where the opposing side is
 * empty. The backend's `buy_ratio`/`sell_ratio` are unbounded divide-by-zero
 * artefacts and are never printed as magnitudes.
 */
import { MousePointerClick } from "lucide-react";
import { useMemo } from "react";

import type { FootprintBar } from "@/components/mpof";
import { StatusBadge, formatISTTime, formatNumber } from "@/components/desk-ui";
import { imbalanceOf } from "@/lib/market-semantics";

import { toChartTime } from "./chart-time";
import { useLinkedChart } from "./LinkedChartProvider";

export function SelectedBarInspector({
  bars,
  digits = 1,
}: {
  bars: FootprintBar[];
  digits?: number;
}) {
  const { selectedTime } = useLinkedChart();

  const bar = useMemo(() => {
    if (selectedTime == null) return null;
    return bars.find((b) => toChartTime(b.time) === selectedTime) ?? null;
  }, [bars, selectedTime]);

  if (selectedTime == null) {
    return (
      <div className="flex items-center gap-2 rounded-xl border border-dashed border-bg-border/60 px-3 py-4 text-[11.5px] text-text-muted">
        <MousePointerClick size={13} />
        Click any bar on either pane to inspect its price-by-price detail.
      </div>
    );
  }

  if (!bar) {
    return (
      <div className="rounded-xl border border-dashed border-bg-border/60 px-3 py-4 text-[11.5px] text-text-muted">
        No footprint detail exists for the selected bar. The bar is on the price
        pane, but the flow payload carried no price-by-price rows for it.
      </div>
    );
  }

  const levels = [...(bar.levels ?? [])].sort((a, b) => b.price - a.price);
  const maxSide = levels.reduce((m, l) => Math.max(m, l.buy ?? 0, l.sell ?? 0), 0) || 1;

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-[12px] text-text-primary">{formatISTTime(bar.time)}</span>
        <StatusBadge
          label={`Δ ${formatNumber(bar.delta, 0)}`}
          variant={Number(bar.delta ?? 0) >= 0 ? "info" : "warn"}
        />
        <span className="text-[11px] text-text-muted">
          volume {formatNumber(bar.volume, 0)} · cumulative Δ {formatNumber(bar.cumulative_delta, 0)}
        </span>
      </div>

      {!levels.length ? (
        <div className="rounded-lg border border-dashed border-bg-border/60 px-3 py-3 text-[11.5px] text-text-muted">
          This bar carries no price-by-price rows.
        </div>
      ) : (
        <div className="max-h-[280px] overflow-y-auto rounded-lg border border-bg-border/70">
          <table className="w-full text-[11px]">
            <thead className="sticky top-0 bg-bg-secondary/90 text-[9.5px] uppercase tracking-[0.1em] text-text-muted">
              <tr>
                <th className="px-2 py-1 text-left">price</th>
                <th className="px-2 py-1 text-right">sell (inferred)</th>
                <th className="px-2 py-1 text-right">buy (inferred)</th>
                <th className="px-2 py-1 text-right">share</th>
              </tr>
            </thead>
            <tbody>
              {levels.map((l) => {
                const buyImb = imbalanceOf(l.buy, l.sell, l.buy_ratio);
                const dominant = (l.buy ?? 0) >= (l.sell ?? 0) ? "buy" : "sell";
                const imb = dominant === "buy" ? buyImb : imbalanceOf(l.sell, l.buy, l.sell_ratio);
                return (
                  <tr key={l.price} className="border-t border-bg-border/40">
                    <td className="px-2 py-1 font-mono text-text-secondary">
                      {formatNumber(l.price, digits)}
                    </td>
                    <td className="relative px-2 py-1 text-right font-mono text-text-secondary">
                      <span
                        className="absolute inset-y-0 right-0 bg-accent-red/15"
                        style={{ width: `${((l.sell ?? 0) / maxSide) * 100}%` }}
                        aria-hidden
                      />
                      <span className="relative">{formatNumber(l.sell, 0)}</span>
                    </td>
                    <td className="relative px-2 py-1 text-right font-mono text-text-secondary">
                      <span
                        className="absolute inset-y-0 left-0 bg-accent-green/15"
                        style={{ width: `${((l.buy ?? 0) / maxSide) * 100}%` }}
                        aria-hidden
                      />
                      <span className="relative">{formatNumber(l.buy, 0)}</span>
                    </td>
                    <td className="px-2 py-1 text-right font-mono text-text-muted">
                      {imb.oneSided
                        ? `one-sided ${dominant}`
                        : imb.pct == null
                          ? "—"
                          : `${dominant} ${imb.pct.toFixed(0)}%`}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      <p className="text-[10.5px] leading-4 text-text-muted">
        The buy/sell split of every row is inferred from quotes plus cumulative
        volume. No wired broker sends an aggressor-tagged print, so these are
        attributions, not counts.
      </p>
    </div>
  );
}

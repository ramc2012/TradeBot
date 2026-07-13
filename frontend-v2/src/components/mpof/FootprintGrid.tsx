"use client";

/**
 * FootprintGrid — the last few footprint bars as a price × (sell|buy) grid.
 *
 * One shared price axis (union of all shown bars' levels, descending) with a
 * sell/buy cell pair per bar. Cells shade with imbalance intensity; a ratio
 * ≥ ratioHighlight (default 3×) gets a hard ring so the trigger condition is
 * visible at a glance. Per-bar Δ and cumulative delta run underneath, and the
 * header carries the tick-source honesty badge + the last bar's timestamp.
 */
import { useMemo } from "react";

import { LastUpdated } from "@/components/common/LastUpdated";
import { formatIST, formatISTTime, formatNumber } from "@/components/desk-ui";

import { OfSourceBadge } from "./OfSourceBadge";

export type FootprintLevel = { price: number; buy: number; sell: number; buy_ratio?: number; sell_ratio?: number };
export type FootprintBar = {
  time: string;
  delta?: number;
  volume?: number;
  cumulative_delta?: number;
  levels?: FootprintLevel[];
};

const compact = (v?: number | null): string => {
  const n = Number(v ?? 0);
  if (!Number.isFinite(n)) return "—";
  const abs = Math.abs(n);
  if (abs >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (abs >= 10_000) return `${(n / 1_000).toFixed(0)}k`;
  if (abs >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return n.toFixed(0);
};

export function FootprintGrid({
  bars,
  maxBars = 4,
  ratioHighlight = 3,
  source,
  digits = 1,
  hideHeader = false,
  maxHeight = 420,
}: {
  bars?: FootprintBar[] | null;
  maxBars?: number;
  ratioHighlight?: number;
  source?: string | null;
  digits?: number;
  hideHeader?: boolean;
  maxHeight?: number;
}) {
  const shown = useMemo(() => (bars ?? []).slice(-maxBars), [bars, maxBars]);

  const { prices, byBar } = useMemo(() => {
    const priceSet = new Map<string, number>();
    const perBar: Array<Map<string, FootprintLevel>> = shown.map((bar) => {
      const m = new Map<string, FootprintLevel>();
      for (const level of bar.levels ?? []) {
        if (!Number.isFinite(Number(level.price))) continue;
        const key = Number(level.price).toFixed(6);
        priceSet.set(key, Number(level.price));
        m.set(key, level);
      }
      return m;
    });
    const sorted = Array.from(priceSet.entries()).sort((a, b) => b[1] - a[1]);
    return { prices: sorted, byBar: perBar };
  }, [shown]);

  const maxVol = useMemo(() => {
    let max = 1;
    for (const bar of shown) for (const level of bar.levels ?? []) max = Math.max(max, Number(level.buy) || 0, Number(level.sell) || 0);
    return max;
  }, [shown]);

  const lastTime = shown.at(-1)?.time ?? null;

  if (!shown.length || !prices.length) {
    return (
      <div className="space-y-2">
        {!hideHeader ? <OfSourceBadge source={source} /> : null}
        <div className="flex h-32 items-center justify-center rounded-xl border border-dashed border-bg-border/60 text-xs text-text-muted">
          No footprint bars — no genuine tick tape reconstructed yet.
        </div>
      </div>
    );
  }

  return (
    <div className="w-full">
      {!hideHeader ? (
        <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <OfSourceBadge source={source} />
            <span className="text-[10px] uppercase tracking-[0.12em] text-text-muted">≥{ratioHighlight}× imbalance ringed</span>
          </div>
          <LastUpdated timestamp={lastTime} label="last bar" />
        </div>
      ) : null}

      <div className="overflow-x-auto">
        <div className="min-w-fit">
          {/* header row: bar times */}
          <div className="flex items-end gap-1 pb-1">
            <div className="w-[70px] shrink-0" />
            {shown.map((bar, i) => (
              <div key={i} className="w-[128px] shrink-0 text-center font-mono text-[10px] text-text-muted" title={`${formatIST(bar.time)} IST`}>
                {formatISTTime(bar.time) || `bar ${i + 1}`}
                <div className="mt-0.5 flex justify-between px-1 text-[8.5px] uppercase tracking-[0.1em]">
                  <span className="text-accent-red/80">sell</span>
                  <span className="text-accent-green/80">buy</span>
                </div>
              </div>
            ))}
          </div>

          {/* price × bar grid */}
          <div className="overflow-y-auto" style={{ maxHeight }}>
            {prices.map(([key, price]) => (
              <div key={key} className="flex items-stretch gap-1 border-b border-bg-border/30">
                <div className="flex w-[70px] shrink-0 items-center justify-end pr-1.5 font-mono text-[10.5px] text-text-secondary">
                  {formatNumber(price, digits)}
                </div>
                {shown.map((_, barIdx) => {
                  const level = byBar[barIdx]?.get(key);
                  if (!level) {
                    return <div key={barIdx} className="w-[128px] shrink-0 bg-bg-secondary/10" />;
                  }
                  const sellRatio = Number(level.sell_ratio ?? (Number(level.buy) > 0 ? Number(level.sell) / Number(level.buy) : Number(level.sell) > 0 ? 99 : 0));
                  const buyRatio = Number(level.buy_ratio ?? (Number(level.sell) > 0 ? Number(level.buy) / Number(level.sell) : Number(level.buy) > 0 ? 99 : 0));
                  const sellHot = sellRatio >= ratioHighlight;
                  const buyHot = buyRatio >= ratioHighlight;
                  const sellAlpha = 0.08 + 0.5 * Math.min(1, (Number(level.sell) || 0) / maxVol);
                  const buyAlpha = 0.08 + 0.5 * Math.min(1, (Number(level.buy) || 0) / maxVol);
                  return (
                    <div key={barIdx} className="flex w-[128px] shrink-0">
                      <div
                        className={`flex h-[19px] w-1/2 items-center justify-end pr-1 font-mono text-[10px] ${sellHot ? "font-bold text-accent-red ring-1 ring-inset ring-accent-red/80" : "text-text-secondary"}`}
                        style={{ backgroundColor: `rgba(255,71,87,${sellAlpha.toFixed(3)})` }}
                        title={`sell ${formatNumber(Number(level.sell), 0)} · ${formatNumber(sellRatio, 1)}×`}
                      >
                        {compact(level.sell)}
                      </div>
                      <div
                        className={`flex h-[19px] w-1/2 items-center pl-1 font-mono text-[10px] ${buyHot ? "font-bold text-accent-green ring-1 ring-inset ring-accent-green/80" : "text-text-secondary"}`}
                        style={{ backgroundColor: `rgba(0,212,163,${buyAlpha.toFixed(3)})` }}
                        title={`buy ${formatNumber(Number(level.buy), 0)} · ${formatNumber(buyRatio, 1)}×`}
                      >
                        {compact(level.buy)}
                      </div>
                    </div>
                  );
                })}
              </div>
            ))}
          </div>

          {/* per-bar delta + cumulative delta footer */}
          <div className="mt-1 flex items-start gap-1 border-t border-bg-border/50 pt-1">
            <div className="flex w-[70px] shrink-0 flex-col items-end pr-1.5 font-mono text-[9.5px] text-text-muted">
              <span>Δ</span>
              <span>CVD</span>
              <span>vol</span>
            </div>
            {shown.map((bar, i) => {
              const delta = Number(bar.delta ?? 0);
              const cvd = Number(bar.cumulative_delta ?? 0);
              return (
                <div key={i} className="flex w-[128px] shrink-0 flex-col items-center font-mono text-[10px]">
                  <span className={delta > 0 ? "text-accent-green" : delta < 0 ? "text-accent-red" : "text-text-muted"}>
                    {delta > 0 ? "+" : ""}{compact(delta)}
                  </span>
                  <span className={cvd > 0 ? "text-accent-green/80" : cvd < 0 ? "text-accent-red/80" : "text-text-muted"}>
                    {cvd > 0 ? "+" : ""}{compact(cvd)}
                  </span>
                  <span className="text-text-muted">{compact(bar.volume)}</span>
                </div>
              );
            })}
          </div>
        </div>
      </div>
    </div>
  );
}

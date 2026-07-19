"use client";

/**
 * PricePane — candles + structural levels + the plan, on the shared timeline.
 *
 * Wraps the existing `CandleChart` (unchanged for its three other call sites)
 * and registers its chart with `LinkedChartProvider` so the crosshair and the
 * zoom are shared with the flow pane below.
 *
 * Every overlay here is a FIELD, not a computation:
 *   POC / VAH / VAL / IBH / IBL   market_profile block of the served payload
 *   prior VAH / VAL / POC         convergence detail `profile.prior`
 *   VWAP (last)                   `metrics.vwap` — a SCALAR. It is drawn as a
 *                                 labelled level, never as a curve, because no
 *                                 payload in this stack carries a vwap series
 *                                 and a drawn curve would be invented.
 *   entry / stop / target         the lane's own risk block, and only when the
 *                                 lane emitted one.
 *
 * Session separators are derived from the bar dates already fetched — a
 * re-expression of data on screen, not a new claim.
 */
import { useCallback, useEffect, useMemo, useRef } from "react";

import { CandleChart, type CandleBar, type ChartPriceLine } from "@/components/strategies/shared";

import { toChartTime } from "../chart-time";
import { useLinkedChart } from "../LinkedChartProvider";
import type { CanvasBar, CanvasLevels, CanvasPlan } from "../useMarketCanvas";

const LEVEL_COLOR = {
  poc: "#ffa502",
  va: "#3b82f6",
  ib: "rgba(255,165,2,0.7)",
  prior: "#a78bfa",
  vwap: "#e6edf3",
  entry: "#00d4a3",
  stop: "#ff4757",
  target: "#38bdf8",
} as const;

export type OverlayToggles = {
  valueArea: boolean;
  initialBalance: boolean;
  prior: boolean;
  vwap: boolean;
  plan: boolean;
};

export function PricePane({
  paneId = "price",
  bars,
  levels,
  plan,
  overlays,
  fitKey,
  height = 380,
}: {
  paneId?: string;
  bars: CanvasBar[];
  levels: CanvasLevels;
  plan: CanvasPlan | null;
  overlays: OverlayToggles;
  fitKey: string;
  height?: number;
}) {
  const { register, unregister } = useLinkedChart();

  const chartBars: CandleBar[] = useMemo(
    () =>
      bars.map((b) => ({
        time: b.time,
        open: b.open,
        high: b.high,
        low: b.low,
        close: b.close,
        volume: b.volume,
      })),
    [bars],
  );

  // Chart-time → close, for the peer crosshair. Held in a ref so re-registering
  // is not needed when the data refreshes.
  const indexRef = useRef<Map<number, number>>(new Map());
  useEffect(() => {
    const map = new Map<number, number>();
    for (const b of bars) map.set(toChartTime(b.time), b.close);
    indexRef.current = map;
  }, [bars]);

  const onChartReady = useCallback(
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (chart: any, series: any) => {
      register(paneId, {
        chart,
        series,
        priceAt: (t: number) => indexRef.current.get(t) ?? null,
      });
    },
    [register, paneId],
  );
  const onChartDispose = useCallback(() => unregister(paneId), [unregister, paneId]);

  const priceLines: ChartPriceLine[] = useMemo(() => {
    const out: ChartPriceLine[] = [];
    const push = (price: number | null, color: string, title: string, dashed = false) => {
      if (price != null && Number.isFinite(price)) out.push({ price, color, title, dashed });
    };
    push(levels.poc, LEVEL_COLOR.poc, "POC");
    if (overlays.valueArea) {
      push(levels.vah, LEVEL_COLOR.va, "VAH", true);
      push(levels.val, LEVEL_COLOR.va, "VAL", true);
    }
    if (overlays.initialBalance) {
      push(levels.ibHigh, LEVEL_COLOR.ib, "IBH", true);
      push(levels.ibLow, LEVEL_COLOR.ib, "IBL", true);
    }
    if (overlays.prior) {
      push(levels.prior.vah, LEVEL_COLOR.prior, "pVAH", true);
      push(levels.prior.poc, LEVEL_COLOR.prior, "pPOC", true);
      push(levels.prior.val, LEVEL_COLOR.prior, "pVAL", true);
    }
    // Labelled "(last)" deliberately: this is one scalar, not a vwap curve.
    if (overlays.vwap) push(levels.vwapLast, LEVEL_COLOR.vwap, "VWAP (last)", true);
    if (overlays.plan && plan) {
      push(plan.entry, LEVEL_COLOR.entry, "entry");
      push(plan.stop, LEVEL_COLOR.stop, "stop");
      push(plan.target1, LEVEL_COLOR.target, "target 1", true);
      push(plan.target2, LEVEL_COLOR.target, "target 2", true);
    }
    return out;
  }, [levels, plan, overlays]);

  return (
    <CandleChart
      bars={chartBars}
      priceLines={priceLines}
      height={height}
      showVolume
      fitKey={fitKey}
      onChartReady={onChartReady}
      onChartDispose={onChartDispose}
    />
  );
}

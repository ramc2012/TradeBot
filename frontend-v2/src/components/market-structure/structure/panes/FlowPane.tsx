"use client";

/**
 * FlowPane — cumulative delta (line) + per-bar signed volume (histogram),
 * BELOW price and on the SAME x-axis.
 *
 * HONESTY. Both series are buy/sell ATTRIBUTIONS. The inputs are observed
 * quotes plus cumulative volume; the split into buy and sell is inferred, and
 * there is no aggressor-tagged print feed to check it against. The pane header
 * says so, and the grade it renders comes from the shared contract with
 * `feature: "flow_attribution"`, so it can never come out green.
 *
 * ALIGNMENT. A crosshair shared between two panes on different bar clocks
 * points at different bars on each — it looks like it works and is wrong. So
 * before drawing anything this pane checks its timestamps against the price
 * pane's (`alignFlowToPrice`) and, when they do not match, renders the reason
 * instead of a resampled series. We do not interpolate flow onto a foreign
 * clock.
 *
 * VIEWPORT. The pane fits its content only when `fitKey` changes — the same
 * gate `CandleChart` uses — so a poll never yanks the viewport, and because
 * both panes share one fitKey an instrument change re-fits them together.
 */
import { useCallback, useEffect, useMemo, useRef } from "react";

import { toChartTime, alignFlowToPrice, DESK_TZ_OFFSET_MINUTES } from "../chart-time";
import { useLinkedChart } from "../LinkedChartProvider";
import type { CanvasBar, FlowPoint } from "../useMarketCanvas";

/* eslint-disable @typescript-eslint/no-explicit-any */

export function FlowPane({
  paneId = "flow",
  points,
  priceBars,
  fitKey,
  height = 190,
}: {
  paneId?: string;
  points: FlowPoint[];
  priceBars: CanvasBar[];
  fitKey: string;
  height?: number;
}) {
  const { register, unregister } = useLinkedChart();
  const containerRef = useRef<HTMLDivElement>(null);
  const refs = useRef<{ chart?: any; cvd?: any; delta?: any }>({});
  const fitRef = useRef<string | null>(null);
  const indexRef = useRef<Map<number, number>>(new Map());
  const registerRef = useRef(register);
  const unregisterRef = useRef(unregister);
  registerRef.current = register;
  unregisterRef.current = unregister;

  const alignment = useMemo(
    () =>
      alignFlowToPrice(
        priceBars.map((b) => toChartTime(b.time)),
        points.map((p) => toChartTime(p.time)),
      ),
    [priceBars, points],
  );

  const rows = useMemo(
    () =>
      points
        .map((p) => ({ time: toChartTime(p.time), cvd: p.cvd, delta: p.delta }))
        .filter((p) => Number.isFinite(p.time) && Number.isFinite(p.cvd))
        .sort((a, b) => a.time - b.time)
        .filter((row, i, all) => i === 0 || row.time !== all[i - 1].time),
    [points],
  );

  useEffect(() => {
    const map = new Map<number, number>();
    for (const r of rows) map.set(r.time, r.cvd);
    indexRef.current = map;
  }, [rows]);

  const drawable = alignment.aligned && rows.length > 1;

  const pushData = useCallback(() => {
    const { chart, cvd, delta } = refs.current;
    if (!chart || !cvd || !delta) return;
    cvd.setData(rows.map((r) => ({ time: r.time, value: r.cvd })));
    delta.setData(
      rows.map((r) => ({
        time: r.time,
        value: r.delta,
        color: r.delta >= 0 ? "rgba(0,212,163,0.45)" : "rgba(255,71,87,0.45)",
      })),
    );
    if (rows.length && fitRef.current !== fitKey) {
      chart.timeScale().fitContent();
      fitRef.current = fitKey;
    }
  }, [rows, fitKey]);

  useEffect(() => {
    if (!drawable) return;
    let disposed = false;
    let ro: ResizeObserver | undefined;
    (async () => {
      const lw = await import("lightweight-charts");
      if (disposed || !containerRef.current) return;
      const cs = getComputedStyle(document.documentElement);
      const col = (v: string) =>
        `rgb(${(cs.getPropertyValue(v).trim() || "148 163 184").replace(/\s+/g, ", ")})`;
      const chart = lw.createChart(containerRef.current, {
        height,
        layout: {
          background: { type: lw.ColorType.Solid, color: "transparent" },
          textColor: col("--text-secondary"),
          fontSize: 10,
        },
        grid: {
          vertLines: { color: "rgba(255,255,255,0.04)" },
          horzLines: { color: "rgba(255,255,255,0.05)" },
        },
        rightPriceScale: { borderColor: col("--bg-border") },
        timeScale: { borderColor: col("--bg-border"), timeVisible: true, secondsVisible: false },
        crosshair: { mode: lw.CrosshairMode.Normal },
      });
      const cvd = chart.addLineSeries({
        color: "#60a5fa",
        lineWidth: 2,
        priceLineVisible: false,
        title: "CVD",
      });
      const delta = chart.addHistogramSeries({
        priceScaleId: "delta",
        priceFormat: { type: "volume" },
      });
      chart.priceScale("delta").applyOptions({ scaleMargins: { top: 0.7, bottom: 0 } });
      refs.current = { chart, cvd, delta };
      ro = new ResizeObserver(() => {
        if (containerRef.current) chart.applyOptions({ width: containerRef.current.clientWidth });
      });
      ro.observe(containerRef.current);
      chart.applyOptions({ width: containerRef.current.clientWidth });
      pushData();
      registerRef.current(paneId, {
        chart,
        series: cvd,
        priceAt: (t: number) => indexRef.current.get(t) ?? null,
      });
    })();
    return () => {
      disposed = true;
      ro?.disconnect();
      if (refs.current.chart) unregisterRef.current(paneId);
      refs.current.chart?.remove();
      refs.current = {};
      fitRef.current = null;
    };
    // Recreated only when the pane becomes drawable or its geometry changes —
    // NOT on a data refresh (that goes through pushData below).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [drawable, height, paneId]);

  useEffect(() => {
    pushData();
  }, [pushData]);

  if (!drawable) {
    return (
      <div
        className="flex items-center justify-center rounded-xl border border-dashed border-bg-border/60 px-4 text-center text-[11.5px] leading-5 text-text-muted"
        style={{ height }}
      >
        {rows.length <= 1
          ? "Not enough flow bars to draw a cumulative-delta pane."
          : alignment.reason}
      </div>
    );
  }

  return (
    <div className="relative w-full" style={{ height }}>
      <div ref={containerRef} className="h-full w-full" />
    </div>
  );
}

export { DESK_TZ_OFFSET_MINUTES };

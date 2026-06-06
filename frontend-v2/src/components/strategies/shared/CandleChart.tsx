"use client";

/**
 * Institutional candlestick chart on TradingView lightweight-charts.
 * Canvas-rendered (crosshair, fit-content, volume pane, price-line overlays),
 * themed live from the app's CSS variables, created once and updated in place
 * (preserves zoom). lightweight-charts is dynamically imported inside the
 * effect so it never touches SSR.
 */
import { useEffect, useRef } from "react";

export type CandleBar = { time: number | string; open: number; high: number; low: number; close: number; volume?: number };
export type ChartPriceLine = { price: number; color: string; title: string; dashed?: boolean };

function toUnix(t: number | string): number {
  if (typeof t === "number") return t > 1e12 ? Math.floor(t / 1000) : t;
  const ms = Date.parse(t);
  return Math.floor((Number.isNaN(ms) ? Date.now() : ms) / 1000);
}

export function CandleChart({
  bars,
  priceLines = [],
  height = 420,
  showVolume = true,
}: {
  bars: CandleBar[];
  priceLines?: ChartPriceLine[];
  height?: number;
  showVolume?: boolean;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const refs = useRef<{ chart?: any; candle?: any; vol?: any; lines: any[] }>({ lines: [] });

  // create chart once
  useEffect(() => {
    let disposed = false;
    let ro: ResizeObserver | undefined;
    (async () => {
      const lw = await import("lightweight-charts");
      if (disposed || !containerRef.current) return;
      const cs = getComputedStyle(document.documentElement);
      const col = (v: string) => `rgb(${cs.getPropertyValue(v).trim() || "148 163 184"})`;
      const chart = lw.createChart(containerRef.current, {
        height,
        layout: { background: { type: lw.ColorType.Solid, color: "transparent" }, textColor: col("--text-secondary"), fontSize: 10 },
        grid: { vertLines: { color: "rgba(255,255,255,0.04)" }, horzLines: { color: "rgba(255,255,255,0.05)" } },
        rightPriceScale: { borderColor: col("--bg-border") },
        timeScale: { borderColor: col("--bg-border"), timeVisible: true, secondsVisible: false },
        crosshair: { mode: lw.CrosshairMode.Normal },
      });
      const candle = chart.addCandlestickSeries({
        upColor: col("--accent-green"),
        downColor: col("--accent-red"),
        wickUpColor: col("--accent-green"),
        wickDownColor: col("--accent-red"),
        borderVisible: false,
      });
      refs.current.chart = chart;
      refs.current.candle = candle;
      if (showVolume) {
        const vol = chart.addHistogramSeries({ priceScaleId: "vol", priceFormat: { type: "volume" } });
        chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.84, bottom: 0 } });
        refs.current.vol = vol;
      }
      ro = new ResizeObserver(() => {
        if (containerRef.current) chart.applyOptions({ width: containerRef.current.clientWidth });
      });
      ro.observe(containerRef.current);
      chart.applyOptions({ width: containerRef.current.clientWidth });
      pushData();
    })();
    return () => {
      disposed = true;
      ro?.disconnect();
      refs.current.chart?.remove();
      refs.current = { lines: [] };
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function pushData() {
    const { candle, vol, chart } = refs.current;
    if (!candle) return;
    const seen = new Set<number>();
    const data = [...bars]
      .map((b) => ({ time: toUnix(b.time), open: +b.open, high: +b.high, low: +b.low, close: +b.close }))
      .sort((a, b) => a.time - b.time)
      .filter((d) => (seen.has(d.time) ? false : seen.add(d.time)));
    candle.setData(data);
    if (vol) {
      const vseen = new Set<number>();
      vol.setData(
        [...bars]
          .map((b) => ({ time: toUnix(b.time), value: b.volume || 0, color: +b.close >= +b.open ? "rgba(0,212,163,0.4)" : "rgba(255,71,87,0.4)" }))
          .sort((a, b) => a.time - b.time)
          .filter((d) => (vseen.has(d.time) ? false : vseen.add(d.time))),
      );
    }
    refs.current.lines.forEach((l) => candle.removePriceLine(l));
    refs.current.lines = priceLines
      .filter((pl) => Number.isFinite(pl.price))
      .map((pl) => candle.createPriceLine({ price: pl.price, color: pl.color, lineWidth: 1, lineStyle: pl.dashed ? 2 : 0, axisLabelVisible: true, title: pl.title }));
    chart?.timeScale().fitContent();
  }

  // update on data / overlay change
  useEffect(() => {
    pushData();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [bars, priceLines]);

  if (!bars?.length) {
    return (
      <div className="flex items-center justify-center rounded-xl border border-dashed border-bg-border/60 text-sm text-text-muted" style={{ height }}>
        No price bars available.
      </div>
    );
  }
  return <div ref={containerRef} className="w-full" style={{ height }} />;
}

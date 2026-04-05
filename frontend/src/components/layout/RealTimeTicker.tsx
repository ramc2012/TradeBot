"use client";

import { useEffect, useRef } from "react";
import { clsx } from "clsx";
import { useQuery } from "@tanstack/react-query";

import { getLTP } from "@/lib/api";
import { createTickSocket } from "@/lib/websocket";
import { MARKET_INDEX_SYMBOLS, getMarketIndexLabel } from "@/lib/marketSymbols";
import type { Tick } from "@/store";
import { useTickStore, useTickSymbol } from "@/store";

function formatChangePct(ltp?: number, close?: number) {
  if (!ltp || !close) return "--";
  const pct = ((ltp - close) / close) * 100;
  const prefix = pct > 0 ? "+" : "";
  return `${prefix}${pct.toFixed(2)}%`;
}

function TickerItem({ symbol }: { symbol: string }) {
  const tick = useTickSymbol(symbol);
  const positive = tick && tick.close > 0 ? tick.ltp >= tick.close : undefined;

  return (
    <div className="min-w-0 rounded-lg border border-bg-border bg-bg-secondary/80 px-2.5 py-1.5">
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <div className="truncate text-[10px] uppercase tracking-[0.16em] text-text-muted">
            {getMarketIndexLabel(symbol)}
          </div>
          <div className="mt-1 font-mono text-[15px] font-semibold text-text-primary">
            {tick ? tick.ltp.toFixed(2) : "--"}
          </div>
        </div>
        <div className="text-right">
          <div
            className={clsx(
              "font-mono text-[11px] font-semibold",
              positive === undefined
                ? "text-text-muted"
                : positive
                  ? "text-accent-green"
                  : "text-accent-red"
            )}
          >
            {tick ? formatChangePct(tick.ltp, tick.close) : "Waiting"}
          </div>
          <div className="text-[10px] text-text-muted">
            {tick && tick.high > 0 && tick.low > 0
              ? `H ${tick.high.toFixed(0)} · L ${tick.low.toFixed(0)}`
              : "Live feed"}
          </div>
        </div>
      </div>
    </div>
  );
}

export default function RealTimeTicker() {
  const updateTick = useTickStore((state) => state.updateTick);
  const socketsRef = useRef<Array<{ close: () => void }>>([]);

  const ltpQuery = useQuery<Record<string, number>>({
    queryKey: ["headerIndexLtp"],
    queryFn: () => getLTP([...MARKET_INDEX_SYMBOLS]).then((response) => response.data),
    refetchInterval: 5000,
    staleTime: 2000,
  });

  useEffect(() => {
    socketsRef.current = MARKET_INDEX_SYMBOLS.map((symbol) =>
      createTickSocket(symbol, (data: object) => updateTick(data as Tick))
    );

    return () => {
      socketsRef.current.forEach((socket) => socket.close());
      socketsRef.current = [];
    };
  }, [updateTick]);

  useEffect(() => {
    const payload = ltpQuery.data;
    if (!payload) return;

    Object.entries(payload).forEach(([symbol, ltp]) => {
      if (!Number.isFinite(ltp) || ltp <= 0) return;
      const existing = useTickStore.getState().getTick(symbol);
      updateTick({
        symbol,
        ltp,
        open: existing?.open ?? 0,
        high: existing?.high ? Math.max(existing.high, ltp) : ltp,
        low: existing?.low ? Math.min(existing.low, ltp) : ltp,
        volume: existing?.volume ?? 0,
        oi: existing?.oi ?? 0,
        close: existing?.close ?? existing?.open ?? 0,
        timestamp: new Date().toISOString(),
      });
    });
  }, [ltpQuery.data, updateTick]);

  return (
    <div className="shrink-0 border-b border-bg-border bg-bg-primary/95 px-3 py-1.5 backdrop-blur">
      <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-4">
        {MARKET_INDEX_SYMBOLS.map((symbol) => (
          <TickerItem key={symbol} symbol={symbol} />
        ))}
      </div>
    </div>
  );
}

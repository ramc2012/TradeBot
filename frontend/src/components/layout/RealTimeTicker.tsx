"use client";

import { memo, useEffect, useRef } from "react";
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

const TickerItem = memo(function TickerItem({ symbol }: { symbol: string }) {
  const tick = useTickSymbol(symbol);
  const positive = tick && tick.close > 0 ? tick.ltp >= tick.close : undefined;

  return (
    <div className="min-w-[220px] rounded-xl border border-bg-border bg-bg-secondary/72 px-3 py-2">
      <div className="flex items-center gap-3">
        <div className="min-w-0 flex-1">
          <div className="text-[10px] uppercase tracking-[0.18em] text-text-muted">
            {getMarketIndexLabel(symbol)}
          </div>
          <div className="mt-1 flex items-baseline gap-2">
            <span className="font-mono text-[17px] font-semibold text-text-primary">
              {tick ? tick.ltp.toFixed(2) : "--"}
            </span>
            <span
              className={clsx(
                "font-mono text-[11px] font-semibold",
                positive === undefined
                  ? "text-text-muted"
                  : positive
                    ? "text-accent-green"
                    : "text-accent-red",
              )}
            >
              {tick ? formatChangePct(tick.ltp, tick.close) : "Waiting"}
            </span>
          </div>
        </div>
        <div className="min-w-[82px] text-right text-[10px] text-text-muted">
          {tick && tick.high > 0 && tick.low > 0 ? `H ${tick.high.toFixed(0)} · L ${tick.low.toFixed(0)}` : "Live feed"}
        </div>
      </div>
    </div>
  );
});

export default function RealTimeTicker() {
  const updateTick = useTickStore((state) => state.updateTick);
  const socketsRef = useRef<Array<{ close: () => void }>>([]);

  const ltpQuery = useQuery<Record<string, number>>({
    queryKey: ["headerIndexLtp"],
    queryFn: () => getLTP([...MARKET_INDEX_SYMBOLS]).then((response) => response.data),
    staleTime: Infinity,
  });

  useEffect(() => {
    socketsRef.current = MARKET_INDEX_SYMBOLS.map((symbol) =>
      createTickSocket(symbol, (data: unknown) => updateTick(data as Tick))
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
    <div className="shrink-0 border-b border-bg-border bg-bg-primary/95 px-3 py-2 backdrop-blur">
      <div className="flex gap-2 overflow-x-auto pb-1 [-ms-overflow-style:none] [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
        {MARKET_INDEX_SYMBOLS.map((symbol) => (
          <TickerItem key={symbol} symbol={symbol} />
        ))}
      </div>
    </div>
  );
}

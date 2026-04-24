"use client";

import { memo, useEffect, useRef } from "react";
import { clsx } from "clsx";

import { getLTP } from "@/lib/api";
import { createTickSocket } from "@/lib/websocket";
import { MARKET_INDEX_SYMBOLS, getMarketIndexLabel } from "@/lib/marketSymbols";
import { usePersistentSnapshotQuery } from "@/hooks/usePersistentSnapshotQuery";
import type { Tick } from "@/store";
import { useTickStore, useTickSymbol } from "@/store";

const HEADER_TICK_STORAGE_KEY = "nomad-curie.header-ticks.v4";
const HEADER_LTP_STORAGE_KEY = "nomad-curie.header-ltp.v4";

type TickSnapshot = Record<string, Tick>;

function asFiniteNumber(value: unknown): number | null {
  const next = Number(value);
  return Number.isFinite(next) ? next : null;
}

function normalizeTick(value: unknown): Tick | null {
  if (!value || typeof value !== "object") return null;
  const raw = value as Partial<Record<keyof Tick, unknown>>;
  if (typeof raw.symbol !== "string" || !raw.symbol.trim()) return null;

  const ltp = asFiniteNumber(raw.ltp);
  if (ltp == null) return null;

  return {
    symbol: raw.symbol,
    ltp,
    open: asFiniteNumber(raw.open) ?? ltp,
    high: asFiniteNumber(raw.high) ?? ltp,
    low: asFiniteNumber(raw.low) ?? ltp,
    close: asFiniteNumber(raw.close) ?? ltp,
    volume: asFiniteNumber(raw.volume) ?? 0,
    oi: asFiniteNumber(raw.oi) ?? 0,
    timestamp: typeof raw.timestamp === "string" ? raw.timestamp : new Date().toISOString(),
  };
}

function loadTickSnapshot(): TickSnapshot {
  if (typeof window === "undefined") return {};
  try {
    const raw = window.localStorage.getItem(HEADER_TICK_STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    const sanitizedEntries = Object.entries(parsed || {})
      .map(([symbol, tick]) => [symbol, normalizeTick(tick)] as const)
      .filter((entry): entry is readonly [string, Tick] => entry[1] !== null);
    return Object.fromEntries(sanitizedEntries) as TickSnapshot;
  } catch {
    return {};
  }
}

function persistTickSnapshot(snapshot: TickSnapshot) {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(HEADER_TICK_STORAGE_KEY, JSON.stringify(snapshot));
  } catch {
    // Ignore storage write failures; live ticks still flow through the store.
  }
}

function formatChangePct(ltp?: number, close?: number) {
  if (!Number.isFinite(ltp) || !Number.isFinite(close) || !close) return "--";
  const safeLtp = Number(ltp);
  const safeClose = Number(close);
  const pct = ((safeLtp - safeClose) / safeClose) * 100;
  const prefix = pct > 0 ? "+" : "";
  return `${prefix}${pct.toFixed(2)}%`;
}

function formatTickValue(value?: number, digits = 2) {
  return Number.isFinite(value) ? value!.toFixed(digits) : "--";
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
              {formatTickValue(tick?.ltp, 2)}
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
              {tick ? (
              formatChangePct(tick.ltp, tick.close)
            ) : (
              <span className="inline-flex items-center gap-1 text-text-muted">
                <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-text-muted" />
                Live
              </span>
            )}
            </span>
          </div>
        </div>
        <div className="min-w-[82px] text-right text-[10px] text-text-muted">
          {tick && Number.isFinite(tick.high) && Number.isFinite(tick.low) && tick.high > 0 && tick.low > 0
            ? `H ${formatTickValue(tick.high, 0)} · L ${formatTickValue(tick.low, 0)}`
            : <span className="animate-pulse text-text-muted/50">connecting…</span>}
        </div>
      </div>
    </div>
  );
});

export default function RealTimeTicker() {
  const updateTick = useTickStore((state) => state.updateTick);
  const socketsRef = useRef<Array<{ close: () => void }>>([]);
  const persistedTicksRef = useRef<TickSnapshot>({});
  const lastPersistAtRef = useRef(0);

  const ltpQuery = usePersistentSnapshotQuery<Record<string, number>>({
    storageKey: HEADER_LTP_STORAGE_KEY,
    queryKey: ["headerIndexLtp"],
    queryFn: () => getLTP([...MARKET_INDEX_SYMBOLS]).then((response) => response.data),
    staleTime: 15_000,
    refetchInterval: 30_000,
    refetchOnWindowFocus: false,
  });

  useEffect(() => {
    const persistedTicks = loadTickSnapshot();
    persistedTicksRef.current = persistedTicks;
    Object.values(persistedTicks).forEach((tick) => updateTick(tick));

    socketsRef.current = MARKET_INDEX_SYMBOLS.map((symbol) =>
      createTickSocket(symbol, (data: unknown) => {
        const tick = normalizeTick(data);
        if (!tick) return;
        updateTick(tick);
        persistedTicksRef.current = {
          ...persistedTicksRef.current,
          [tick.symbol]: tick,
        };
        const now = Date.now();
        if (now - lastPersistAtRef.current >= 5_000) {
          lastPersistAtRef.current = now;
          persistTickSnapshot(persistedTicksRef.current);
        }
      })
    );

    return () => {
      socketsRef.current.forEach((socket) => socket.close());
      socketsRef.current = [];
    };
  }, [updateTick]);

  useEffect(() => {
    const payload = ltpQuery.data;
    if (!payload || typeof payload !== "object") return;

    Object.entries(payload).forEach(([symbol, rawLtp]) => {
      const ltp = asFiniteNumber(rawLtp);
      if (ltp == null || ltp <= 0) return;
      const existing = useTickStore.getState().getTick(symbol);
      const nextTick = {
        symbol,
        ltp,
        open: existing?.open ?? ltp,
        high: existing?.high ? Math.max(existing.high, ltp) : ltp,
        low: existing?.low ? Math.min(existing.low, ltp) : ltp,
        volume: existing?.volume ?? 0,
        oi: existing?.oi ?? 0,
        close: existing?.close ?? existing?.open ?? ltp,
        timestamp: new Date().toISOString(),
      } satisfies Tick;
      updateTick(nextTick);
      persistedTicksRef.current = {
        ...persistedTicksRef.current,
        [symbol]: nextTick,
      };
    });

    persistTickSnapshot(persistedTicksRef.current);
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

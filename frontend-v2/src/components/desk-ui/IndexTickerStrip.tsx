"use client";

/**
 * Index ticker strip — NIFTY / BANKNIFTY / SENSEX at the top of every
 * strategy desk.
 *
 * Two data layers, freshest wins per symbol:
 *   1. The shared /ws/quotes tape (useQuote) — sub-second during NSE RTH.
 *      Index ticks ride the broker WS required-symbol set, so they stream
 *      whenever the feed is up.
 *   2. A 15s POST /api/market/latest-ticks poll — paints instantly on
 *      mount, covers after-hours (last close) and any tape outage. This is
 *      what keeps the strip honest instead of frozen when a socket dies.
 *
 * Change is computed against the previous session close (tape `pc`, REST
 * `close`). A freshness dot marks each chip: green = ticking (<30s),
 * amber = stalling (<5m), muted = market closed / feed down.
 */
import { useEffect, useRef, useState } from "react";
import { clsx } from "clsx";

import { api } from "@/lib/api";
import { useQuote } from "@/hooks/useQuoteStore";

const INDICES = [
  { symbol: "NSE:NIFTY50-INDEX", label: "NIFTY" },
  { symbol: "NSE:BANKNIFTY-INDEX", label: "BANKNIFTY" },
  { symbol: "BSE:SENSEX-INDEX", label: "SENSEX" },
] as const;

const REST_POLL_MS = 15_000;

type RestTick = {
  symbol?: string;
  ltp?: number | null;
  close?: number | null;
  timestamp?: string | null;
  stale?: boolean;
  stale_seconds?: number | null;
};

function toEpochMs(ts: string | null | undefined): number {
  if (!ts) return 0;
  // Backend snapshots are tz-aware ISO; guard naive strings anyway.
  const normalized = /[zZ]|[+-]\d{2}:\d{2}$/.test(ts) ? ts : `${ts}Z`;
  const ms = new Date(normalized).getTime();
  return Number.isFinite(ms) ? ms : 0;
}

function useRestTicks(): Record<string, RestTick> {
  const [ticks, setTicks] = useState<Record<string, RestTick>>({});
  useEffect(() => {
    let active = true;
    const symbols = INDICES.map((i) => i.symbol);
    const poll = async () => {
      try {
        const res = await api.post("/api/market/latest-ticks", { symbols });
        if (active && res.data && typeof res.data === "object") {
          setTicks(res.data as Record<string, RestTick>);
        }
      } catch {
        // keep last snapshot — staleness dot reports the gap
      }
    };
    void poll();
    const id = setInterval(poll, REST_POLL_MS);
    return () => {
      active = false;
      clearInterval(id);
    };
  }, []);
  return ticks;
}

function IndexChip({
  label,
  symbol,
  rest,
}: {
  label: string;
  symbol: string;
  rest: RestTick | undefined;
}) {
  const quote = useQuote(symbol);

  const tapeMs = quote?.rxAt ?? 0;
  const restMs = toEpochMs(rest?.timestamp);
  const useTape = quote?.ltp != null && tapeMs >= restMs;

  const ltp = useTape ? quote?.ltp : rest?.ltp;
  const prevClose = (useTape ? quote?.prevClose : null) ?? rest?.close ?? quote?.prevClose ?? null;
  const asOfMs = useTape ? tapeMs : restMs;

  // Flash direction on price change (tape dir when live, else derived).
  const lastLtp = useRef<number | null>(null);
  const dir =
    ltp != null && lastLtp.current != null && ltp !== lastLtp.current
      ? ltp > lastLtp.current
        ? "up"
        : "down"
      : "flat";
  useEffect(() => {
    lastLtp.current = ltp ?? lastLtp.current;
  }, [ltp]);

  const change = ltp != null && prevClose != null && prevClose > 0 ? ltp - prevClose : null;
  const changePct = change != null && prevClose ? (change / prevClose) * 100 : null;

  const ageSec = asOfMs > 0 ? (Date.now() - asOfMs) / 1000 : Infinity;
  const dotClass =
    ageSec < 30
      ? "bg-emerald-400"
      : ageSec < 300
        ? "bg-amber-400"
        : "bg-text-muted/40";

  return (
    <span className="inline-flex items-baseline gap-1.5 whitespace-nowrap" title={symbol}>
      <span className={clsx("h-1.5 w-1.5 self-center rounded-full", dotClass)} />
      <span className="text-[9.5px] font-semibold uppercase tracking-[0.14em] text-text-muted">{label}</span>
      <span
        className={clsx(
          "font-mono text-[12px] font-medium tabular-nums transition-colors duration-300",
          dir === "up" ? "text-emerald-300" : dir === "down" ? "text-rose-300" : "text-text-primary",
        )}
      >
        {ltp != null
          ? ltp.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })
          : "—"}
      </span>
      {change != null && changePct != null ? (
        <span
          className={clsx(
            "font-mono text-[10.5px] tabular-nums",
            change >= 0 ? "text-emerald-400/90" : "text-rose-400/90",
          )}
        >
          {change >= 0 ? "+" : ""}
          {change.toFixed(2)} ({change >= 0 ? "+" : ""}
          {changePct.toFixed(2)}%)
        </span>
      ) : null}
    </span>
  );
}

export function IndexTickerStrip({ className }: { className?: string }) {
  const rest = useRestTicks();
  return (
    <div className={clsx("flex flex-wrap items-center gap-x-5 gap-y-1", className)}>
      {INDICES.map((idx) => (
        <IndexChip key={idx.symbol} label={idx.label} symbol={idx.symbol} rest={rest[idx.symbol]} />
      ))}
    </div>
  );
}

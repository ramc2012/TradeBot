"use client";

import { useEffect, useRef, useState } from "react";
import { createTickSocket } from "@/lib/websocket";

/**
 * Symbol-translation table: charts/orderflow refer to instruments by
 * their app symbol (NIFTY, BANKNIFTY, …) but the broker WebSocket only
 * publishes ticks under the broker key (NSE:NIFTY50-INDEX, etc.).
 * Mirrors backend/market_data/symbols.APP_TO_BROKER_SYMBOL.
 */
const APP_TO_BROKER: Record<string, string> = {
  NIFTY: "NSE:NIFTY50-INDEX",
  BANKNIFTY: "NSE:NIFTYBANK-INDEX",
  FINNIFTY: "NSE:FINNIFTY-INDEX",
  MIDCPNIFTY: "NSE:MIDCPNIFTY-INDEX",
  SENSEX: "BSE:SENSEX-INDEX",
};

export interface TickPayload {
  symbol: string;
  ltp: number | null;
  open: number | null;
  high: number | null;
  low: number | null;
  close: number | null;
  volume: number | null;
  oi: number | null;
  timestamp: string;
  source?: string;
  stale?: boolean;
}

/**
 * Subscribe to the backend tick stream for one symbol. Returns the last
 * tick payload (or null until the first tick arrives).
 *
 * Replaces the per-30s REST poll pattern: when the chart or orderflow
 * page wants live price, the WebSocket pushes every broker tick instead
 * of waiting for the next polling cycle. Historical bars + indicators
 * remain on REST — they don't change on tick.
 */
export function useTickStream(appSymbol: string | null | undefined): TickPayload | null {
  const [tick, setTick] = useState<TickPayload | null>(null);
  const symbolRef = useRef<string | null>(null);

  useEffect(() => {
    if (!appSymbol) return;
    // Only NSE indices have direct WS feed today; for stocks / commodities
    // the page falls back to the existing REST polling.
    const brokerSymbol = APP_TO_BROKER[appSymbol.toUpperCase()];
    if (!brokerSymbol) {
      setTick(null);
      return;
    }
    symbolRef.current = appSymbol;
    setTick(null);
    const socket = createTickSocket(brokerSymbol, (raw) => {
      try {
        const data = (typeof raw === "string" ? JSON.parse(raw) : raw) as TickPayload;
        // Guard against late deliveries from a previous symbol when
        // the user switches instruments quickly.
        if (symbolRef.current !== appSymbol) return;
        setTick(data);
      } catch {
        // Ignore malformed payloads
      }
    });
    return () => {
      socket.close();
    };
  }, [appSymbol]);

  return tick;
}

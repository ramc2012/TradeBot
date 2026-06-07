"use client";

/**
 * Live 5-level depth (DOM) ladder for one focused symbol. Opens a dedicated
 * /ws/depth/{symbol} socket (the backend ref-counts the DepthUpdate subscription)
 * and returns the latest book. One ladder is typically open at a time.
 */
import { useEffect, useState } from "react";

import { createDepthSocket } from "@/lib/websocket";

export type DepthLevel = { p: number; q: number; o: number };
export type DepthBook = {
  symbol: string;
  bids: DepthLevel[];
  asks: DepthLevel[];
  tbq?: number;
  tsq?: number;
  timestamp?: string;
};

export function useDepth(symbol: string | null | undefined): {
  book: DepthBook | null;
  connected: boolean;
} {
  const [book, setBook] = useState<DepthBook | null>(null);
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    if (!symbol) {
      setBook(null);
      setConnected(false);
      return;
    }
    setBook(null);
    const sock = createDepthSocket(
      symbol,
      (data) => setBook(data as DepthBook),
      (c) => setConnected(c),
    );
    return () => sock.close();
  }, [symbol]);

  return { book, connected };
}

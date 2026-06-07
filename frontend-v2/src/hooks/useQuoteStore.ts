"use client";

/**
 * Terminal-grade quote store — the frontend half of the low-latency pipeline.
 *
 * The problem: a fast tape (many symbols × several prints/sec) would cause a
 * React re-render storm if every tick called setState. The solution, in two
 * layers (the backend coalesces to ~150ms frames; this is the frontend layer):
 *
 *   1. A module-level store OUTSIDE React. The single shared /ws/quotes socket
 *      writes ticks straight into a Map — it NEVER calls setState directly.
 *   2. A single requestAnimationFrame loop drains "dirty" symbols once per frame
 *      (≤60fps) and notifies ONLY the components subscribed to a changed symbol.
 *
 * Components use `useQuote(symbol)` (cell-isolated via useSyncExternalStore) so a
 * NIFTY tick re-renders only the NIFTY cell, not the whole grid. The socket is
 * ref-counted: opened on the first subscriber, closed when the last leaves.
 */
import { useCallback, useSyncExternalStore } from "react";

import { createQuotesSocket } from "@/lib/websocket";

export type Quote = {
  symbol: string;
  ltp: number | null;
  /** previous ltp, for price-flash direction */
  prevLtp: number | null;
  dir: "up" | "down" | "flat";
  open?: number | null;
  high?: number | null;
  low?: number | null;
  prevClose?: number | null;
  bid?: number | null;
  ask?: number | null;
  bidQty?: number | null;
  askQty?: number | null;
  volume?: number | null;
  oi?: number | null;
  /** epoch ms of the underlying tick */
  ts: number;
  /** received-at epoch ms (for freshness/staleness) */
  rxAt: number;
  /** true when the value came from a coalesced (≤150ms batched) frame, not a raw print */
  coalesced: boolean;
};

type Listener = () => void;

// Short-key → Quote mapper (matches backend quote_bus._compact / frame shape).
type RawQuote = {
  s: string; p?: number; o?: number; h?: number; l?: number; pc?: number;
  b?: number; a?: number; bz?: number; az?: number; v?: number; oi?: number;
  t?: number; c?: number;
};

class QuoteStore {
  private quotes = new Map<string, Quote>();
  private listeners = new Map<string, Set<Listener>>();
  private connListeners = new Set<Listener>();
  private symbolListeners = new Set<Listener>();
  private dirty = new Set<string>();
  private socket: { close: () => void } | null = null;
  private rafHandle: number | null = null;
  private refCount = 0;
  private connected = false;
  // Stable cached sorted symbol list (rebuilt only when the set changes) so
  // useSyncExternalStore sees a stable reference between unchanged renders.
  private knownCache: string[] = [];
  private knownDirty = false;

  // ── public read API ──
  get(symbol: string | null | undefined): Quote | undefined {
    return symbol ? this.quotes.get(symbol) : undefined;
  }

  isConnected(): boolean {
    return this.connected;
  }

  knownSymbols(): string[] {
    if (this.knownDirty) {
      this.knownCache = Array.from(this.quotes.keys()).sort();
      this.knownDirty = false;
    }
    return this.knownCache;
  }

  // ── subscription (ref-counted; opens/closes the shared socket) ──
  subscribe(symbol: string | null | undefined, cb: Listener): () => void {
    if (!symbol) return () => {};
    let set = this.listeners.get(symbol);
    if (!set) {
      set = new Set();
      this.listeners.set(symbol, set);
    }
    set.add(cb);
    this.acquire();
    return () => {
      const s = this.listeners.get(symbol);
      if (s) {
        s.delete(cb);
        if (s.size === 0) this.listeners.delete(symbol);
      }
      this.release();
    };
  }

  subscribeConnection(cb: Listener): () => void {
    this.connListeners.add(cb);
    this.acquire();
    return () => {
      this.connListeners.delete(cb);
      this.release();
    };
  }

  subscribeSymbols(cb: Listener): () => void {
    this.symbolListeners.add(cb);
    this.acquire();
    return () => {
      this.symbolListeners.delete(cb);
      this.release();
    };
  }

  // ── socket lifecycle ──
  private acquire() {
    this.refCount += 1;
    if (this.refCount === 1) this.open();
  }

  private release() {
    this.refCount = Math.max(0, this.refCount - 1);
    if (this.refCount === 0) this.close();
  }

  private open() {
    if (this.socket || typeof window === "undefined") return;
    this.socket = createQuotesSocket(
      (data) => this.ingest(data),
      (connected) => {
        this.connected = connected;
        this.connListeners.forEach((cb) => cb());
      },
    );
    this.startRaf();
  }

  private close() {
    this.socket?.close();
    this.socket = null;
    this.stopRaf();
    this.connected = false;
  }

  // ── ingest a frame: {q:[{s,p,...}], snap?} — write to store, mark dirty ──
  private ingest(data: unknown) {
    const frame = data as { q?: RawQuote[] } | null;
    const rows = frame?.q;
    if (!Array.isArray(rows)) return;
    const now = Date.now();
    let newSymbol = false;
    for (const r of rows) {
      if (!r || !r.s) continue;
      const prev = this.quotes.get(r.s);
      if (!prev) {
        newSymbol = true;
        this.knownDirty = true;
      }
      const ltp = r.p ?? prev?.ltp ?? null;
      const prevLtp = prev?.ltp ?? null;
      let dir: Quote["dir"] = prev?.dir ?? "flat";
      if (ltp != null && prevLtp != null) {
        dir = ltp > prevLtp ? "up" : ltp < prevLtp ? "down" : "flat";
      }
      this.quotes.set(r.s, {
        symbol: r.s,
        ltp,
        prevLtp,
        dir,
        open: r.o ?? prev?.open ?? null,
        high: r.h ?? prev?.high ?? null,
        low: r.l ?? prev?.low ?? null,
        prevClose: r.pc ?? prev?.prevClose ?? null,
        bid: r.b ?? prev?.bid ?? null,
        ask: r.a ?? prev?.ask ?? null,
        bidQty: r.bz ?? prev?.bidQty ?? null,
        askQty: r.az ?? prev?.askQty ?? null,
        volume: r.v ?? prev?.volume ?? null,
        oi: r.oi ?? prev?.oi ?? null,
        ts: r.t ?? now,
        rxAt: now,
        coalesced: r.c === 1,
      });
      this.dirty.add(r.s);
    }
    if (newSymbol) this.symbolListeners.forEach((cb) => cb());
  }

  // ── rAF drain: notify subscribers of changed symbols, once per frame ──
  private startRaf() {
    if (this.rafHandle != null || typeof window === "undefined") return;
    const tick = () => {
      if (this.dirty.size) {
        const changed = this.dirty;
        this.dirty = new Set();
        changed.forEach((sym) => {
          const set = this.listeners.get(sym);
          if (set) set.forEach((cb) => cb());
        });
      }
      this.rafHandle = window.requestAnimationFrame(tick);
    };
    this.rafHandle = window.requestAnimationFrame(tick);
  }

  private stopRaf() {
    if (this.rafHandle != null && typeof window !== "undefined") {
      window.cancelAnimationFrame(this.rafHandle);
    }
    this.rafHandle = null;
  }
}

// Module-level singleton (one store + one socket per browser tab).
export const quoteStore = new QuoteStore();

/**
 * Subscribe a component to ONE symbol. Re-renders only when that symbol's quote
 * changes (cell isolation). Returns undefined until the first tick arrives.
 */
export function useQuote(symbol: string | null | undefined): Quote | undefined {
  const subscribe = useCallback(
    (cb: () => void) => quoteStore.subscribe(symbol, cb),
    [symbol],
  );
  const getSnapshot = useCallback(() => quoteStore.get(symbol), [symbol]);
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}

/** Live connection status of the shared quote socket (for a health badge). */
export function useQuotesConnection(): boolean {
  const subscribe = useCallback((cb: () => void) => quoteStore.subscribeConnection(cb), []);
  const getSnapshot = useCallback(() => quoteStore.isConnected(), []);
  return useSyncExternalStore(subscribe, getSnapshot, () => false);
}

const EMPTY_SYMBOLS: string[] = [];

/**
 * All symbols the tape currently knows about (sorted). Auto-populates the
 * terminal grid from whatever the backend is actually subscribed to — so we
 * never hardcode or guess broker symbol keys.
 */
export function useKnownSymbols(): string[] {
  const subscribe = useCallback((cb: () => void) => quoteStore.subscribeSymbols(cb), []);
  const getSnapshot = useCallback(() => quoteStore.knownSymbols(), []);
  return useSyncExternalStore(subscribe, getSnapshot, () => EMPTY_SYMBOLS);
}

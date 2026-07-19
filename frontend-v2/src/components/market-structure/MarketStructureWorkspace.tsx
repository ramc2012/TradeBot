"use client";

/**
 * MarketStructureWorkspace — the instrument-centric shell.
 *
 * ONE context (in the URL), ONE summary query set, ONE detail drawer. Changing
 * the instrument is a single context mutation, so every panel moves together;
 * there is no per-panel symbol state anywhere in this tree for them to drift on.
 *
 * The old desks are untouched and keep working: this is an additional route
 * that composes the SAME endpoints they already serve, and deep-links back into
 * them carrying the pin.
 */
import { useCallback, useMemo, useState } from "react";

import { StatusBadge } from "@/components/desk-ui";
import { useSystemState } from "@/hooks/useSystemState";

import { CommandView } from "./command/CommandView";
import { decorateRows, useUniverseMatrix } from "./command/useUniverseMatrix";
import { ContextBar } from "./ContextBar";
import { useWorkspaceContext } from "./context/useWorkspaceContext";
import type { WorkspaceView } from "./context/schema";
import { InstrumentDrawer } from "./drawer/InstrumentDrawer";
import { PlaceholderView } from "./views/PlaceholderView";
import { ViewNav } from "./ViewNav";

type AuctionState = { regime: string | null; allowed: boolean | null; reasons: string[] };

export default function MarketStructureWorkspace() {
  const { ctx, setCtx, replayForced } = useWorkspaceContext();
  const matrix = useUniverseMatrix(ctx.market);

  const [drawerOpen, setDrawerOpen] = useState(true);
  /** Auction states the trader has explicitly loaded, per symbol. */
  const [auctionStates, setAuctionStates] = useState<Record<string, AuctionState>>({});

  const onAuctionLoaded = useCallback(
    (symbol: string, regime: string | null, allowed: boolean | null, reasons: string[]) => {
      setAuctionStates((prev) => {
        const cur = prev[symbol];
        if (cur && cur.regime === regime && cur.allowed === allowed) return prev;
        return { ...prev, [symbol]: { regime, allowed, reasons } };
      });
    },
    [],
  );

  // Merge on-demand auction states into the summary rows. This is the only path
  // by which the Auction column becomes populated — it is never guessed.
  const rowsWithAuction = useMemo(() => {
    if (!Object.keys(auctionStates).length) return matrix.rows;
    return matrix.rows.map((r) => {
      const a = auctionStates[r.symbol];
      if (!a) return r;
      return {
        ...r,
        auction: {
          available: true,
          reason: null,
          regime: a.regime,
          allowed: a.allowed,
          reasons: a.reasons,
          openLots: r.auction.openLots,
        },
      };
    });
  }, [matrix.rows, auctionStates]);

  const mergedMatrix = useMemo(() => ({ ...matrix, rows: rowsWithAuction }), [matrix, rowsWithAuction]);

  // The drawer needs the SAME derived row the matrix shows, so the two can never
  // disagree about freshness or plan completeness.
  // Session state must be fed in here exactly as CommandView feeds it, or the
  // drawer and the matrix disagree about data mode for the very same row.
  const { nseOpen, mcxOpen } = useSystemState();
  const sessionOpen = ctx.market === "MCX" ? mcxOpen : nseOpen;

  const selectedRow = useMemo(() => {
    const base = rowsWithAuction.find((r) => r.symbol === ctx.symbol);
    if (!base) return null;
    return decorateRows([base], Date.now(), { replay: ctx.replay, sessionOpen })[0] ?? null;
  }, [rowsWithAuction, ctx.symbol, ctx.replay, sessionOpen]);

  const contractHint = selectedRow?.contract ?? null;

  const onSelect = useCallback(
    (symbol: string, contract: string | null) => {
      setCtx({ symbol, contract });
      setDrawerOpen(true);
    },
    [setCtx],
  );

  const onSort = useCallback(
    (key: string) => {
      setCtx({ sortKey: key, sortDir: ctx.sortKey === key && ctx.sortDir === "desc" ? "asc" : "desc" });
    },
    [setCtx, ctx.sortKey, ctx.sortDir],
  );

  const setView = useCallback((view: WorkspaceView) => setCtx({ view }), [setCtx]);

  return (
    <div className="space-y-4" onKeyDown={(e) => { if (e.key === "Escape") setDrawerOpen(false); }}>
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-text-primary">Market structure</h1>
          <p className="mt-0.5 max-w-3xl text-sm text-text-muted">
            One instrument-centric workspace over every lane. The matrix is summary-only — no live
            per-symbol subscriptions — and detail loads for the pinned instrument alone.
          </p>
        </div>
        <div className="flex items-center gap-2">
          {matrix.isFetching ? <StatusBadge label="refreshing" variant="info" /> : null}
          <button
            type="button"
            onClick={() => setDrawerOpen((v) => !v)}
            className="rounded-lg border border-bg-border px-2.5 py-1 text-[11.5px] font-semibold text-text-secondary transition-colors hover:border-accent-blue/50 hover:text-text-primary"
          >
            {drawerOpen ? "Hide detail" : "Show detail"}
          </button>
        </div>
      </header>

      <ContextBar
        ctx={ctx}
        setCtx={setCtx}
        replayForced={replayForced}
        rowCount={matrix.rows.length}
        contractHint={contractHint}
      />

      <ViewNav view={ctx.view} onChange={setView} />

      <div className={drawerOpen ? "grid gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(340px,420px)]" : ""}>
        <div className="min-w-0">
          {ctx.view === "command" ? (
            <CommandView
              ctx={ctx}
              matrix={mergedMatrix}
              onSelect={onSelect}
              onSort={onSort}
              onQuery={(q) => setCtx({ query: q })}
            />
          ) : (
            <PlaceholderView view={ctx.view} ctx={ctx} />
          )}
        </div>
        {drawerOpen ? (
          <div className="min-w-0">
            <InstrumentDrawer
              ctx={ctx}
              row={selectedRow}
              onClose={() => setDrawerOpen(false)}
              onAuctionLoaded={onAuctionLoaded}
            />
          </div>
        ) : null}
      </div>
    </div>
  );
}

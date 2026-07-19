"use client";

/**
 * MarketStructureWorkspace — the instrument-centric shell.
 *
 * ONE context (in the URL), ONE summary query set, ONE detail drawer. Changing
 * the instrument is a single context mutation, so every panel moves together;
 * there is no per-panel symbol state anywhere in this tree for them to drift on.
 *
 * ONE DECORATION PASS (2026-07-19). The freshness ticker and `decorateRows`
 * live HERE, not in CommandView, and the decorated array is handed to both the
 * matrix and the header. The header's live verdict and the row's Readiness cell
 * therefore read the same object: they cannot disagree, because there is no
 * second freshness computation to drift. (Before this, the header did not read
 * the row at all — it hard-coded "no observation".)
 *
 * The old desks are untouched and keep working: this is an additional route
 * that composes the SAME endpoints they already serve, and deep-links back into
 * them carrying the pin.
 */
import { useCallback, useEffect, useMemo, useState } from "react";

import { StatusBadge } from "@/components/desk-ui";
import { useSystemState } from "@/hooks/useSystemState";

import { CommandView } from "./command/CommandView";
import { decorateRows, useUniverseMatrix, type MatrixRow } from "./command/useUniverseMatrix";
import { ContextBar } from "./ContextBar";
import { useWorkspaceContext } from "./context/useWorkspaceContext";
import type { WorkspaceView } from "./context/schema";
import { InstrumentDrawer } from "./drawer/InstrumentDrawer";
import { PlaceholderView } from "./views/PlaceholderView";
import { ViewNav } from "./ViewNav";

type AuctionState = { regime: string | null; allowed: boolean | null; reasons: string[] };

/** One ticker for the whole workspace — 200 rows cost 0 subscriptions. */
const TICK_MS = 15_000;

export default function MarketStructureWorkspace() {
  const { ctx, setCtx, asOfPinnedButUnapplied } = useWorkspaceContext();
  const matrix = useUniverseMatrix(ctx.market);

  const [drawerOpen, setDrawerOpen] = useState(true);
  /** Auction states the trader has explicitly loaded, per symbol. */
  const [auctionStates, setAuctionStates] = useState<Record<string, AuctionState>>({});

  const [nowMs, setNowMs] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNowMs(Date.now()), TICK_MS);
    return () => window.clearInterval(id);
  }, []);

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

  // Session state from the shared clock. A CLOSED session is the one honest
  // replay claim this workspace can make: what you are looking at is the last
  // session, not the one happening now. A user-typed `asOf` is NOT such a claim
  // and deliberately does not reach here.
  const { nseOpen, mcxOpen, feedOnline } = useSystemState();
  const sessionOpen = ctx.market === "MCX" ? mcxOpen : nseOpen;

  /** THE decoration pass. Everything downstream reads this one array. */
  const decorated: MatrixRow[] = useMemo(
    () => decorateRows(rowsWithAuction, nowMs, { sessionOpen }),
    [rowsWithAuction, nowMs, sessionOpen],
  );

  const selectedRow = useMemo(
    () => decorated.find((r) => r.symbol === ctx.symbol) ?? null,
    [decorated, ctx.symbol],
  );

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
        asOfPinnedButUnapplied={asOfPinnedButUnapplied}
        rowCount={matrix.rows.length}
        contractHint={contractHint}
        selectedRow={selectedRow}
        sessionOpen={sessionOpen}
        feedOnline={feedOnline}
        matrixLoading={matrix.isLoading}
      />

      <ViewNav view={ctx.view} onChange={setView} />

      {/*
        LAYOUT (2026-07-19 fix): the matrix needs ~1180px and the drawer 420px,
        so on anything narrower than a very wide desktop a persistent side panel
        left ~5 of 12 columns visible — which defeats the entire point of a
        comparison screen. Side-by-side ONLY where both genuinely fit (2xl);
        below that the drawer becomes an OVERLAY on the right, so the matrix
        keeps its full width behind it and the frozen Instrument + Readiness
        columns stay readable while detail is open.
      */}
      <div className={drawerOpen ? "grid gap-4 2xl:grid-cols-[minmax(0,1fr)_minmax(360px,420px)]" : ""}>
        <div className="min-w-0">
          {ctx.view === "command" ? (
            <CommandView
              ctx={ctx}
              rows={decorated}
              matrix={matrix}
              sessionOpen={sessionOpen}
              nowMs={nowMs}
              onSelect={onSelect}
              onSort={onSort}
              onQuery={(q) => setCtx({ query: q })}
            />
          ) : (
            <PlaceholderView view={ctx.view} ctx={ctx} />
          )}
        </div>
        {drawerOpen ? (
          <div
            className={
              "fixed inset-y-2 right-2 z-40 w-[min(420px,94vw)] min-w-0 rounded-2xl bg-bg-primary shadow-2xl " +
              "2xl:static 2xl:inset-auto 2xl:z-auto 2xl:w-auto 2xl:rounded-none 2xl:bg-transparent 2xl:shadow-none"
            }
          >
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

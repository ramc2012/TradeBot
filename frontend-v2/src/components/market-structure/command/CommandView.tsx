"use client";

/**
 * CommandView — the opportunity matrix.
 *
 * One screen that answers "where is there anything worth looking at, and can I
 * believe it" across the whole universe, then hands off to the detail drawer
 * for the one instrument that earns attention.
 *
 * Three invariants make this cheap and honest:
 *   1. ONE freshness ticker and ONE `decorateRows` pass for the whole workspace.
 *      Both live in `MarketStructureWorkspace`, which hands the decorated array
 *      to this view AND to the header — so the header's live verdict and a
 *      row's Readiness cell are literally the same object.
 *   2. Every cell is a real observation or an explicit "unavailable + reason".
 *      No column is faked to look complete, and the coverage strip states how
 *      many rows each column actually has a source for.
 *   3. LOADING, EMPTY-BECAUSE-FILTERED and EMPTY-BECAUSE-NO-DATA are three
 *      visually distinct states (2026-07-19 fix). The shipped build rendered a
 *      zero-row grid reading "0 instruments · No instruments match this filter"
 *      while the very first request was still in flight, which is a claim about
 *      the universe that had not been checked yet.
 */
import { Search } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { StatusBadge, StorageModeBadge } from "@/components/desk-ui";
import { DataModeBadge, FreshnessBadge } from "@/components/desk-ui";
import { classifyStorageMode, deriveFreshness } from "@/lib/market-semantics";

import type { WorkspaceContext } from "../context/schema";
import { compareRows, MATRIX_COLUMNS } from "./columns";
import { MatrixTable } from "./MatrixTable";
import type { MatrixRow, UniverseMatrix } from "./useUniverseMatrix";

export function CommandView({
  ctx,
  rows: decorated,
  matrix,
  sessionOpen,
  nowMs,
  onSelect,
  onSort,
  onQuery,
}: {
  ctx: WorkspaceContext;
  /** Already decorated by the workspace's single pass. Never re-derived here. */
  rows: MatrixRow[];
  matrix: UniverseMatrix;
  sessionOpen: boolean;
  nowMs: number;
  onSelect: (symbol: string, contract: string | null) => void;
  onSort: (key: string) => void;
  onQuery: (q: string) => void;
}) {
  const rows: MatrixRow[] = useMemo(() => {
    const q = ctx.query.trim().toUpperCase();
    const filtered = q ? decorated.filter((r) => r.symbol.includes(q)) : decorated;
    return [...filtered].sort((a, b) => compareRows(a, b, ctx.sortKey, ctx.sortDir));
  }, [decorated, ctx.query, ctx.sortKey, ctx.sortDir]);

  // Focus is LOCAL state, deliberately: committing every arrow keypress to the
  // URL would spam history and re-render the tree. Only Enter/click pins.
  const [focusedIndex, setFocusedIndex] = useState(0);
  const [focusedColumn, setFocusedColumn] = useState(0);
  const gridRef = useRef<HTMLDivElement | null>(null);
  const searchRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    const idx = rows.findIndex((r) => r.symbol === ctx.symbol);
    if (idx >= 0) setFocusedIndex(idx);
    else if (focusedIndex >= rows.length) setFocusedIndex(Math.max(0, rows.length - 1));
    // Only re-sync when the pin or the row set changes, not on every focus move.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ctx.symbol, rows.length]);

  const commit = useCallback(
    (index: number) => {
      const row = rows[index];
      if (row) onSelect(row.symbol, row.contract);
    },
    [rows, onSelect],
  );

  const onKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLDivElement>) => {
      const target = e.target as HTMLElement;
      const typing = target.tagName === "INPUT" || target.tagName === "TEXTAREA";
      if (typing) {
        if (e.key === "Escape") {
          (target as HTMLInputElement).blur();
          gridRef.current?.focus();
        }
        return;
      }
      const last = rows.length - 1;
      const page = 12;
      const move = (next: number) => {
        e.preventDefault();
        setFocusedIndex(Math.min(last, Math.max(0, next)));
      };
      switch (e.key) {
        case "ArrowDown":
        case "j":
          return move(focusedIndex + 1);
        case "ArrowUp":
        case "k":
          return move(focusedIndex - 1);
        case "PageDown":
          return move(focusedIndex + page);
        case "PageUp":
          return move(focusedIndex - page);
        case "Home":
          return move(0);
        case "End":
          return move(last);
        case "ArrowRight":
          e.preventDefault();
          return setFocusedColumn((c) => Math.min(MATRIX_COLUMNS.length - 1, c + 1));
        case "ArrowLeft":
          e.preventDefault();
          return setFocusedColumn((c) => Math.max(0, c - 1));
        case "Enter":
          e.preventDefault();
          return commit(focusedIndex);
        case "s": {
          e.preventDefault();
          const col = MATRIX_COLUMNS[focusedColumn];
          if (col?.sortValue) onSort(col.key);
          return;
        }
        case "/":
          e.preventDefault();
          searchRef.current?.focus();
          return;
        default:
          return;
      }
    },
    [rows.length, focusedIndex, focusedColumn, commit, onSort],
  );

  const readiness = useMemo(() => {
    const acc = { fresh: 0, stale: 0, absent: 0 };
    for (const r of rows) acc[r.freshness] += 1;
    return acc;
  }, [rows]);

  const generated = deriveFreshness(matrix.generatedAt, { nowMs });

  // THE THREE STATES. `loading` wins; then "filtered to nothing" (the query is
  // the cause, and it is recoverable by clearing it); then "the universe itself
  // came back empty", which is a statement about the endpoints, not the filter.
  const loading = matrix.isLoading;
  const filteredOut = !loading && rows.length === 0 && decorated.length > 0;
  const emptyUniverse = !loading && decorated.length === 0;
  const universeStorage = classifyStorageMode(matrix.universeSource);

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-2">
          <div className="relative">
            <Search size={12} className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-text-muted" />
            <input
              ref={searchRef}
              value={ctx.query}
              onChange={(e) => onQuery(e.target.value)}
              placeholder="Search symbol   ( / )"
              className="w-56 rounded-lg border border-bg-border bg-bg-primary/40 py-1.5 pl-7 pr-2 font-mono text-[11.5px] text-text-primary outline-none placeholder:text-text-muted focus:border-accent-blue/60"
            />
          </div>
          <span className="font-mono text-[11px] text-text-muted">
            {loading ? "loading universe…" : `${rows.length} / ${decorated.length} instruments`}
          </span>
          {loading ? (
            <StatusBadge label="loading" variant="info" className="animate-pulse" />
          ) : (
            <>
              <StatusBadge label={`${readiness.fresh} fresh`} variant={readiness.fresh ? "success" : "neutral"} />
              <StatusBadge label={`${readiness.stale} stale`} variant={readiness.stale ? "warn" : "neutral"} />
              <StatusBadge label={`${readiness.absent} no timestamp`} variant="neutral" />
            </>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {/* Replay is claimed ONLY when the session is genuinely closed, i.e.
              when these rows really do describe the previous session. A typed
              as-of never reaches this. */}
          {!sessionOpen ? (
            <DataModeBadge
              mode="historical_replay"
              title="the session is closed — every row below describes the LAST session"
            />
          ) : null}
          {matrix.universeSource ? (
            <>
              <StatusBadge label={`universe: ${matrix.universeSource}`} variant="info" />
              {/* Storage mode is its OWN axis: "snapshot" says how the universe
                  was READ, and says nothing at all about how its numbers were
                  derived. It used to be graded "bar inferred", which claimed a
                  worse provenance than the payload supports. */}
              <StorageModeBadge mode={universeStorage} source={matrix.universeSource} />
            </>
          ) : null}
          <FreshnessBadge asOf={matrix.generatedAt} label="Scan" />
        </div>
      </div>

      {matrix.universeDetail ? (
        <div className="rounded-xl border border-accent-amber/25 bg-accent-amber/5 px-3 py-2 text-[11.5px] text-accent-amber">
          {matrix.universeDetail}
        </div>
      ) : null}
      {matrix.errors.length ? (
        <div className="rounded-xl border border-accent-red/25 bg-accent-red/5 px-3 py-2 text-[11.5px] text-accent-red">
          {matrix.errors.join(" · ")} — affected columns render as unavailable, not as zero.
        </div>
      ) : null}

      <div
        ref={gridRef}
        role="grid"
        aria-rowcount={rows.length + 1}
        aria-busy={loading}
        aria-label="Instrument opportunity matrix"
        tabIndex={0}
        onKeyDown={onKeyDown}
        className="outline-none focus-visible:ring-1 focus-visible:ring-accent-blue/40 rounded-2xl"
      >
        <MatrixTable
          rows={rows}
          loading={loading}
          emptyState={
            filteredOut
              ? {
                  headline: `No instrument matches "${ctx.query.trim()}"`,
                  detail: `${decorated.length} instruments are loaded — the filter, not the data, is hiding them.`,
                  onClear: () => onQuery(""),
                }
              : emptyUniverse
                ? {
                    headline: matrix.hasLoaded
                      ? "The universe endpoints returned no instruments"
                      : "No universe payload has landed",
                    detail: matrix.errors.length
                      ? `This is a data state, not a filter: ${matrix.errors.join(" · ")}.`
                      : matrix.hasLoaded
                        ? "This is a data state, not a filter — the requests completed and carried no rows."
                        : "The requests have not returned a payload, and none reported an error either. This is not an empty universe; it is an unanswered one.",
                    onClear: null,
                  }
                : null
          }
          selectedSymbol={ctx.symbol}
          focusedIndex={focusedIndex}
          focusedColumn={focusedColumn}
          sortKey={ctx.sortKey}
          sortDir={ctx.sortDir}
          onSort={onSort}
          onSelect={(symbol) => {
            const idx = rows.findIndex((r) => r.symbol === symbol);
            if (idx >= 0) commit(idx);
          }}
          onFocus={setFocusedIndex}
        />
      </div>

      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[10.5px] text-text-muted">
        <span className="font-mono">
          ↑↓ row · PgUp/PgDn page · Home/End · ←→ column · s sort · Enter open · / search · Esc close
        </span>
        <span>
          scan {generated.freshness === "absent" ? "time not reported" : `${Math.round((generated.ageSeconds ?? 0) / 60)}m old`}
        </span>
      </div>

      <CoverageStrip matrix={matrix} loading={loading} />
    </div>
  );
}

/**
 * Column coverage — the antidote to a matrix that looks complete because the
 * empty cells are quiet. States, per column, how many rows have a real source
 * and why the rest do not. While loading it says so rather than printing 0/1.
 */
function CoverageStrip({ matrix, loading }: { matrix: UniverseMatrix; loading: boolean }) {
  return (
    <div className="rounded-2xl border border-bg-border bg-bg-secondary/16 px-3 py-2.5">
      <div className="mb-1.5 text-[10px] uppercase tracking-[0.14em] text-text-muted">
        Column coverage — what actually has a source
      </div>
      {loading ? (
        <div className="font-mono text-[11px] text-text-muted">
          counting sources… coverage is unknown until the universe lands.
        </div>
      ) : (
        <div className="flex flex-wrap gap-x-4 gap-y-1.5 text-[11px]">
          {matrix.coverage.map((c) => {
            const full = c.covered >= c.total;
            const none = c.covered === 0;
            return (
              <span
                key={c.key}
                className="inline-flex items-center gap-1.5 font-mono"
                title={c.unavailableReason ?? `${c.covered} of ${c.total} rows carry this column`}
              >
                <span
                  className={
                    none ? "text-text-muted" : full ? "text-accent-green" : "text-accent-amber"
                  }
                >
                  {c.label}
                </span>
                <span className="text-text-muted">
                  {c.covered}/{c.total}
                </span>
                {c.unavailableReason ? <span className="text-text-muted">ⓘ</span> : null}
              </span>
            );
          })}
        </div>
      )}
    </div>
  );
}

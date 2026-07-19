"use client";

/**
 * MatrixTable — hand-rolled fixed-height row virtualization, with the two
 * priority columns FROZEN.
 *
 * The repo has no virtualization dependency and this needs ~60 lines of index
 * arithmetic, so adding one (and the lockfile churn) would cost more than it
 * saves. Fixed `ROW_H` means the window is pure maths: no measurement, no
 * ResizeObserver, no layout thrash, and `scrollToIndex` is a number rather than
 * `scrollIntoView` (which fights a windowed list).
 *
 * At 216 rows only ~26 are mounted at any time.
 *
 * ─── FROZEN COLUMNS (2026-07-19) ────────────────────────────────────────────
 *
 * The grid needs ~1180px. With the detail panel open at a normal desktop width
 * only ~5 of 12 columns were visible, and horizontal scrolling took the
 * INSTRUMENT column off screen — at which point the trader is reading anonymous
 * rows, and the whole point of a comparison screen is gone.
 *
 * So Instrument and Readiness (the two columns that answer "what is this" and
 * "can I believe it") are `position: sticky` and never scroll away. That needs
 * ONE scroll container for both axes: sticky-x tracks the nearest scrollport,
 * so the old outer-x / inner-y split would have pinned the cells to a container
 * that never scrolls horizontally. Hence: one scroller, a sticky header row,
 * and fixed pixel widths for the two frozen tracks so their offsets are known
 * without measuring.
 */
import { clsx } from "clsx";
import { ChevronDown, ChevronUp } from "lucide-react";
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

import { FROZEN_COLUMN_COUNT, GRID_TEMPLATE, MATRIX_COLUMNS, frozenLeftOffset } from "./columns";
import { MatrixRowView } from "./MatrixRow";
import type { MatrixRow } from "./useUniverseMatrix";

export const ROW_H = 30;
const OVERSCAN = 8;
const HEADER_H = 33;

export type MatrixEmptyState = {
  headline: string;
  detail: string;
  /** Non-null only when the trader can act on it (i.e. a filter is the cause). */
  onClear: (() => void) | null;
};

export function MatrixTable({
  rows,
  loading = false,
  emptyState = null,
  selectedSymbol,
  focusedIndex,
  focusedColumn,
  sortKey,
  sortDir,
  onSort,
  onSelect,
  onFocus,
  height = 560,
}: {
  rows: MatrixRow[];
  /** No successful payload yet — render a loading state, NOT an empty grid. */
  loading?: boolean;
  /** Which KIND of empty this is, when there are no rows and none are coming. */
  emptyState?: MatrixEmptyState | null;
  selectedSymbol: string;
  focusedIndex: number;
  focusedColumn: number;
  sortKey: string;
  sortDir: "asc" | "desc";
  onSort: (key: string) => void;
  onSelect: (symbol: string) => void;
  onFocus: (index: number) => void;
  height?: number;
}) {
  const scrollerRef = useRef<HTMLDivElement | null>(null);
  const [scrollTop, setScrollTop] = useState(0);

  const onScroll = useCallback((e: React.UIEvent<HTMLDivElement>) => {
    setScrollTop(e.currentTarget.scrollTop);
  }, []);

  // Keep the keyboard-focused row inside the window by index arithmetic. The
  // sticky header eats HEADER_H of the scrollport, so the bottom edge accounts
  // for it — otherwise the focused row hides underneath it.
  useLayoutEffect(() => {
    const el = scrollerRef.current;
    if (!el || focusedIndex < 0) return;
    const top = focusedIndex * ROW_H;
    const bottom = top + ROW_H;
    if (top < el.scrollTop) el.scrollTop = top;
    else if (bottom > el.scrollTop + el.clientHeight - HEADER_H) {
      el.scrollTop = bottom - el.clientHeight + HEADER_H;
    }
  }, [focusedIndex]);

  useEffect(() => {
    // A new filter/sort can leave the scroller past the end of a shorter list.
    const el = scrollerRef.current;
    if (el && el.scrollTop > rows.length * ROW_H) el.scrollTop = 0;
  }, [rows.length]);

  const start = Math.max(0, Math.floor(scrollTop / ROW_H) - OVERSCAN);
  const visibleCount = Math.ceil(height / ROW_H) + OVERSCAN * 2;
  const end = Math.min(rows.length, start + visibleCount);
  const windowRows = rows.slice(start, end);

  return (
    <div className="overflow-hidden rounded-2xl border border-bg-border bg-bg-secondary/16">
      {/* ONE scroll container for BOTH axes. The page body never scrolls
          horizontally; this does, and the frozen cells stick to its left edge. */}
      <div
        ref={scrollerRef}
        onScroll={onScroll}
        style={{ height: height + HEADER_H }}
        className="relative overflow-auto"
      >
        <div className="min-w-[1180px]">
          <div
            role="row"
            style={{ gridTemplateColumns: GRID_TEMPLATE }}
            className="sticky top-0 z-20 grid items-center gap-2 border-b border-bg-border/60 bg-bg-secondary py-2 text-[10px] uppercase tracking-[0.12em] text-text-muted"
          >
            {MATRIX_COLUMNS.map((col, ci) => {
              const active = sortKey === col.key;
              const frozen = ci < FROZEN_COLUMN_COUNT;
              return (
                <button
                  key={col.key}
                  type="button"
                  role="columnheader"
                  aria-sort={active ? (sortDir === "asc" ? "ascending" : "descending") : "none"}
                  title={col.title}
                  disabled={!col.sortValue}
                  onClick={() => col.sortValue && onSort(col.key)}
                  style={frozen ? { left: frozenLeftOffset(ci) } : undefined}
                  className={clsx(
                    "flex min-w-0 items-center gap-1 truncate",
                    ci === 0 && "pl-3",
                    ci === MATRIX_COLUMNS.length - 1 && "pr-3",
                    col.align === "right" ? "justify-end" : "justify-start",
                    col.sortValue ? "cursor-pointer hover:text-text-secondary" : "cursor-default",
                    active && "text-accent-blue",
                    frozen && "sticky z-30 bg-bg-secondary",
                    frozen && ci === FROZEN_COLUMN_COUNT - 1 && "border-r border-bg-border/60",
                  )}
                >
                  <span className="truncate">{col.label}</span>
                  {active ? (sortDir === "asc" ? <ChevronUp size={11} /> : <ChevronDown size={11} />) : null}
                </button>
              );
            })}
          </div>

          <div style={{ height: rows.length * ROW_H }} className="relative">
            {windowRows.map((row, i) => {
              const index = start + i;
              return (
                <MatrixRowView
                  key={row.symbol}
                  row={row}
                  index={index}
                  top={index * ROW_H}
                  height={ROW_H}
                  selected={row.symbol === selectedSymbol}
                  focused={index === focusedIndex}
                  focusedColumn={focusedColumn}
                  onSelect={onSelect}
                  onFocus={onFocus}
                />
              );
            })}
          </div>

          {/* THREE DISTINCT STATES — never one silent zero-row grid. */}
          {loading ? <LoadingRows /> : null}
          {!loading && rows.length === 0 && emptyState ? (
            <div className="px-3 py-8 text-center">
              <div className="text-[12.5px] font-semibold text-text-secondary">{emptyState.headline}</div>
              <div className="mx-auto mt-1 max-w-md text-[11.5px] text-text-muted">{emptyState.detail}</div>
              {emptyState.onClear ? (
                <button
                  type="button"
                  onClick={emptyState.onClear}
                  className="mt-2.5 rounded-lg border border-bg-border px-2.5 py-1 text-[11px] font-semibold text-text-secondary transition-colors hover:border-accent-blue/50 hover:text-text-primary"
                >
                  Clear the filter
                </button>
              ) : null}
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}

/**
 * A loading state that cannot be mistaken for "no instruments": shimmering
 * placeholder rows plus the words. The shipped build rendered the real
 * zero-row grid while the first request was still in flight, so "0 instruments
 * · No instruments match this filter" was indistinguishable from a genuinely
 * empty universe.
 */
function LoadingRows() {
  return (
    <div className="px-3 py-3" aria-live="polite">
      <div className="mb-2 font-mono text-[11.5px] text-text-muted">
        Loading the universe… no instrument count has been established yet.
      </div>
      <div className="space-y-1.5">
        {Array.from({ length: 8 }).map((_, i) => (
          <div
            key={i}
            className="h-[18px] animate-pulse rounded bg-bg-secondary/50"
            style={{ opacity: 1 - i * 0.09 }}
          />
        ))}
      </div>
    </div>
  );
}

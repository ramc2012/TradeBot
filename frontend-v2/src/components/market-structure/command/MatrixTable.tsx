"use client";

/**
 * MatrixTable — hand-rolled fixed-height row virtualization.
 *
 * The repo has no virtualization dependency and this needs ~60 lines of index
 * arithmetic, so adding one (and the lockfile churn) would cost more than it
 * saves. Fixed `ROW_H` means the window is pure maths: no measurement, no
 * ResizeObserver, no layout thrash, and `scrollToIndex` is a number rather than
 * `scrollIntoView` (which fights a windowed list).
 *
 * At 216 rows only ~26 are mounted at any time.
 */
import { clsx } from "clsx";
import { ChevronDown, ChevronUp } from "lucide-react";
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

import { GRID_TEMPLATE, MATRIX_COLUMNS } from "./columns";
import { MatrixRowView } from "./MatrixRow";
import type { MatrixRow } from "./useUniverseMatrix";

export const ROW_H = 30;
const OVERSCAN = 8;

export function MatrixTable({
  rows,
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

  // Keep the keyboard-focused row inside the window by index arithmetic.
  useLayoutEffect(() => {
    const el = scrollerRef.current;
    if (!el || focusedIndex < 0) return;
    const top = focusedIndex * ROW_H;
    const bottom = top + ROW_H;
    if (top < el.scrollTop) el.scrollTop = top;
    else if (bottom > el.scrollTop + el.clientHeight) el.scrollTop = bottom - el.clientHeight;
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
      {/* The matrix is intentionally wide. It scrolls horizontally INSIDE its
          own container (header and rows together) so the page body never does,
          and so narrowing the viewport for the drawer never clips a column. */}
      <div className="overflow-x-auto">
      <div className="min-w-[1180px]">
      <div
        role="row"
        style={{ gridTemplateColumns: GRID_TEMPLATE }}
        className="grid items-center gap-2 border-b border-bg-border/60 bg-bg-secondary/40 px-3 py-2 text-[10px] uppercase tracking-[0.12em] text-text-muted"
      >
        {MATRIX_COLUMNS.map((col) => {
          const active = sortKey === col.key;
          return (
            <button
              key={col.key}
              type="button"
              role="columnheader"
              aria-sort={active ? (sortDir === "asc" ? "ascending" : "descending") : "none"}
              title={col.title}
              disabled={!col.sortValue}
              onClick={() => col.sortValue && onSort(col.key)}
              className={clsx(
                "flex min-w-0 items-center gap-1 truncate",
                col.align === "right" ? "justify-end" : "justify-start",
                col.sortValue ? "cursor-pointer hover:text-text-secondary" : "cursor-default",
                active && "text-accent-blue",
              )}
            >
              <span className="truncate">{col.label}</span>
              {active ? (sortDir === "asc" ? <ChevronUp size={11} /> : <ChevronDown size={11} />) : null}
            </button>
          );
        })}
      </div>

      <div
        ref={scrollerRef}
        onScroll={onScroll}
        style={{ height }}
        className="relative overflow-y-auto overflow-x-hidden"
      >
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
        {rows.length === 0 ? (
          <div className="px-3 py-6 text-[12px] text-text-muted">
            No instruments match this filter.
          </div>
        ) : null}
      </div>
      </div>
      </div>
    </div>
  );
}

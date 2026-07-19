"use client";

/**
 * One matrix row.
 *
 * HARD RULE — this component must never open a subscription. No `useQuery`,
 * no `useQuote`/`useTickStream`/`useDepth`, no `LiveMarkBadge`, no interval,
 * no per-row effect. 200 rows on screen must cost 200 renders and ZERO
 * connections; anything live belongs in the detail drawer, which exists for
 * exactly one instrument at a time.
 *
 * It is a pure `React.memo` over an already-derived row object, so a 15-second
 * freshness tick re-renders only the ~30 rows inside the window.
 */
import { clsx } from "clsx";
import { memo } from "react";

import { GRID_TEMPLATE, MATRIX_COLUMNS } from "./columns";
import type { MatrixRow as Row } from "./useUniverseMatrix";

function MatrixRowImpl({
  row,
  index,
  top,
  height,
  selected,
  focused,
  focusedColumn,
  onSelect,
  onFocus,
}: {
  row: Row;
  index: number;
  top: number;
  height: number;
  selected: boolean;
  focused: boolean;
  focusedColumn: number;
  onSelect: (symbol: string) => void;
  onFocus: (index: number) => void;
}) {
  return (
    <div
      role="row"
      aria-rowindex={index + 2}
      aria-selected={selected}
      style={{ top, height, gridTemplateColumns: GRID_TEMPLATE }}
      className={clsx(
        "absolute inset-x-0 grid cursor-pointer items-center gap-2 border-b border-bg-border/25 px-3 text-[11.5px]",
        selected
          ? "bg-accent-blue/10"
          : focused
            ? "bg-bg-secondary/45"
            : "hover:bg-bg-secondary/25",
        focused && "ring-1 ring-inset ring-accent-blue/40",
      )}
      onMouseDown={() => onFocus(index)}
      onClick={() => onSelect(row.symbol)}
    >
      {MATRIX_COLUMNS.map((col, ci) => (
        <div
          key={col.key}
          role="gridcell"
          className={clsx(
            "min-w-0 truncate",
            col.align === "right" ? "text-right" : "text-left",
            focused && focusedColumn === ci && "underline decoration-accent-blue/60 underline-offset-4",
          )}
        >
          {col.render(row)}
        </div>
      ))}
    </div>
  );
}

export const MatrixRowView = memo(MatrixRowImpl);

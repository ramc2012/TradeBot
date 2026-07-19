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
 *
 * FROZEN CELLS: the first `FROZEN_COLUMN_COUNT` cells are sticky, so they must
 * carry their own OPAQUE background — otherwise the scrolling columns would
 * show through them. The backdrop is two layers (solid page colour + the row's
 * own translucent state tint) so a frozen cell looks identical to the rest of
 * its row instead of approximating it.
 */
import { clsx } from "clsx";
import { memo } from "react";

import { FROZEN_COLUMN_COUNT, GRID_TEMPLATE, MATRIX_COLUMNS, frozenLeftOffset } from "./columns";
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
  // The row's state tint, named once so the frozen cells can repaint it.
  const tint = selected ? "bg-accent-blue/10" : focused ? "bg-bg-secondary/45" : null;
  const lastIndex = MATRIX_COLUMNS.length - 1;

  return (
    <div
      role="row"
      aria-rowindex={index + 2}
      aria-selected={selected}
      style={{ top, height, gridTemplateColumns: GRID_TEMPLATE }}
      className={clsx(
        "absolute inset-x-0 grid cursor-pointer items-center gap-2 border-b border-bg-border/25 text-[11.5px]",
        tint ?? "hover:bg-bg-secondary/25",
        focused && "ring-1 ring-inset ring-accent-blue/40",
      )}
      onMouseDown={() => onFocus(index)}
      onClick={() => onSelect(row.symbol)}
    >
      {MATRIX_COLUMNS.map((col, ci) => {
        const frozen = ci < FROZEN_COLUMN_COUNT;
        return (
          <div
            key={col.key}
            role="gridcell"
            style={frozen ? { left: frozenLeftOffset(ci), height } : undefined}
            className={clsx(
              "min-w-0 truncate",
              // The row itself has no horizontal padding: the edge padding lives
              // on the first/last CELL, so a frozen cell's natural left offset is
              // 0 and it does not jump when it becomes stuck.
              ci === 0 && "pl-3",
              ci === lastIndex && "pr-3",
              col.align === "right" ? "text-right" : "text-left",
              focused && focusedColumn === ci && "underline decoration-accent-blue/60 underline-offset-4",
              frozen && "sticky z-10 flex items-center",
              frozen && ci === FROZEN_COLUMN_COUNT - 1 && "border-r border-bg-border/40",
            )}
          >
            {frozen ? (
              <>
                <span aria-hidden className="absolute inset-0 -z-10 bg-bg-primary" />
                {tint ? <span aria-hidden className={clsx("absolute inset-0 -z-10", tint)} /> : null}
              </>
            ) : null}
            {col.render(row)}
          </div>
        );
      })}
    </div>
  );
}

export const MatrixRowView = memo(MatrixRowImpl);

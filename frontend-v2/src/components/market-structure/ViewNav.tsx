"use client";

/**
 * ViewNav — the six stable views of the terminal.
 *
 * All six are wired to the SAME pinned context, so switching view never loses
 * the instrument. Command, Structure, Flow and Strategies are built; Risk &
 * Execution and Research are labelled placeholders, deliberately not
 * half-implemented — a panel that renders something plausible from nothing is
 * exactly the failure this rebuild exists to remove.
 *
 * `BUILT` must flip in the SAME commit the view lands. Flipping it early is the
 * worse failure of the two: a "soon" tag over a working view merely understates,
 * while a missing tag over a stub overstates.
 */
import { clsx } from "clsx";

import { VIEWS, VIEW_LABEL, type WorkspaceView } from "./context/schema";

const BUILT: Record<WorkspaceView, boolean> = {
  command: true,
  structure: true,
  flow: true,
  strategies: true,
  risk: false,
  research: false,
};

export function ViewNav({
  view,
  onChange,
}: {
  view: WorkspaceView;
  onChange: (v: WorkspaceView) => void;
}) {
  return (
    <nav className="flex flex-wrap items-center gap-1 border-b border-bg-border/40" aria-label="Workspace views">
      {VIEWS.map((v) => (
        <button
          key={v}
          type="button"
          onClick={() => onChange(v)}
          aria-current={view === v ? "page" : undefined}
          className={clsx(
            "inline-flex items-center gap-1.5 rounded-t-lg border-b-2 px-3 py-2 text-[12.5px] font-semibold transition-colors",
            view === v
              ? "border-accent-blue text-text-primary"
              : "border-transparent text-text-muted hover:text-text-secondary",
          )}
        >
          {VIEW_LABEL[v]}
          {!BUILT[v] ? (
            <span className="rounded px-1 py-0 text-[9px] uppercase tracking-[0.12em] text-text-muted">
              soon
            </span>
          ) : null}
        </button>
      ))}
    </nav>
  );
}

export { BUILT as VIEW_BUILT };

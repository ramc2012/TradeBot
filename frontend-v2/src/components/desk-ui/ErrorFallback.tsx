"use client";

import { AlertTriangle, RotateCcw } from "lucide-react";

/**
 * Reusable error-boundary fallback for app-router error.tsx files. Keeps the
 * terminal chrome (TopBar/Sidebar live above this in the tree) so one desk's
 * runtime error never blanks the whole app — the trader retries just this view
 * while every other desk keeps streaming.
 */
export function ErrorFallback({
  error,
  reset,
  scope = "view",
}: {
  error: Error & { digest?: string };
  reset: () => void;
  scope?: string;
}) {
  return (
    <div className="flex min-h-[50vh] items-center justify-center p-6">
      <div className="w-full max-w-lg rounded-2xl border border-accent-red/30 bg-bg-secondary/30 p-6 text-center">
        <div className="mx-auto mb-3 flex h-11 w-11 items-center justify-center rounded-full bg-accent-red/12 text-accent-red">
          <AlertTriangle size={22} />
        </div>
        <h2 className="text-lg font-semibold text-text-primary">This {scope} hit an error</h2>
        <p className="mt-1.5 text-sm text-text-muted">
          The rest of the terminal is unaffected — other desks keep streaming. Retry to reload just this {scope}.
        </p>
        <pre className="mt-3 max-h-32 overflow-auto rounded-lg border border-bg-border bg-bg-primary/40 p-3 text-left text-[11px] text-text-muted">
          {error?.message || "Unknown error"}
          {error?.digest ? `\n\ndigest: ${error.digest}` : ""}
        </pre>
        <button
          type="button"
          onClick={reset}
          className="mt-4 inline-flex items-center gap-1.5 rounded-lg border border-bg-border bg-bg-primary/40 px-3 py-1.5 text-sm text-text-secondary transition-colors hover:border-bg-active hover:text-text-primary"
        >
          <RotateCcw size={14} />
          Retry
        </button>
      </div>
    </div>
  );
}

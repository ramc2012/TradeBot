"use client";

/**
 * PortfolioReconciliation — one canonical count, discrepancy shown not hidden.
 *
 * The Strategies Overview KPI reduces open positions over 6 lanes (NSE,
 * directional, auction, gann, cbe, commodity) — trusting summary scalars for
 * some and WS-slice lengths for others. The Global Positions page counts REST
 * array rows over 9 books, INCLUDING MACD Refined, US MACD and Fractal. So the
 * two totals legitimately differ (the ~72 vs ~88 gap).
 *
 * Rather than silently pick a number, this panel makes ONE canonical source —
 * buildStrategyBookSummaries over the shared snapshot, all 9 books — and shows,
 * per book: the canonical summary scalar, the actual array-row count, their Δ
 * (amber when a book's scalar disagrees with its rows), and whether the Overview
 * surface counts that book. The footer states the three numbers and the reason.
 */
import { clsx } from "clsx";

import {
  buildOpenPositionRows,
  buildStrategyBookSummaries,
  type AppStrategyPortfolioSnapshot,
} from "@/lib/strategy-position-ledger";

/** Books the Strategies Overview (6-lane) surface does NOT count. */
const OVERVIEW_EXCLUDED = new Set(["macd", "us_macd", "fractal"]);

export function PortfolioReconciliation({
  snapshot,
  overviewDisplayedOpen,
  className,
}: {
  snapshot?: AppStrategyPortfolioSnapshot | null;
  /** The number the Overview KPI actually renders (stream/summary mix), if known. */
  overviewDisplayedOpen?: number | null;
  className?: string;
}) {
  if (!snapshot) return null;

  const summaries = buildStrategyBookSummaries(snapshot);
  const rows = buildOpenPositionRows(snapshot);

  // Array-row count per book source (the Global-page basis).
  const rowCountBySource = new Map<string, number>();
  for (const r of rows) {
    rowCountBySource.set(r.source, (rowCountBySource.get(r.source) ?? 0) + 1);
  }

  const books = summaries.map((b) => {
    const arrayRows = rowCountBySource.get(b.key) ?? 0;
    const scalar = b.openPositions;
    const inOverview = !OVERVIEW_EXCLUDED.has(b.key);
    return { ...b, arrayRows, scalar, inOverview, mismatch: arrayRows !== scalar };
  });

  const canonicalAll = books.reduce((a, b) => a + b.scalar, 0);
  const canonicalOverviewScope = books
    .filter((b) => b.inOverview)
    .reduce((a, b) => a + b.scalar, 0);
  const excludedContribution = canonicalAll - canonicalOverviewScope;
  const arrayAll = books.reduce((a, b) => a + b.arrayRows, 0);

  return (
    <section
      className={clsx(
        "flex flex-col gap-3 rounded-2xl border border-bg-border bg-bg-secondary/20 p-4",
        className,
      )}
    >
      <div>
        <div className="text-sm font-semibold text-text-primary">Portfolio reconciliation</div>
        <div className="mt-1 text-xs text-text-muted">
          One canonical count (all 9 strategy books). The Overview surface counts only 6 lanes —
          the difference is shown per book, never silently resolved.
        </div>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full min-w-[560px] text-left text-[12px]">
          <thead>
            <tr className="border-b border-bg-border text-text-muted">
              <th className="pb-2 pr-3 font-medium uppercase tracking-[0.1em]">Book</th>
              <th className="pb-2 pr-3 font-medium uppercase tracking-[0.1em]">In Overview</th>
              <th className="pb-2 pr-3 text-right font-medium uppercase tracking-[0.1em]">
                Open (canonical)
              </th>
              <th className="pb-2 pr-3 text-right font-medium uppercase tracking-[0.1em]">
                Open (rows)
              </th>
              <th className="pb-2 pr-3 text-right font-medium uppercase tracking-[0.1em]">Δ</th>
            </tr>
          </thead>
          <tbody>
            {books.map((b) => (
              <tr
                key={b.key}
                className={clsx(
                  "border-b border-bg-border/40",
                  b.mismatch ? "bg-accent-amber/[0.06]" : "",
                )}
              >
                <td className="py-1.5 pr-3 text-text-primary">{b.label}</td>
                <td className="py-1.5 pr-3">
                  {b.inOverview ? (
                    <span className="text-text-secondary">counted</span>
                  ) : (
                    <span className="text-accent-amber">excluded</span>
                  )}
                </td>
                <td className="py-1.5 pr-3 text-right font-mono text-text-primary">{b.scalar}</td>
                <td className="py-1.5 pr-3 text-right font-mono text-text-secondary">{b.arrayRows}</td>
                <td
                  className={clsx(
                    "py-1.5 pr-3 text-right font-mono",
                    b.mismatch ? "text-accent-amber" : "text-text-muted",
                  )}
                  title={
                    b.mismatch
                      ? "Summary scalar disagrees with array-row count for this book (server scalar vs materialized rows)."
                      : undefined
                  }
                >
                  {b.arrayRows - b.scalar > 0 ? "+" : ""}
                  {b.arrayRows - b.scalar}
                </td>
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr className="border-t border-bg-border text-[12px]">
              <td className="pt-2 pr-3 font-semibold text-text-primary">All books (canonical)</td>
              <td className="pt-2 pr-3" />
              <td className="pt-2 pr-3 text-right font-mono font-semibold text-text-primary">
                {canonicalAll}
              </td>
              <td className="pt-2 pr-3 text-right font-mono text-text-secondary">{arrayAll}</td>
              <td className="pt-2 pr-3" />
            </tr>
          </tfoot>
        </table>
      </div>

      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 rounded-xl border border-bg-border bg-bg-primary/20 px-3.5 py-2.5 text-[12px]">
        <span>
          Overview scope (6 lanes):{" "}
          <span className="font-mono text-text-primary">{canonicalOverviewScope}</span>
        </span>
        <span>
          All books (9): <span className="font-mono text-text-primary">{canonicalAll}</span>
        </span>
        <span className={excludedContribution !== 0 ? "text-accent-amber" : "text-text-secondary"}>
          Unreconciled Δ:{" "}
          <span className="font-mono">
            {excludedContribution > 0 ? "+" : ""}
            {excludedContribution}
          </span>
        </span>
        {overviewDisplayedOpen != null ? (
          <span className="text-text-muted">
            Overview KPI shows{" "}
            <span className="font-mono">{overviewDisplayedOpen}</span> (stream/summary mix)
          </span>
        ) : null}
      </div>

      <p className="text-[11px] leading-5 text-text-muted">
        Why they differ: the Overview omits MACD Refined, US MACD and Fractal (Δ ={" "}
        {excludedContribution}); it counts summary scalars / WS-slice lengths while Global counts
        REST array rows. Any amber row above is a book whose server scalar disagrees with its
        materialized rows.
      </p>
    </section>
  );
}

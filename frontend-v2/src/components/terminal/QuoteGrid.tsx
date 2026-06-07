"use client";

/**
 * QuoteGrid — the live quote tape (terminal cockpit).
 *
 * Each row is a memoized component that subscribes to its OWN symbol via
 * useQuote(), so a tick on one symbol re-renders only that row, never the whole
 * table — the cell-isolation that lets the grid stay smooth under a fast tape.
 * LTP cells flash green/red on change (usePriceFlash).
 */
import { memo } from "react";

import { formatNumber } from "@/components/desk-ui";
import { useQuote } from "@/hooks/useQuoteStore";
import { usePriceFlash } from "@/hooks/usePriceFlash";

export type QuoteGridProps = {
  symbols: string[];
  /** optional display labels keyed by symbol */
  labels?: Record<string, string>;
  /** show the bid/ask columns (hide for index-only grids) */
  showBook?: boolean;
  /** click-to-focus (drives the depth ladder) */
  onSelect?: (symbol: string) => void;
  selected?: string | null;
};

function pct(ltp?: number | null, ref?: number | null): number | null {
  if (ltp == null || ref == null || ref === 0) return null;
  return ((ltp - ref) / ref) * 100;
}

const QuoteRow = memo(function QuoteRow({
  symbol,
  label,
  showBook,
  onSelect,
  isSelected,
}: {
  symbol: string;
  label: string;
  showBook: boolean;
  onSelect?: (symbol: string) => void;
  isSelected?: boolean;
}) {
  const q = useQuote(symbol);
  const flash = usePriceFlash(q?.ltp);

  const ref = q?.prevClose ?? q?.open ?? null;
  const chg = q?.ltp != null && ref != null ? q.ltp - ref : null;
  const chgPct = pct(q?.ltp, ref);
  const chgTone = chg == null ? "text-text-secondary" : chg > 0 ? "text-accent-green" : chg < 0 ? "text-accent-red" : "text-text-secondary";
  const flashBg = flash === "up" ? "bg-accent-green/20" : flash === "down" ? "bg-accent-red/20" : "bg-transparent";

  return (
    <tr
      onClick={onSelect ? () => onSelect(symbol) : undefined}
      className={`border-b border-bg-border/50 text-[12px] hover:bg-bg-primary/20 ${onSelect ? "cursor-pointer" : ""} ${isSelected ? "bg-bg-primary/30" : ""}`}
    >
      <td className="px-2.5 py-1.5 font-medium text-text-primary">{label}</td>
      <td className={`px-2.5 py-1.5 text-right font-mono tabular-nums transition-colors duration-150 ${flashBg}`}>
        {q?.ltp != null ? formatNumber(q.ltp, 2) : "—"}
      </td>
      <td className={`px-2.5 py-1.5 text-right font-mono tabular-nums ${chgTone}`}>
        {chg != null ? `${chg > 0 ? "+" : ""}${formatNumber(chg, 2)}` : "—"}
      </td>
      <td className={`px-2.5 py-1.5 text-right font-mono tabular-nums ${chgTone}`}>
        {chgPct != null ? `${chgPct > 0 ? "+" : ""}${chgPct.toFixed(2)}%` : "—"}
      </td>
      {showBook ? (
        <>
          <td className="px-2.5 py-1.5 text-right font-mono tabular-nums text-accent-green/90">
            {q?.bid != null ? formatNumber(q.bid, 2) : "—"}
          </td>
          <td className="px-2.5 py-1.5 text-right font-mono tabular-nums text-accent-red/90">
            {q?.ask != null ? formatNumber(q.ask, 2) : "—"}
          </td>
        </>
      ) : null}
      <td className="px-2.5 py-1.5 text-right font-mono tabular-nums text-text-secondary">
        {q?.volume != null ? formatNumber(q.volume, 0) : "—"}
      </td>
      <td className="px-2.5 py-1.5 text-right font-mono tabular-nums text-text-secondary">
        {q?.oi != null ? formatNumber(q.oi, 0) : "—"}
      </td>
    </tr>
  );
});

export function QuoteGrid({ symbols, labels = {}, showBook = true, onSelect, selected }: QuoteGridProps) {
  return (
    <div className="overflow-x-auto rounded-xl border border-bg-border bg-bg-card/40">
      <table className="w-full border-collapse">
        <thead>
          <tr className="border-b border-bg-border text-[10.5px] uppercase tracking-[0.12em] text-text-muted">
            <th className="px-2.5 py-2 text-left">Symbol</th>
            <th className="px-2.5 py-2 text-right">LTP</th>
            <th className="px-2.5 py-2 text-right">Chg</th>
            <th className="px-2.5 py-2 text-right">Chg%</th>
            {showBook ? (
              <>
                <th className="px-2.5 py-2 text-right">Bid</th>
                <th className="px-2.5 py-2 text-right">Ask</th>
              </>
            ) : null}
            <th className="px-2.5 py-2 text-right">Vol</th>
            <th className="px-2.5 py-2 text-right">OI</th>
          </tr>
        </thead>
        <tbody>
          {symbols.map((s) => (
            <QuoteRow
              key={s}
              symbol={s}
              label={labels[s] ?? s}
              showBook={showBook}
              onSelect={onSelect}
              isSelected={selected === s}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
}

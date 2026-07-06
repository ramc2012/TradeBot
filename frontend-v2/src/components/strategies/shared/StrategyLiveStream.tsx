"use client";

/**
 * Terminal-style streaming board shared by every strategy desk.
 *
 * Watchlist prices subscribe to the same cell-isolated /ws/quotes store as the
 * MACD terminal. Positions come from /ws/positions-overview, whose backend
 * overlays hot-cache marks and recomputes P&L on quote events.
 */
import { useMemo } from "react";
import { Radio } from "lucide-react";

import {
  Section,
  StatusBadge,
  formatNumber,
  formatSignedMoney,
  tone,
} from "@/components/desk-ui";
import { QuoteGrid } from "@/components/terminal/QuoteGrid";
import { QuoteConnectionBadge } from "@/components/terminal/LiveMarkBadge";
import { useKnownSymbols } from "@/hooks/useQuoteStore";
import { useStrategyPositionsStream } from "@/hooks/useStrategyPositionsStream";
import { buildOpenPositionRows } from "@/lib/strategy-position-ledger";

export type StrategyWatchSymbol = { symbol: string; label?: string };

function token(value: string): string {
  return value
    .toUpperCase()
    .replace(/^NSE:|^BSE:|^MCX:/, "")
    .replace(/50-INDEX|-INDEX|INDEX/g, "")
    .replace(/[^A-Z0-9]/g, "");
}

function resolveWatchlist(
  requested: StrategyWatchSymbol[],
  known: string[],
  maxRows: number,
): StrategyWatchSymbol[] {
  const seen = new Set<string>();
  const rows: StrategyWatchSymbol[] = [];
  for (const item of requested) {
    if (!item?.symbol) continue;
    const wanted = token(item.symbol);
    const exact = known.find((candidate) => candidate === item.symbol);
    const tokenExact = known.find((candidate) => token(candidate) === wanted);
    const fuzzy = known
      .filter((candidate) => {
        const candidateToken = token(candidate);
        return candidateToken.startsWith(wanted) || wanted.startsWith(candidateToken);
      })
      .sort((left, right) => Math.abs(token(left).length - wanted.length) - Math.abs(token(right).length - wanted.length))[0];
    const symbol = exact ?? tokenExact ?? fuzzy ?? item.symbol;
    if (seen.has(symbol)) continue;
    seen.add(symbol);
    rows.push({ symbol, label: item.label ?? item.symbol });
    if (rows.length >= maxRows) break;
  }
  return rows;
}

export function StrategyLiveStream({
  title,
  watchlist,
  positionSources,
  maxWatchlistRows = 40,
}: {
  title: string;
  watchlist: StrategyWatchSymbol[];
  positionSources: string[];
  maxWatchlistRows?: number;
}) {
  const knownSymbols = useKnownSymbols();
  const positionsStream = useStrategyPositionsStream();
  const resolvedWatchlist = useMemo(
    () => resolveWatchlist(watchlist, knownSymbols, maxWatchlistRows),
    [watchlist, knownSymbols, maxWatchlistRows],
  );
  const labels = useMemo(
    () => Object.fromEntries(resolvedWatchlist.map((item) => [item.symbol, item.label ?? item.symbol])),
    [resolvedWatchlist],
  );
  const positions = useMemo(
    () =>
      buildOpenPositionRows(positionsStream.data).filter((row) =>
        positionSources.includes(row.source),
      ),
    [positionSources, positionsStream.data],
  );

  return (
    <div className="space-y-3">
      <Section
        title={`${title} watchlist`}
        icon={<Radio size={16} className="text-accent-blue" />}
        description="Cell-isolated quote updates from the same low-latency tape as the MACD terminal. Continuous display is capped to protect the browser and backend."
        rightSlot={<QuoteConnectionBadge />}
      >
        {resolvedWatchlist.length ? (
          <QuoteGrid
            symbols={resolvedWatchlist.map((item) => item.symbol)}
            labels={labels}
            showBook
          />
        ) : (
          <div className="rounded-xl border border-bg-border bg-bg-card/40 p-6 text-center text-sm text-text-muted">
            No instruments are currently configured for this strategy watchlist.
          </div>
        )}
      </Section>

      <Section
        title={`${title} open positions`}
        description="Structure refreshes every two seconds; live marks and P&L are recomputed when quote events arrive."
        rightSlot={
          <StatusBadge
            label={positionsStream.isStreamConnected ? "stream live" : "poll fallback"}
            variant={positionsStream.isStreamConnected ? "success" : "warn"}
          />
        }
      >
        <div className="overflow-x-auto rounded-xl border border-bg-border bg-bg-card/40">
          <table className="w-full min-w-[920px] border-collapse text-left text-xs">
            <thead>
              <tr className="border-b border-bg-border text-[10px] uppercase tracking-[0.11em] text-text-muted">
                {[
                  "Underlying",
                  "Contract",
                  "Side",
                  "Qty",
                  "Entry",
                  "Live mark",
                  "Return",
                  "Live P&L",
                  "Updated",
                ].map((label) => (
                  <th key={label} className="px-2.5 py-2 text-right first:text-left">{label}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {positions.map((row) => (
                <tr key={row.id} className="border-b border-bg-border/40 hover:bg-bg-primary/20">
                  <td className="px-2.5 py-2 font-semibold text-text-primary">{row.underlying}</td>
                  <td className="px-2.5 py-2 text-right font-mono text-text-secondary">{row.contract}</td>
                  <td className="px-2.5 py-2 text-right font-mono text-text-secondary">{row.action}</td>
                  <td className="px-2.5 py-2 text-right font-mono">{formatNumber(row.qty, 0)}</td>
                  <td className="px-2.5 py-2 text-right font-mono">{formatNumber(row.entryPrice, 2)}</td>
                  <td className="px-2.5 py-2 text-right font-mono text-accent-blue">{formatNumber(row.currentPrice, 2)}</td>
                  <td className={`px-2.5 py-2 text-right font-mono ${tone(row.returnPct)}`}>{row.returnPct == null ? "—" : `${row.returnPct >= 0 ? "+" : ""}${row.returnPct.toFixed(2)}%`}</td>
                  <td className={`px-2.5 py-2 text-right font-mono ${tone(row.unrealizedPnl)}`}>{formatSignedMoney(row.unrealizedPnl)}</td>
                  <td className="px-2.5 py-2 text-right text-[10px] text-text-muted">{row.updatedAt ? new Date(row.updatedAt).toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata" }) : "live"}</td>
                </tr>
              ))}
              {!positions.length ? (
                <tr><td colSpan={9} className="px-3 py-7 text-center text-sm text-text-muted">No open positions for this strategy.</td></tr>
              ) : null}
            </tbody>
          </table>
        </div>
      </Section>
    </div>
  );
}

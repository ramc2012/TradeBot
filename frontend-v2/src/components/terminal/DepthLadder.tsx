"use client";

/**
 * DepthLadder — live 5-level DOM for one focused symbol (Fyers v3 DepthUpdate).
 * Bid side (green) and ask side (red), bar width ∝ size, with the order count.
 * Index symbols carry no depth, so the ladder shows an explicit "no depth" note.
 */
import { formatNumber } from "@/components/desk-ui";
import { useDepth, type DepthLevel } from "@/hooks/useDepth";

function maxQty(levels: DepthLevel[]): number {
  return levels.reduce((m, l) => Math.max(m, l.q || 0), 0) || 1;
}

export function DepthLadder({ symbol }: { symbol: string | null | undefined }) {
  const { book, connected } = useDepth(symbol);

  if (!symbol) {
    return <div className="rounded-xl border border-bg-border bg-bg-card/40 p-3 text-[12px] text-text-muted">Select a symbol to view depth</div>;
  }

  const bids = book?.bids ?? [];
  const asks = book?.asks ?? [];
  const mq = Math.max(maxQty(bids), maxQty(asks));

  return (
    <div className="rounded-xl border border-bg-border bg-bg-card/40">
      <div className="flex items-center justify-between border-b border-bg-border px-3 py-2">
        <span className="font-mono text-[12px] text-text-primary">{symbol}</span>
        <span className="text-[10.5px] uppercase tracking-[0.12em] text-text-muted">
          {connected ? "depth · live" : "depth · connecting"}
        </span>
      </div>

      {bids.length === 0 && asks.length === 0 ? (
        <div className="px-3 py-4 text-center text-[12px] text-text-muted">
          {connected ? "No depth (index, or awaiting first frame)" : "Connecting…"}
        </div>
      ) : (
        <div className="grid grid-cols-2 gap-px p-2 text-[11.5px]">
          {/* Bids */}
          <div className="space-y-px">
            <div className="grid grid-cols-3 px-1 pb-1 text-[10px] uppercase tracking-wider text-text-muted">
              <span>Ord</span><span className="text-right">Qty</span><span className="text-right">Bid</span>
            </div>
            {bids.slice(0, 5).map((l, i) => (
              <div key={i} className="relative grid grid-cols-3 px-1 py-0.5 font-mono tabular-nums">
                <div className="absolute inset-y-0 right-0 bg-accent-green/15" style={{ width: `${((l.q || 0) / mq) * 100}%` }} />
                <span className="relative z-10 text-text-muted">{l.o || "—"}</span>
                <span className="relative z-10 text-right text-text-secondary">{formatNumber(l.q, 0)}</span>
                <span className="relative z-10 text-right text-accent-green">{formatNumber(l.p, 2)}</span>
              </div>
            ))}
          </div>
          {/* Asks */}
          <div className="space-y-px">
            <div className="grid grid-cols-3 px-1 pb-1 text-[10px] uppercase tracking-wider text-text-muted">
              <span>Ask</span><span className="text-right">Qty</span><span className="text-right">Ord</span>
            </div>
            {asks.slice(0, 5).map((l, i) => (
              <div key={i} className="relative grid grid-cols-3 px-1 py-0.5 font-mono tabular-nums">
                <div className="absolute inset-y-0 left-0 bg-accent-red/15" style={{ width: `${((l.q || 0) / mq) * 100}%` }} />
                <span className="relative z-10 text-accent-red">{formatNumber(l.p, 2)}</span>
                <span className="relative z-10 text-right text-text-secondary">{formatNumber(l.q, 0)}</span>
                <span className="relative z-10 text-right text-text-muted">{l.o || "—"}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {book && (book.tbq != null || book.tsq != null) ? (
        <div className="flex justify-between border-t border-bg-border px-3 py-1.5 text-[10.5px] text-text-muted">
          <span>Σ buy {formatNumber(book.tbq, 0)}</span>
          <span>Σ sell {formatNumber(book.tsq, 0)}</span>
        </div>
      ) : null}
    </div>
  );
}

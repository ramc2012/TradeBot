"use client";

/**
 * Signal board — the latest normalised signal per lane in one sortable,
 * filterable table. Lanes without a current signal still appear, showing "—".
 *
 * Pulls entirely from the LaneView[] the desk already assembled (each lane's
 * live-snapshot / current_signal / scan top-row), so there's no extra fetch.
 */
import { useMemo, useState } from "react";
import { clsx } from "clsx";
import { ArrowDown, ArrowUp, ArrowUpDown, ListChecks } from "lucide-react";

import {
  Section,
  StatusBadge,
  directionTone,
  formatIST,
  formatPct,
} from "@/components/desk-ui";
import { biasBucket, type LaneView } from "./types";

type SortKey = "lane" | "symbol" | "direction" | "confidence" | "time";
type SortDir = "asc" | "desc";
type FilterBias = "all" | "bullish" | "bearish" | "neutral";

/** directionTone only understands CE/PE; widen it to the bias bucket. */
function biasTextTone(direction?: string | null): string {
  const ctone = directionTone(direction);
  if (ctone !== "text-text-muted") return ctone;
  const bucket = biasBucket(direction);
  if (bucket === "bullish") return "text-accent-green";
  if (bucket === "bearish") return "text-accent-red";
  return "text-text-muted";
}

export function SignalBoardTab({ lanes }: { lanes: LaneView[] }) {
  const [sortKey, setSortKey] = useState<SortKey>("confidence");
  const [sortDir, setSortDir] = useState<SortDir>("desc");
  const [filter, setFilter] = useState<FilterBias>("all");

  const onSort = (key: SortKey) => {
    if (key === sortKey) setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    else {
      setSortKey(key);
      setSortDir("desc");
    }
  };

  const rows = useMemo(() => {
    const filtered =
      filter === "all"
        ? lanes
        : lanes.filter((l) => biasBucket(l.signal?.direction ?? l.regime) === filter);

    const getVal = (l: LaneView): string | number => {
      switch (sortKey) {
        case "lane":
          return l.label;
        case "symbol":
          return l.signal?.symbol ?? "";
        case "direction":
          return l.signal?.direction ?? l.regime ?? "";
        case "confidence":
          return l.signal?.confidence ?? -Infinity;
        case "time":
          return l.signal?.time ? new Date(l.signal.time).getTime() || 0 : -Infinity;
        default:
          return l.label;
      }
    };

    return [...filtered].sort((a, b) => {
      const va = getVal(a);
      const vb = getVal(b);
      const cmp =
        typeof va === "string" || typeof vb === "string"
          ? String(va).localeCompare(String(vb))
          : (va as number) - (vb as number);
      return sortDir === "asc" ? cmp : -cmp;
    });
  }, [lanes, sortKey, sortDir, filter]);

  const counts = useMemo(() => {
    let withSignal = 0;
    for (const l of lanes) if (l.signal && (l.signal.direction || l.signal.state)) withSignal += 1;
    return { withSignal, total: lanes.length };
  }, [lanes]);

  return (
    <Section
      title="Signal board"
      icon={<ListChecks size={16} className="text-accent-blue" />}
      description="Latest read per lane — direction, confidence, reason. Sort by any column; filter by bias."
      rightSlot={
        <div className="flex items-center gap-1.5">
          {(["all", "bullish", "bearish", "neutral"] as FilterBias[]).map((f) => (
            <button
              key={f}
              type="button"
              onClick={() => setFilter(f)}
              className={clsx(
                "rounded-full border px-2.5 py-0.5 text-[10.5px] font-semibold uppercase tracking-[0.1em] transition-colors",
                filter === f
                  ? "border-accent-blue/40 bg-accent-blue/10 text-accent-blue"
                  : "border-bg-border bg-bg-primary/15 text-text-muted hover:text-text-secondary",
              )}
            >
              {f}
            </button>
          ))}
        </div>
      }
    >
      <div className="mb-2 text-[11px] text-text-muted">
        {counts.withSignal} / {counts.total} lanes reporting a signal
      </div>
      <div className="-mx-2 overflow-x-auto">
        <table className="w-full min-w-[860px] border-collapse text-left">
          <thead>
            <tr className="border-b border-bg-border/60">
              <SortHead label="Lane" k="lane" sk={sortKey} dir={sortDir} onSort={onSort} />
              <SortHead label="Symbol" k="symbol" sk={sortKey} dir={sortDir} onSort={onSort} />
              <SortHead label="Direction" k="direction" sk={sortKey} dir={sortDir} onSort={onSort} />
              <SortHead label="Conf" k="confidence" sk={sortKey} dir={sortDir} onSort={onSort} align="right" />
              <th className="px-2.5 py-1.5 text-left text-[10px] font-semibold uppercase tracking-[0.12em] text-text-muted">
                State / reason
              </th>
              <SortHead label="As of" k="time" sk={sortKey} dir={sortDir} onSort={onSort} align="right" />
            </tr>
          </thead>
          <tbody>
            {rows.map((l) => {
              const sig = l.signal;
              const dir = sig?.direction ?? null;
              const hasRead = Boolean(sig && (sig.direction || sig.state));
              return (
                <tr key={l.key} className="border-b border-bg-border/25 align-top hover:bg-bg-primary/20">
                  <td className="px-2.5 py-2">
                    <div className="font-semibold text-text-primary">{l.label}</div>
                    {l.degraded ? (
                      <div className="text-[10px] text-accent-red/80">endpoint unavailable</div>
                    ) : null}
                  </td>
                  <td className="px-2.5 py-2 font-mono text-text-secondary">{sig?.symbol ?? "—"}</td>
                  <td className="px-2.5 py-2">
                    {dir ? (
                      <span className={clsx("font-mono text-xs font-semibold uppercase", biasTextTone(dir))}>
                        {dir}
                      </span>
                    ) : (
                      <span className="text-text-muted">—</span>
                    )}
                  </td>
                  <td className="px-2.5 py-2 text-right font-mono text-text-secondary">
                    {sig?.confidence != null ? formatPct(sig.confidence, 0) : "—"}
                  </td>
                  <td className="max-w-[340px] px-2.5 py-2 text-[11.5px] text-text-secondary">
                    {hasRead ? (
                      <div className="flex flex-col gap-0.5">
                        {sig?.state ? <StatusBadge label={sig.state.replace(/_/g, " ")} variant="info" /> : null}
                        {sig?.reason ? <span className="text-text-muted">{sig.reason}</span> : null}
                      </div>
                    ) : (
                      <span className="text-text-muted">no signal</span>
                    )}
                  </td>
                  <td className="px-2.5 py-2 text-right text-[11px] text-text-muted">
                    {sig?.time ? formatIST(sig.time) : "—"}
                  </td>
                </tr>
              );
            })}
            {!rows.length ? (
              <tr>
                <td colSpan={6} className="px-2.5 py-6 text-center text-sm text-text-muted">
                  No lanes match this filter.
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>
    </Section>
  );
}

function SortHead({
  label,
  k,
  sk,
  dir,
  onSort,
  align = "left",
}: {
  label: string;
  k: SortKey;
  sk: SortKey;
  dir: SortDir;
  onSort: (k: SortKey) => void;
  align?: "left" | "right";
}) {
  const active = sk === k;
  return (
    <th className={clsx("select-none px-2.5 py-1.5", align === "right" && "text-right")}>
      <button
        type="button"
        onClick={() => onSort(k)}
        className={clsx(
          "inline-flex items-center gap-1 text-[10px] font-semibold uppercase tracking-[0.12em] transition-colors hover:text-text-primary",
          align === "right" && "flex-row-reverse",
          active ? "text-text-primary" : "text-text-muted",
        )}
      >
        {label}
        {active ? (
          dir === "asc" ? (
            <ArrowUp size={11} />
          ) : (
            <ArrowDown size={11} />
          )
        ) : (
          <ArrowUpDown size={11} className="opacity-40" />
        )}
      </button>
    </th>
  );
}

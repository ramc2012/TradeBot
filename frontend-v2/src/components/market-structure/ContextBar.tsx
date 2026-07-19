"use client";

/**
 * ContextBar — the pinned context, and the only place it can be changed.
 *
 * Every control writes through `setCtx`, i.e. one atomic URL mutation, so there
 * is no intermediate state in which half the workspace has moved to the new
 * instrument and half has not.
 *
 * The truth chips on the right are the SHARED ones (`useSystemState` +
 * `liveVerdict`): this workspace does not invent its own answer to "is anything
 * live" — with the market closed it says so, and the replay pin locks the whole
 * surface out of any live claim.
 */
import { Link2, RotateCcw } from "lucide-react";
import { useState } from "react";

import { StatusBadge } from "@/components/desk-ui";
import { DataModeBadge, ExecutionModeBadge } from "@/components/desk-ui";
import { useSystemState } from "@/hooks/useSystemState";
import { liveVerdict } from "@/lib/market-semantics";

import {
  HORIZONS,
  MARKETS,
  TIMEFRAMES,
  contextHref,
  describeContext,
  type Horizon,
  type MarketKey,
  type Timeframe,
  type WorkspaceContext,
} from "./context/schema";

const SELECT_CLASS =
  "rounded-lg border border-bg-border bg-bg-primary/40 px-2 py-1 font-mono text-[11.5px] text-text-primary outline-none focus:border-accent-blue/60";

export function ContextBar({
  ctx,
  setCtx,
  replayForced,
  rowCount,
  contractHint,
}: {
  ctx: WorkspaceContext;
  setCtx: (patch: Partial<WorkspaceContext>) => void;
  replayForced: boolean;
  rowCount: number;
  contractHint: string | null;
}) {
  const system = useSystemState();
  const [copied, setCopied] = useState(false);

  const sessionOpen = ctx.market === "MCX" ? system.mcxOpen : system.nseOpen;
  const verdict = liveVerdict({
    sessionOpen,
    feedOnline: system.feedOnline,
    dataMode: ctx.replay ? "historical_replay" : "unknown",
    freshness: "absent",
    hasSymbolObservation: false,
  });

  const copyLink = async () => {
    try {
      const url = `${window.location.origin}${contextHref(ctx)}`;
      await navigator.clipboard.writeText(url);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard unavailable — the URL bar already carries the context */
    }
  };

  return (
    <section className="rounded-2xl border border-bg-border bg-bg-secondary/22 px-4 py-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-2">
          <select
            aria-label="Market"
            className={SELECT_CLASS}
            value={ctx.market}
            onChange={(e) => setCtx({ market: e.target.value as MarketKey })}
          >
            {MARKETS.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </select>

          <span className="rounded-lg border border-accent-blue/40 bg-accent-blue/10 px-2.5 py-1 font-mono text-[12.5px] font-semibold text-text-primary">
            {ctx.symbol}
          </span>

          <span className="font-mono text-[11px] text-text-muted" title="Resolved derivative contract for the pin">
            {ctx.contract ?? contractHint ?? "no contract resolved"}
          </span>

          <select
            aria-label="Horizon"
            className={SELECT_CLASS}
            value={ctx.horizon}
            onChange={(e) => setCtx({ horizon: e.target.value as Horizon })}
          >
            {HORIZONS.map((h) => (
              <option key={h} value={h}>
                {h}
              </option>
            ))}
          </select>

          <select
            aria-label="Timeframe"
            className={SELECT_CLASS}
            value={ctx.timeframe}
            onChange={(e) => setCtx({ timeframe: e.target.value as Timeframe })}
          >
            {TIMEFRAMES.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>

          <label className="inline-flex items-center gap-1.5 font-mono text-[11px] text-text-muted">
            as of
            <input
              aria-label="Time frontier"
              value={ctx.asOf}
              onChange={(e) => setCtx({ asOf: e.target.value.trim() || "now" })}
              placeholder="now"
              className="w-40 rounded-lg border border-bg-border bg-bg-primary/40 px-2 py-1 font-mono text-[11.5px] text-text-primary outline-none focus:border-accent-blue/60"
            />
          </label>

          <button
            type="button"
            onClick={() => !replayForced && setCtx({ replay: !ctx.replay })}
            disabled={replayForced}
            title={
              replayForced
                ? "The time frontier is in the past — this workspace IS a replay and the flag cannot be cleared."
                : "Force replay: no surface may render a live verdict."
            }
            className="rounded-lg border border-bg-border px-2 py-1 text-[11px] font-semibold text-text-secondary transition-colors hover:border-accent-amber/50 disabled:opacity-70"
          >
            {ctx.replay ? "replay ON" : "replay off"}
          </button>

          {ctx.asOf !== "now" ? (
            <button
              type="button"
              onClick={() => setCtx({ asOf: "now", replay: false })}
              title="Return the time frontier to now"
              className="inline-flex items-center gap-1 rounded-lg border border-bg-border px-2 py-1 text-[11px] text-text-muted hover:text-text-primary"
            >
              <RotateCcw size={11} /> now
            </button>
          ) : null}
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <StatusBadge label={`${rowCount} instruments`} variant="neutral" />
          <span title={verdict.reason}>
            <StatusBadge label={verdict.label} variant={verdict.variant} />
          </span>
          {ctx.replay ? <DataModeBadge mode="historical_replay" /> : null}
          <ExecutionModeBadge mode={system.modeKnown ? (system.isLive ? "live" : "paper") : "none"} />
          <button
            type="button"
            onClick={copyLink}
            title="Copy a deep link to this exact context"
            className="inline-flex items-center gap-1.5 rounded-lg border border-bg-border px-2 py-1 text-[11px] text-text-muted transition-colors hover:text-text-primary"
          >
            <Link2 size={11} />
            {copied ? "copied" : "link"}
          </button>
        </div>
      </div>

      <div className="mt-1.5 font-mono text-[10.5px] text-text-muted">{describeContext(ctx)}</div>
    </section>
  );
}

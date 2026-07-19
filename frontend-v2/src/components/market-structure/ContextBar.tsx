"use client";

/**
 * ContextBar — the pinned context, and the only place it can be changed.
 *
 * Every control writes through `setCtx`, i.e. one atomic URL mutation, so there
 * is no intermediate state in which half the workspace has moved to the new
 * instrument and half has not.
 *
 * ─── TWO HONESTY FIXES LIVE HERE (2026-07-19) ───────────────────────────────
 *
 * 1. THE LIVE VERDICT IS DERIVED, NOT HARD-CODED. This bar used to call
 *    `liveVerdict({... freshness: "absent", hasSymbolObservation: false })`, so
 *    the header could only ever say "no observation" — on a Monday, with the
 *    session open, the feed connected and fresh rows on screen. It now reads the
 *    SELECTED matrix row (the very object whose freshness the Readiness cell
 *    renders) through the shared `pinnedObservationOf` derivation, so the header
 *    and the row are the same claim by construction.
 *
 * 2. AS-OF / HORIZON / TIMEFRAME ARE LABELLED NOT-APPLIED. No wired endpoint
 *    accepts them (see `context/schema.ts`), and the shipped build turned a
 *    past `asOf` into `dataMode: historical_replay`, painting REPLAY over
 *    live-latest data. The controls remain — they are useful annotations on a
 *    shared link — but they are marked CONTEXT ONLY, and nothing here may
 *    derive a replay claim from them. The only replay claim this workspace
 *    makes is the honest one: the session is CLOSED, so these numbers are the
 *    last session's.
 */
import { Link2, RotateCcw } from "lucide-react";
import { useState } from "react";

import { StatusBadge } from "@/components/desk-ui";
import { DataModeBadge, ExecutionModeBadge } from "@/components/desk-ui";
import { useSystemState } from "@/hooks/useSystemState";
import { liveVerdict, liveVerdictInputFor, pinnedObservationOf } from "@/lib/market-semantics";

import type { MatrixRow } from "./command/useUniverseMatrix";
import {
  HORIZONS,
  MARKETS,
  TIMEFRAMES,
  UNAPPLIED_NOTE,
  contextHref,
  describeContext,
  type Horizon,
  type MarketKey,
  type Timeframe,
  type WorkspaceContext,
} from "./context/schema";

const SELECT_CLASS =
  "rounded-lg border border-bg-border bg-bg-primary/40 px-2 py-1 font-mono text-[11.5px] text-text-primary outline-none focus:border-accent-blue/60";

/** Applied to every control that does NOT reach a query. */
const UNAPPLIED_CLASS = "border-dashed opacity-80";

export function ContextBar({
  ctx,
  setCtx,
  asOfPinnedButUnapplied,
  rowCount,
  contractHint,
  selectedRow,
  sessionOpen,
  feedOnline,
  matrixLoading,
}: {
  ctx: WorkspaceContext;
  setCtx: (patch: Partial<WorkspaceContext>) => void;
  /** The trader typed a past as-of. It does NOT move the data — say so. */
  asOfPinnedButUnapplied: boolean;
  rowCount: number;
  contractHint: string | null;
  /** The SAME decorated row the matrix renders. The verdict's only data input. */
  selectedRow: MatrixRow | null;
  sessionOpen: boolean;
  feedOnline: boolean;
  matrixLoading: boolean;
}) {
  const system = useSystemState();
  const [copied, setCopied] = useState(false);

  // THE derivation — shared with the matrix row, never invented here.
  const observation = pinnedObservationOf(selectedRow);
  const verdict = liveVerdict(liveVerdictInputFor({ sessionOpen, feedOnline, observation }));

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
            aria-label="Horizon (context only — not applied to data)"
            title={UNAPPLIED_NOTE}
            className={`${SELECT_CLASS} ${UNAPPLIED_CLASS}`}
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
            aria-label="Timeframe (context only — not applied to data)"
            title={UNAPPLIED_NOTE}
            className={`${SELECT_CLASS} ${UNAPPLIED_CLASS}`}
            value={ctx.timeframe}
            onChange={(e) => setCtx({ timeframe: e.target.value as Timeframe })}
          >
            {TIMEFRAMES.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>

          <label
            className="inline-flex items-center gap-1.5 font-mono text-[11px] text-text-muted"
            title={UNAPPLIED_NOTE}
          >
            as of
            <input
              aria-label="Time frontier (context only — not applied to data)"
              value={ctx.asOf}
              onChange={(e) => setCtx({ asOf: e.target.value.trim() || "now" })}
              placeholder="now"
              className={`w-40 rounded-lg border border-bg-border bg-bg-primary/40 px-2 py-1 font-mono text-[11.5px] text-text-primary outline-none focus:border-accent-blue/60 ${UNAPPLIED_CLASS}`}
            />
          </label>

          <button
            type="button"
            onClick={() => setCtx({ suppressLive: !ctx.suppressLive })}
            title={
              "Mute live claims: no surface may render a live verdict. This SUPPRESSES a claim; " +
              "it never re-labels the data as a replay of another session."
            }
            className="rounded-lg border border-bg-border px-2 py-1 text-[11px] font-semibold text-text-secondary transition-colors hover:border-accent-amber/50"
          >
            {ctx.suppressLive ? "live claims muted" : "mute live claims"}
          </button>

          {ctx.asOf !== "now" ? (
            <button
              type="button"
              onClick={() => setCtx({ asOf: "now" })}
              title="Clear the (unapplied) time-frontier annotation"
              className="inline-flex items-center gap-1 rounded-lg border border-bg-border px-2 py-1 text-[11px] text-text-muted hover:text-text-primary"
            >
              <RotateCcw size={11} /> now
            </button>
          ) : null}
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <StatusBadge label={`${rowCount} instruments`} variant="neutral" />
          {ctx.suppressLive ? (
            <span title={`live claims muted by the trader — the underlying verdict is: ${verdict.reason}`}>
              <StatusBadge label="live claims muted" variant="neutral" />
            </span>
          ) : (
            <span
              title={`${verdict.reason} — derived from ${
                selectedRow ? `${selectedRow.symbol}'s own observation` : "no pinned row"
              }${matrixLoading ? " (universe still loading)" : ""}`}
            >
              <StatusBadge label={verdict.label} variant={verdict.variant} />
            </span>
          )}
          {/* The ONLY replay badge this bar renders, and it comes from the row's
              own data mode — i.e. the session is closed — never from a
              user-typed as-of. */}
          {observation.dataMode === "historical_replay" ? (
            <DataModeBadge
              mode="historical_replay"
              title="the session is closed, so these numbers describe the LAST session"
            />
          ) : null}
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

      {/*
        The wiring gap, stated on screen rather than implied by a badge. A typed
        but unapplied as-of is the most dangerous control on this bar, so it
        gets its own amber line the moment it is non-default.
      */}
      <div
        className={
          asOfPinnedButUnapplied
            ? "mt-1.5 rounded-lg border border-accent-amber/30 bg-accent-amber/5 px-2.5 py-1.5 text-[11px] text-accent-amber"
            : "mt-1 text-[10.5px] text-text-muted"
        }
      >
        {asOfPinnedButUnapplied ? (
          <>
            <span className="font-semibold uppercase tracking-[0.12em]">as-of not applied</span> —{" "}
            {`you pinned ${ctx.asOf}, but no wired endpoint accepts a time frontier, so every panel below is showing the LATEST available data. None of it is a replay of that instant, and none of it will be labelled as one.`}
          </>
        ) : (
          UNAPPLIED_NOTE
        )}
      </div>
    </section>
  );
}

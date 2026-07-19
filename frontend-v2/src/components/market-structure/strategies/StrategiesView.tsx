"use client";

/**
 * StrategiesView — the cross-lane comparison screen.
 *
 * ONE ROW PER HORIZON, one column per policy. Every intersection is one of four
 * honest things and never a fifth:
 *
 *   · a real state from that lane's payload
 *   · UNAVAILABLE, with the reason (usually: the state exists only inside a
 *     heavy per-symbol snapshot that has not been requested)
 *   · "does not operate at this horizon", with the evidence
 *   · permanently unavailable — the whole SCALP row, which needs aggressor
 *     prints and/or real L2 depth and can never be satisfied on today's feeds
 *
 * Disagreement is surfaced, never averaged. The decision waterfall underneath
 * turns each policy's badge list into an auditable path: data → structure →
 * flow → anti-chase → risk → execution, with the payload field behind every
 * verdict one click away.
 *
 * This view receives the SAME decorated row the header and matrix read. It
 * computes no freshness of its own — a second decoration pass is exactly the
 * drift the workspace's one-pass rule exists to close.
 */
import { useState } from "react";

import { StatusBadge } from "@/components/desk-ui";
import {
  HORIZON_BLURB,
  HORIZON_LABEL,
  SCALP_UNAVAILABLE,
  TRADING_HORIZONS,
  type TradingHorizon,
} from "@/lib/lane-taxonomy";
import { missingCapability } from "@/lib/market-semantics";
import {
  POLICY_COLUMNS,
  POLICY_COLUMN_LABEL,
  POLICY_COLUMN_MEMBERS,
  POLICY_LABEL,
  policyOperatesAt,
  type PolicyId,
} from "@/lib/policy-state";

import type { MatrixRow } from "../command/useUniverseMatrix";
import { describeContext, type WorkspaceContext } from "../context/schema";
import { LaneGroupGrid } from "../lanes/LaneGroupGrid";
import { DecisionWaterfall } from "./DecisionWaterfall";
import { DisagreementStrip } from "./DisagreementStrip";
import { PolicyCell } from "./PolicyCell";
import { useStrategyMatrix } from "./useStrategyMatrix";

export function StrategiesView({ ctx, row }: { ctx: WorkspaceContext; row: MatrixRow | null }) {
  const matrix = useStrategyMatrix(ctx, row, ctx.view === "strategies");
  const [focus, setFocus] = useState<PolicyId>("convergence");
  const focused = matrix.entries[focus];

  return (
    <div className="space-y-4">
      <section className="rounded-2xl border border-bg-border bg-bg-secondary/22 p-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <h2 className="text-base font-semibold text-text-primary">
              Policies on {row?.symbol ?? ctx.symbol}
            </h2>
            <p className="mt-0.5 max-w-3xl text-[11.5px] leading-4 text-text-muted">
              One row per horizon, one column per policy. MP+OF is two policy ids sharing one
              library — index long-premium and commodity futures — so they are two chips in one
              column rather than one averaged cell, because they answer for different instruments.
            </p>
          </div>
          {matrix.convergenceDetailLoading ? (
            <StatusBadge label="loading convergence detail" variant="info" />
          ) : null}
        </div>
        <div className="mt-2 rounded-xl border border-bg-border/70 bg-bg-primary/20 px-3 py-2">
          <div className="text-[10px] uppercase tracking-[0.14em] text-text-muted">Pinned context</div>
          <div className="mt-0.5 font-mono text-[11px] text-text-secondary">{describeContext(ctx)}</div>
        </div>
      </section>

      <DisagreementStrip
        disagreements={matrix.disagreements}
        opinions={matrix.opinions}
        symbol={row?.symbol ?? ctx.symbol}
      />

      {/* ── The horizon × policy grid ── */}
      <section className="overflow-x-auto rounded-2xl border border-bg-border bg-bg-secondary/22 p-4">
        <div className="min-w-[900px]">
          <div className="grid grid-cols-[160px_repeat(4,minmax(0,1fr))] gap-2">
            <div className="text-[10px] uppercase tracking-[0.14em] text-text-muted">Horizon</div>
            {POLICY_COLUMNS.map((c) => (
              <div key={c} className="text-[10px] uppercase tracking-[0.14em] text-text-muted">
                {POLICY_COLUMN_LABEL[c]}
              </div>
            ))}
          </div>

          {TRADING_HORIZONS.map((h) =>
            h === "scalp" ? (
              <ScalpRow key={h} />
            ) : (
              <div
                key={h}
                className="mt-2 grid grid-cols-[160px_repeat(4,minmax(0,1fr))] items-start gap-2"
              >
                <div className="pt-1">
                  <div className="text-[12px] font-semibold text-text-primary">
                    {HORIZON_LABEL[h]}
                  </div>
                  <p className="mt-0.5 text-[10px] leading-4 text-text-muted">{HORIZON_BLURB[h]}</p>
                  {!anyPolicyAt(h) ? (
                    <p className="mt-1 text-[10px] leading-4 text-text-muted">
                      None of the four policies operates here. Lanes that DO hold at this horizon
                      are listed in the grouping below — they are not policies in this matrix.
                    </p>
                  ) : null}
                </div>
                {POLICY_COLUMNS.map((col) => (
                  <div key={col} className="space-y-1.5">
                    {POLICY_COLUMN_MEMBERS[col].map((pid) => (
                      <PolicyCell
                        key={pid}
                        data={matrix.entries[pid].cell}
                        horizon={h}
                        loader={matrix.entries[pid].loader}
                        selected={focus === pid}
                        onSelect={() => setFocus(pid)}
                      />
                    ))}
                  </div>
                ))}
              </div>
            ),
          )}
        </div>
      </section>

      <DecisionWaterfall
        title={`Decision waterfall · ${POLICY_LABEL[focused.policyId]}`}
        stages={focused.stages}
      />

      <LaneGroupGrid />
    </div>
  );
}

function anyPolicyAt(h: TradingHorizon): boolean {
  return POLICY_COLUMNS.some((c) =>
    POLICY_COLUMN_MEMBERS[c].some((pid) => policyOperatesAt(pid, h)),
  );
}

/**
 * The scalp row. Rendered as a single permanently-unavailable band rather than
 * four empty cells: the reason is one data-capability gap, not four separate
 * lane gaps, and four blank cells would read as "not built yet".
 */
function ScalpRow() {
  return (
    <div className="mt-2 grid grid-cols-[160px_repeat(4,minmax(0,1fr))] items-start gap-2">
      <div className="pt-1">
        <div className="text-[12px] font-semibold text-text-muted line-through decoration-text-muted/50">
          {HORIZON_LABEL.scalp}
        </div>
        <p className="mt-0.5 text-[10px] leading-4 text-text-muted">{HORIZON_BLURB.scalp}</p>
      </div>
      <div className="col-span-4 rounded-xl border border-bg-border/60 bg-bg-primary/10 p-3">
        <div className="text-[10px] font-semibold uppercase tracking-[0.14em] text-text-muted">
          permanently unavailable · not an empty row
        </div>
        <p className="mt-1 max-w-prose text-[11px] leading-5 text-text-secondary/85">
          {SCALP_UNAVAILABLE.reason}
        </p>
        <ul className="mt-1.5 flex flex-wrap gap-x-4 gap-y-0.5">
          {SCALP_UNAVAILABLE.missingCapabilities.map(missingCapability).map((c) => (
            <li key={c.key} className="text-[10.5px] text-text-muted">
              <span className="font-mono">{c.key}</span> — {c.label}
            </li>
          ))}
        </ul>
        <p className="mt-1.5 font-mono text-[10px] leading-4 text-text-muted/80">
          {SCALP_UNAVAILABLE.citation}
        </p>
      </div>
    </div>
  );
}

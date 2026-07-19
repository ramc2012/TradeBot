"use client";

/**
 * InstrumentDrawer — the detail side of "summary in the matrix, detail on
 * demand". Selecting a row opens this; it never navigates, so the matrix keeps
 * its scroll position, its focus and its keyboard.
 *
 * Deliberately NOT a modal dialog: a focus trap would take the arrow keys away
 * from the grid, and the whole point is that the trader can keep walking the
 * universe with the drawer open.
 *
 * Everything shown here states its provenance and its blockers. Where a lane
 * gives a number that is not a plan (the classic `reward_risk: 9.53` with
 * `stop: null`), it is rendered through the shared R/R verdict and suppressed
 * with the reason — display honesty only, no enforcement is touched.
 */
import { clsx } from "clsx";
import { ExternalLink, X } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";

import {
  MetricTile,
  ProvenanceChip,
  StatusBadge,
  SufficiencyBadge,
  formatNumber,
} from "@/components/desk-ui";
import { laneDisplayStatus, laneDisplayVariant, useLaneRegistry } from "@/hooks/useLaneRegistry";
import { rrRender } from "@/lib/market-semantics";

import { deskHref, type WorkspaceContext } from "../context/schema";
import type { MatrixRow } from "../command/useUniverseMatrix";
import { useAuctionDetail, useConvergenceDetail } from "./useInstrumentDetail";

/** Lanes this workspace actually draws from, per market. */
const LANE_KEYS: Record<string, string[]> = {
  NSE: ["institutional_convergence", "auction_intelligence", "market_intelligence", "s2_index_mp_macd"],
  MCX: ["institutional_convergence_commodity", "auction_intelligence_commodity", "commodity_mp_orderflow", "commodity_mp_history"],
};

export function InstrumentDrawer({
  ctx,
  row,
  onClose,
  onAuctionLoaded,
}: {
  ctx: WorkspaceContext;
  row: MatrixRow | null;
  onClose: () => void;
  onAuctionLoaded: (symbol: string, regime: string | null, allowed: boolean | null, reasons: string[]) => void;
}) {
  const [auctionRequested, setAuctionRequested] = useState(false);
  const detail = useConvergenceDetail(row?.symbol ?? "", ctx.market, !!row);
  const auction = useAuctionDetail(row?.symbol ?? "", auctionRequested && !!row);
  const lanes = useLaneRegistry();

  const auctionAnalysis = auction.data?.analysis ?? null;
  const auctionSymbol = row?.symbol ?? "";

  // Hand the loaded state back so the matrix's Auction column can fill in for
  // the symbols the trader actually opened — in an effect, never during render.
  useEffect(() => {
    if (!auctionSymbol || !auctionAnalysis) return;
    onAuctionLoaded(
      auctionSymbol,
      auctionAnalysis.regime ?? null,
      auctionAnalysis.risk?.allowed ?? null,
      (auctionAnalysis.risk?.reasons ?? []).map(String),
    );
  }, [auctionSymbol, auctionAnalysis, onAuctionLoaded]);

  // Re-pinning must not silently pull a 59 KB snapshot for the new instrument:
  // the request is per-instrument and expires with the selection.
  useEffect(() => {
    setAuctionRequested(false);
  }, [auctionSymbol]);

  if (!row) return null;

  const rr = rrRender({
    entry: row.risk.entry,
    stop: row.risk.stop,
    target1: row.risk.target1,
  });

  const relevantLanes = (lanes.data?.lanes ?? []).filter((l) =>
    (LANE_KEYS[ctx.market] ?? []).includes(l.key),
  );

  return (
    <aside
      aria-label={`${row.symbol} detail`}
      className="flex h-full w-full flex-col gap-3 overflow-y-auto rounded-2xl border border-bg-border bg-bg-secondary/28 p-4"
    >
      <header className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <h2 className="truncate font-mono text-lg font-semibold text-text-primary">{row.symbol}</h2>
            <StatusBadge label={String(row.kind || "instrument").toLowerCase()} variant="neutral" />
          </div>
          <div className="mt-0.5 truncate font-mono text-[11px] text-text-muted">
            {row.contract ?? "no resolved contract"}
            {row.expiry ? ` · expiry ${row.expiry}` : ""}
            {row.lotSize ? ` · lot ${row.lotSize}` : ""}
          </div>
          <ProvenanceChip provenance={row.provenance} density="caption" />
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close detail"
          className="rounded-lg border border-bg-border p-1.5 text-text-muted transition-colors hover:text-text-primary"
        >
          <X size={14} />
        </button>
      </header>

      <div className="grid grid-cols-2 gap-2">
        <MetricTile
          size="sm"
          label="Spot"
          value={row.spot == null ? "—" : formatNumber(row.spot, 2)}
          detail={row.spot == null ? "not reported" : undefined}
        />
        <MetricTile
          size="sm"
          label="Sufficiency"
          value={row.sufficiency}
          detail={row.readinessReasons[0] ?? "no caveats reported"}
          color={row.sufficiency === "ok" ? undefined : "text-accent-amber"}
        />
      </div>

      <DrawerSection title="Convergence evidence">
        {!row.convergence.available ? (
          <Empty reason={row.convergence.reason} />
        ) : (
          <div className="space-y-2">
            <div className="flex flex-wrap items-center gap-2">
              <StatusBadge label={row.convergence.setupState ?? "no state"} variant="info" />
              <StatusBadge label={`action ${row.convergence.action ?? "—"}`} variant="neutral" />
              <StatusBadge
                label={`confirmations ${row.convergence.confirmations ?? "—"}/${row.convergence.required ?? "—"}`}
                variant={
                  row.convergence.confirmations != null &&
                  row.convergence.required != null &&
                  row.convergence.confirmations >= row.convergence.required
                    ? "success"
                    : "warn"
                }
              />
              {row.convergence.direction ? (
                <StatusBadge label={row.convergence.direction} variant="neutral" />
              ) : null}
            </div>
            {row.convergence.blocked.length ? (
              <div className="flex flex-wrap gap-1.5">
                {row.convergence.blocked.map((b) => (
                  <StatusBadge key={b} label={`⊘ ${b}`} variant="warn" />
                ))}
              </div>
            ) : (
              <div className="text-[11.5px] text-text-muted">No blockers reported this cycle.</div>
            )}
            <div className="font-mono text-[11px] text-text-muted">
              cvd source {row.convergence.cvdSource ?? "—"} · footprint source {row.convergence.footprintSource ?? "—"} · sides inferred from quotes (no aggressor tape)
              {row.tickAgeMs != null
                ? ` · tick age ${formatNumber(row.tickAgeMs / 1000, 1)}s${row.tickLimitMs != null ? ` / ${formatNumber(row.tickLimitMs / 1000, 0)}s limit` : ""}`
                : ""}
            </div>
            <div className="text-[11px] text-text-muted">
              {detail.isFetching
                ? "loading full detail (levels, TPO, CVD series)…"
                : detail.data?.result
                  ? "full detail loaded for this instrument only"
                  : detail.isError
                    ? "detail unavailable"
                    : "detail not loaded"}
            </div>
          </div>
        )}
      </DrawerSection>

      <DrawerSection title="Risk plan">
        {!row.risk.available ? (
          <Empty reason={row.risk.reason} />
        ) : (
          <div className="space-y-2">
            <div className="grid grid-cols-3 gap-2">
              <MetricTile size="sm" label="Entry" value={row.risk.entry == null ? "—" : formatNumber(row.risk.entry, 2)} />
              <MetricTile size="sm" label="Stop" value={row.risk.stop == null ? "—" : formatNumber(row.risk.stop, 2)} />
              <MetricTile size="sm" label="Target 1" value={row.risk.target1 == null ? "—" : formatNumber(row.risk.target1, 2)} />
            </div>
            <MetricTile
              size="sm"
              label="Reward / risk"
              value={rr.ok ? rr.text : "R/R unavailable"}
              detail={rr.ok ? "entry, stop and target-1 all present" : rr.reason}
              color={rr.ok ? undefined : "text-text-muted"}
            />
            {!rr.ok ? (
              <div className="text-[11.5px] text-accent-amber">
                Trade plan incomplete — missing {rr.missing.join(", ")}. Any reward/risk the lane
                reports is computed off a plan that does not exist, so it is not rendered as an R.
              </div>
            ) : null}
          </div>
        )}
      </DrawerSection>

      <DrawerSection title="Structure (MP)">
        {!row.mp.available ? (
          <Empty reason={row.mp.reason} />
        ) : (
          <div className="grid grid-cols-3 gap-2">
            <MetricTile size="sm" label="Regime" value={(row.mp.regime ?? "—").replace(/_/g, " ")} detail={row.mp.dayType ?? undefined} />
            <MetricTile size="sm" label="POC" value={row.mp.poc == null ? "—" : formatNumber(row.mp.poc, 1)} detail={`VA ${formatNumber(row.mp.val, 0)}–${formatNumber(row.mp.vah, 0)}`} />
            <MetricTile
              size="sm"
              label="Value migration"
              value={(row.mp.migrationState ?? "—").replace(/_/g, " ")}
              detail={row.mp.migrationDirection ?? undefined}
            />
          </div>
        )}
      </DrawerSection>

      <DrawerSection title="Flow (MP + OF) · sides inferred from quotes">
        {!row.mpof.available ? (
          <Empty reason={row.mpof.reason} />
        ) : (
          <div className="space-y-1.5">
            <div className="flex flex-wrap items-center gap-2">
              <StatusBadge label={row.mpof.signal ?? row.mpof.candidate ?? "no signal"} variant={row.mpof.signal ? "success" : "neutral"} />
              {row.mpof.ofSource ? <StatusBadge label={`of ${row.mpof.ofSource} · sides inferred`} variant="warn" /> : null}
              {row.mpof.confidence != null ? (
                <StatusBadge label={`confidence ${row.mpof.confidence.toFixed(2)}`} variant="neutral" />
              ) : null}
            </div>
            {row.mpof.detail ? (
              <div className="text-[11.5px] text-text-muted">{row.mpof.detail}</div>
            ) : null}
          </div>
        )}
      </DrawerSection>

      <DrawerSection title="Options / OI context">
        {!row.options.available ? (
          <Empty reason={row.options.reason} />
        ) : (
          <div className="grid grid-cols-2 gap-2">
            <MetricTile size="sm" label="ATM PCR" value={row.options.pcr == null ? "—" : formatNumber(row.options.pcr, 2)} />
            <MetricTile size="sm" label="ATM IV" value={row.options.iv == null ? "—" : `${formatNumber(row.options.iv, 2)}%`} />
            <MetricTile size="sm" label="Call wall" value={row.options.callWall == null ? "—" : formatNumber(row.options.callWall, 0)} />
            <MetricTile size="sm" label="Put wall" value={row.options.putWall == null ? "—" : formatNumber(row.options.putWall, 0)} />
          </div>
        )}
      </DrawerSection>

      <DrawerSection title="Auction state">
        {auctionAnalysis ? (
          <div className="space-y-1.5">
            <div className="flex flex-wrap items-center gap-2">
              <StatusBadge label={(auctionAnalysis.regime ?? "no regime").replace(/_/g, " ")} variant="info" />
              <StatusBadge
                label={auctionAnalysis.risk?.allowed ? "risk allows" : "risk blocks"}
                variant={auctionAnalysis.risk?.allowed ? "success" : "warn"}
              />
            </div>
            {(auctionAnalysis.risk?.reasons ?? []).length ? (
              <SufficiencyBadge sufficiency="degraded" reasons={(auctionAnalysis.risk?.reasons ?? []).map(String)} />
            ) : null}
          </div>
        ) : (
          <div className="space-y-2">
            <div className="text-[11.5px] text-text-muted">
              The auction snapshot is 59 KB per symbol and has no universe-scale endpoint, so it is
              never fetched for the matrix. Load it for this instrument only.
            </div>
            <button
              type="button"
              onClick={() => setAuctionRequested(true)}
              disabled={auction.isFetching}
              className="rounded-lg border border-bg-border px-2.5 py-1 text-[11px] font-semibold text-text-secondary transition-colors hover:border-accent-blue/50 hover:text-text-primary disabled:opacity-50"
            >
              {auction.isFetching ? "Loading…" : auction.isError ? "Retry auction snapshot" : "Load auction state"}
            </button>
          </div>
        )}
      </DrawerSection>

      <DrawerSection title="Portfolio intent">
        <div className="font-mono text-[11.5px] text-text-secondary">
          {row.intent.legs === 0
            ? "flat — no open legs in the polled books"
            : `${row.intent.side?.toLowerCase()} · ${row.intent.legs} leg(s) · net ${row.intent.netQty}`}
        </div>
        {row.intent.lanes.length ? (
          <div className="mt-1 flex flex-wrap gap-1.5">
            {row.intent.lanes.map((l) => (
              <StatusBadge key={l} label={l} variant="neutral" />
            ))}
          </div>
        ) : null}
        <div className="mt-1 text-[10.5px] text-text-muted">
          Composed from the real book and the auction paper book. Lanes whose only position endpoint
          is megabyte-scale are read from their own desk, not here.
        </div>
      </DrawerSection>

      <DrawerSection title="Lanes feeding this instrument">
        <div className="space-y-1">
          {relevantLanes.length === 0 ? (
            <Empty reason="lane registry not loaded" />
          ) : (
            relevantLanes.map((l) => (
              <div key={l.key} className="flex items-center justify-between gap-2">
                <span className="truncate font-mono text-[11px] text-text-secondary">{l.label || l.key}</span>
                <StatusBadge label={laneDisplayStatus(l)} variant={laneDisplayVariant(l)} />
              </div>
            ))
          )}
        </div>
      </DrawerSection>

      <DrawerSection title="Open in a desk">
        <div className="flex flex-wrap gap-2">
          <DeskLink label="Convergence" href={deskHref("/strategies/institutional-convergence", ctx)} />
          <DeskLink label="Auction IQ" href={deskHref("/strategies/auction", ctx)} />
          <DeskLink label={ctx.market === "MCX" ? "Commodity" : "MP Live"} href={deskHref(ctx.market === "MCX" ? "/strategies/commodity" : "/strategies/mp", ctx)} />
        </div>
      </DrawerSection>
    </aside>
  );
}

function DrawerSection({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="rounded-xl border border-bg-border/70 bg-bg-primary/20 p-3">
      <div className="mb-2 text-[10px] uppercase tracking-[0.14em] text-text-muted">{title}</div>
      {children}
    </section>
  );
}

function Empty({ reason }: { reason: string | null }) {
  return (
    <div className={clsx("font-mono text-[11.5px] text-text-muted")}>
      — unavailable{reason ? ` — ${reason}` : ""}
    </div>
  );
}

function DeskLink({ label, href }: { label: string; href: string }) {
  return (
    <Link
      href={href}
      className="inline-flex items-center gap-1.5 rounded-lg border border-bg-border px-2.5 py-1 text-[11px] font-semibold text-text-secondary transition-colors hover:border-accent-blue/50 hover:text-text-primary"
    >
      {label}
      <ExternalLink size={11} />
    </Link>
  );
}

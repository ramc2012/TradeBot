"use client";

/**
 * Lane-audit invariants board — the body of the old /lane-health route,
 * now rendered as the "Lane invariants" tab of /system (the route itself
 * redirects there). Ported from raw Tailwind greys + emoji glyphs to
 * desk-ui tokens in the same move.
 *
 * A lane is trustworthy only when all six invariants pass. Audits are
 * written by `python -m audits.lane_audit --lane <lane>`.
 */
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { clsx } from "clsx";
import { AlertTriangle, CheckCircle2, MinusCircle, ShieldAlert } from "lucide-react";

import { Section, StatusBadge } from "@/components/desk-ui";
import { api } from "@/lib/api";

type Invariant =
  | "data_integrity_pass"
  | "replay_parity_pass"
  | "gate_attribution_pass"
  | "backtest_parity_pass"
  | "trade_recon_pass"
  | "edge_persistence_pass";

const INVARIANTS: { key: Invariant; metaKey: string; label: string }[] = [
  { key: "data_integrity_pass", metaKey: "data_integrity", label: "Data integrity" },
  { key: "replay_parity_pass", metaKey: "replay_parity", label: "Replay parity" },
  { key: "gate_attribution_pass", metaKey: "gate_attribution", label: "Gate attribution" },
  { key: "backtest_parity_pass", metaKey: "backtest_parity", label: "Backtest⇄live parity" },
  { key: "trade_recon_pass", metaKey: "trade_reconciliation", label: "Trade reconciliation" },
  { key: "edge_persistence_pass", metaKey: "edge_persistence", label: "Edge persistence" },
];

type InvStatus = "pass" | "fail" | "na";

interface LatestRow {
  lane: string;
  audit_date: string;
  overall_status: "green" | "yellow" | "red";
  data_integrity_pass: boolean;
  replay_parity_pass: boolean;
  gate_attribution_pass: boolean;
  backtest_parity_pass: boolean;
  trade_recon_pass: boolean;
  edge_persistence_pass: boolean;
  signals_emitted: number;
  signals_blocked_total: number;
  gate_block_breakdown: Record<string, number>;
  replay_signals: number;
  live_signals: number;
  replay_match_count: number;
  trades_booked: number;
  expectancy_60d: number | null;
  expectancy_baseline: number | null;
  drift_pct: number | null;
  report_path: string | null;
  status?: string;
  metadata?: {
    invariant_status?: Record<string, InvStatus>;
  };
}

const laneApi = {
  list: () => api.get<{ lanes: string[] }>("/api/lane-health/lanes").then((r) => r.data),
  latest: (lane: string) =>
    api.get<LatestRow>(`/api/lane-health/${lane}/latest`).then((r) => r.data),
};

const OVERALL_VARIANT: Record<string, "success" | "warn" | "error"> = {
  green: "success",
  yellow: "warn",
  red: "error",
};

const INV_STYLE: Record<InvStatus, { icon: typeof CheckCircle2; iconCls: string; cls: string }> = {
  pass: { icon: CheckCircle2, iconCls: "text-accent-green", cls: "border-accent-green/30 bg-accent-green/8" },
  fail: { icon: AlertTriangle, iconCls: "text-accent-red", cls: "border-accent-red/30 bg-accent-red/8" },
  na: { icon: MinusCircle, iconCls: "text-accent-amber", cls: "border-accent-amber/30 bg-accent-amber/8" },
};

export default function LaneHealthBoard() {
  const [selected, setSelected] = useState<string>("s1");
  const lanes = useQuery({ queryKey: ["lane-health-lanes"], queryFn: laneApi.list });
  const latest = useQuery({
    queryKey: ["lane-health-latest", selected],
    queryFn: () => laneApi.latest(selected),
    refetchInterval: 30_000,
  });

  return (
    <div className="space-y-4">
      <Section
        title="Lane audits"
        icon={<ShieldAlert size={16} />}
        description={
          <>
            Per-lane audit results. A lane is trustworthy only when all six invariants pass. Audits are written by{" "}
            <code>python -m audits.lane_audit --lane &lt;lane&gt;</code>.
          </>
        }
      >
        <div className="flex flex-wrap gap-2">
          {(lanes.data?.lanes ?? ["s1"]).map((lane) => (
            <button
              key={lane}
              type="button"
              onClick={() => setSelected(lane)}
              className={clsx(
                "rounded-lg border px-3 py-1 text-[12px] font-semibold transition-colors",
                selected === lane
                  ? "border-accent-blue/55 bg-accent-blue/15 text-accent-blue"
                  : "border-bg-border bg-bg-primary/30 text-text-secondary hover:border-bg-active hover:text-text-primary",
              )}
            >
              {lane.toUpperCase()}
            </button>
          ))}
        </div>
      </Section>

      {latest.isLoading ? (
        <div className="rounded-xl border border-dashed border-bg-border/60 px-4 py-10 text-center text-sm text-text-muted">
          <span className="animate-pulse">Loading lane audit…</span>
        </div>
      ) : null}
      {latest.data && latest.data.status === "no-audit-yet" ? (
        <div className="rounded-xl border border-accent-amber/30 bg-accent-amber/8 p-4 text-sm text-text-secondary">
          No audit recorded for <strong className="text-text-primary">{selected.toUpperCase()}</strong> yet.
        </div>
      ) : null}
      {latest.data && !latest.data.status ? <LaneCard row={latest.data} /> : null}
    </div>
  );
}

function LaneCard({ row }: { row: LatestRow }) {
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <StatusBadge label={row.overall_status.toUpperCase()} variant={OVERALL_VARIANT[row.overall_status] ?? "neutral"} />
        <span className="text-sm text-text-muted">
          Audit date {row.audit_date} · report {row.report_path ? <code>{row.report_path}</code> : "—"}
        </span>
      </div>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-3">
        {INVARIANTS.map((inv) => {
          const triState: InvStatus =
            (row.metadata?.invariant_status?.[inv.metaKey] as InvStatus | undefined) ??
            (row[inv.key] ? "pass" : "fail");
          const { icon: Icon, iconCls, cls } = INV_STYLE[triState];
          return (
            <div key={inv.key} className={clsx("flex items-center gap-2 rounded-xl border p-3", cls)} title={triState}>
              <Icon size={16} className={iconCls} />
              <span className="text-sm font-medium text-text-primary">{inv.label}</span>
            </div>
          );
        })}
      </div>

      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        <StatBox title="Signal cadence">
          <KV label="Emitted (window)" v={row.signals_emitted} />
          <KV label="Blocked" v={row.signals_blocked_total} />
          <KV label="Replay signals" v={row.replay_signals} />
          <KV label="Live signals" v={row.live_signals} />
          <KV label="Replay matches" v={row.replay_match_count} />
        </StatBox>

        <StatBox title="Edge">
          <KV label="60d expectancy" v={fmtPct(row.expectancy_60d)} />
          <KV label="1y baseline" v={fmtPct(row.expectancy_baseline)} />
          <KV label="Drift" v={fmtPct(row.drift_pct)} />
          <KV label="Trades booked" v={row.trades_booked} />
        </StatBox>
      </div>

      <StatBox title="Top gate blockers">
        <GateBreakdown breakdown={row.gate_block_breakdown ?? {}} />
      </StatBox>
    </div>
  );
}

function StatBox({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="rounded-2xl border border-bg-border bg-bg-secondary/28 p-4">
      <div className="mb-2 text-[10.5px] uppercase tracking-[0.16em] text-text-muted">{title}</div>
      <div className="space-y-1">{children}</div>
    </div>
  );
}

function KV({ label, v }: { label: string; v: React.ReactNode }) {
  return (
    <div className="flex justify-between text-sm">
      <span className="text-text-secondary">{label}</span>
      <span className="font-mono text-text-primary">{v ?? "—"}</span>
    </div>
  );
}

function GateBreakdown({ breakdown }: { breakdown: Record<string, number> }) {
  const total = Object.values(breakdown).reduce((a, b) => a + b, 0) || 1;
  const sorted = Object.entries(breakdown).sort((a, b) => b[1] - a[1]).slice(0, 5);
  if (sorted.length === 0) return <div className="text-sm text-text-muted">No gate events.</div>;
  return (
    <div className="space-y-1">
      {sorted.map(([gate, n]) => (
        <div key={gate} className="flex items-center gap-2 text-sm">
          <span className="w-48 truncate text-text-secondary" title={gate}>
            {gate}
          </span>
          <div className="h-2 flex-1 rounded bg-bg-primary/50">
            <div className="h-2 rounded bg-accent-blue" style={{ width: `${(n / total) * 100}%` }} />
          </div>
          <span className="w-12 text-right font-mono text-text-primary">{n}</span>
        </div>
      ))}
    </div>
  );
}

function fmtPct(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  return `${v.toFixed(1)}%`;
}

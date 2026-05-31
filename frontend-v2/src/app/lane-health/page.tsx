"use client";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { clsx } from "clsx";
import { useState } from "react";

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

export default function LaneHealthPage() {
  const [selected, setSelected] = useState<string>("s1");
  const lanes = useQuery({ queryKey: ["lane-health-lanes"], queryFn: laneApi.list });
  const latest = useQuery({
    queryKey: ["lane-health-latest", selected],
    queryFn: () => laneApi.latest(selected),
    refetchInterval: 30_000,
  });

  return (
    <div className="p-6 space-y-6 max-w-5xl">
      <div>
        <h1 className="text-2xl font-semibold">Lane health</h1>
        <p className="text-sm text-gray-500 mt-1">
          Per-lane audit results. A lane is trustworthy only when all six invariants pass.
          Audits are written by <code>python -m audits.lane_audit --lane &lt;lane&gt;</code>.
        </p>
      </div>

      <div className="flex gap-2">
        {(lanes.data?.lanes ?? ["s1"]).map((lane) => (
          <button
            key={lane}
            onClick={() => setSelected(lane)}
            className={clsx(
              "px-3 py-1 rounded text-sm border",
              selected === lane
                ? "bg-blue-600 text-white border-blue-600"
                : "bg-white border-gray-300 hover:bg-gray-50",
            )}
          >
            {lane.toUpperCase()}
          </button>
        ))}
      </div>

      {latest.isLoading && <div>Loading…</div>}
      {latest.data && latest.data.status === "no-audit-yet" && (
        <div className="p-4 bg-yellow-50 border border-yellow-200 rounded">
          No audit recorded for <strong>{selected.toUpperCase()}</strong> yet.
        </div>
      )}
      {latest.data && !latest.data.status && <LaneCard row={latest.data} />}
    </div>
  );
}

function LaneCard({ row }: { row: LatestRow }) {
  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <OverallBadge status={row.overall_status} />
        <span className="text-sm text-gray-500">
          Audit date {row.audit_date} · report{" "}
          {row.report_path ? <code>{row.report_path}</code> : "—"}
        </span>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
        {INVARIANTS.map((inv) => {
          const triState: InvStatus =
            (row.metadata?.invariant_status?.[inv.metaKey] as InvStatus | undefined) ??
            (row[inv.key] ? "pass" : "fail");
          const { icon, cls } = STATUS_STYLE[triState];
          return (
            <div
              key={inv.key}
              className={clsx("p-3 rounded border flex items-center gap-2", cls)}
              title={triState}
            >
              <span className="text-lg">{icon}</span>
              <span className="text-sm font-medium">{inv.label}</span>
            </div>
          );
        })}
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
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

const STATUS_STYLE: Record<InvStatus, { icon: string; cls: string }> = {
  pass: { icon: "🟢", cls: "bg-green-50 border-green-200" },
  fail: { icon: "🔴", cls: "bg-red-50 border-red-200" },
  na:   { icon: "🟡", cls: "bg-yellow-50 border-yellow-200" },
};

function OverallBadge({ status }: { status: string }) {
  const cls = {
    green: "bg-green-600",
    yellow: "bg-yellow-500",
    red: "bg-red-600",
  }[status] ?? "bg-gray-500";
  return (
    <span className={clsx("text-white text-sm font-semibold px-3 py-1 rounded", cls)}>
      {status.toUpperCase()}
    </span>
  );
}

function StatBox({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="p-4 border rounded">
      <div className="text-xs uppercase tracking-wide text-gray-500 mb-2">{title}</div>
      <div className="space-y-1">{children}</div>
    </div>
  );
}

function KV({ label, v }: { label: string; v: React.ReactNode }) {
  return (
    <div className="flex justify-between text-sm">
      <span className="text-gray-600">{label}</span>
      <span className="font-mono">{v ?? "—"}</span>
    </div>
  );
}

function GateBreakdown({ breakdown }: { breakdown: Record<string, number> }) {
  const total = Object.values(breakdown).reduce((a, b) => a + b, 0) || 1;
  const sorted = Object.entries(breakdown).sort((a, b) => b[1] - a[1]).slice(0, 5);
  if (sorted.length === 0) return <div className="text-sm text-gray-500">No gate events.</div>;
  return (
    <div className="space-y-1">
      {sorted.map(([gate, n]) => (
        <div key={gate} className="flex items-center gap-2 text-sm">
          <span className="w-48 truncate" title={gate}>{gate}</span>
          <div className="flex-1 bg-gray-100 rounded h-2">
            <div
              className="bg-blue-500 h-2 rounded"
              style={{ width: `${(n / total) * 100}%` }}
            />
          </div>
          <span className="w-12 text-right font-mono">{n}</span>
        </div>
      ))}
    </div>
  );
}

function fmtPct(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  return `${v.toFixed(1)}%`;
}

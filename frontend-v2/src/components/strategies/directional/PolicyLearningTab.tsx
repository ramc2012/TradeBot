"use client";

/**
 * Policy & learning tab — global bandit state and strategy params,
 * pulled from /api/directional-options/policy.
 */
import { clsx } from "clsx";
import { useQuery } from "@tanstack/react-query";
import { Activity, Brain, GitBranch, ShieldCheck, Target } from "lucide-react";

import { MetricTile, REFRESH_MS, Section, formatMoney, formatNumber, formatSignedMoney, tone } from "@/components/desk-ui";
import { api as apiClient } from "@/lib/api";

type StrategyParams = {
  universe?: string[];
  risk_pct?: number | null;
  premium_cap_pct?: number | null;
  planned_stop_pct?: number | null;
  profit_target_pct?: number | null;
  trail_giveback_pct?: number | null;
  daily_loss_cap_r?: number | null;
  weekly_loss_cap_r?: number | null;
  starting_equity?: number | null;
  min_hold_bars?: number | null;
  one_position_per_symbol?: boolean | null;
};

type PolicySnap = {
  enabled?: boolean;
  reason?: string;
  n_seen?: number;
  feature_dim?: number;
  feature_version?: number;
  size_buckets?: Record<string, { mean_R?: number | null; n?: number }>;
  pending_positions?: string[];
  strategy_params?: StrategyParams;
  learning_summary?: {
    bucket_trades?: number;
    mean_R?: number | null;
    best_multiplier?: string | null;
    best_mean_R?: number | null;
    worst_multiplier?: string | null;
    worst_mean_R?: number | null;
    pending_rewards?: number;
    untrained_buckets?: number;
  };
  paper?: Record<string, number>;
};

function fmtPct(v: number | null | undefined, digits = 2): string {
  if (v == null) return "—";
  return `${(v * 100).toFixed(digits)}%`;
}

export default function PolicyLearningTab() {
  const { data, isLoading } = useQuery({
    queryKey: ["directional", "policy-state"],
    queryFn: async () => (await apiClient.get("/api/directional-options/policy")).data as PolicySnap,
    refetchInterval: REFRESH_MS.summary,
    refetchOnWindowFocus: false,
  });

  if (isLoading) {
    return (
      <div className="rounded-2xl border border-bg-border bg-bg-secondary/20 p-4 text-sm text-text-muted">
        Loading policy state…
      </div>
    );
  }

  if (!data?.enabled) {
    return (
      <div className="rounded-2xl border border-accent-amber/30 bg-accent-amber/8 p-4 text-sm text-accent-amber">
        RL policy is disabled. {data?.reason}
      </div>
    );
  }

  const buckets = data.size_buckets || {};
  const totalTrades = Object.values(buckets).reduce((a, b) => a + (b?.n || 0), 0);
  const orderedKeys = Object.keys(buckets).sort((a, b) => parseFloat(a) - parseFloat(b));
  const params = data.strategy_params || {};
  const learning = data.learning_summary || {};
  const paper = data.paper || {};

  return (
    <div className="space-y-4">
      <Section title="RL control tower" icon={<Brain size={16} />}>
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-6">
          <MetricTile label="Trades learned" value={String(data.n_seen ?? 0)} detail={`${totalTrades} size outcomes`} />
          <MetricTile label="Mean learned R" value={learning.mean_R != null ? formatNumber(learning.mean_R, 3) : "—"} detail={`${learning.bucket_trades ?? 0} bucket closes`} color={tone(learning.mean_R)} />
          <MetricTile label="Best multiplier" value={learning.best_multiplier ? `${parseFloat(learning.best_multiplier).toFixed(1)}×` : "—"} detail={learning.best_mean_R != null ? `${formatNumber(learning.best_mean_R, 3)}R mean` : "needs closes"} color={tone(learning.best_mean_R)} />
          <MetricTile label="Pending reward" value={String(learning.pending_rewards ?? (data.pending_positions || []).length)} detail="Open positions not closed" />
          <MetricTile label="Feature set" value={`v${data.feature_version ?? "—"}`} detail={`${data.feature_dim ?? 0} signals`} />
          <MetricTile label="Paper equity" value={formatMoney(paper.total_equity)} detail={`Open ${formatSignedMoney(paper.unrealized_pnl)}`} color={tone(paper.total_pnl)} />
        </div>
      </Section>

      <Section title="Size-multiplier posterior" icon={<Target size={16} />}>
        <table className="w-full text-[12px]">
          <thead className="text-[10.5px] uppercase tracking-wide text-text-muted">
            <tr className="border-b border-bg-border/60">
              <th className="px-2 py-2 text-left">Multiplier</th>
              <th className="px-2 py-2 text-right">Trades closed</th>
              <th className="px-2 py-2 text-right">Mean R</th>
              <th className="px-2 py-2 text-left">Share</th>
              <th className="px-2 py-2 text-left">State</th>
            </tr>
          </thead>
          <tbody>
            {orderedKeys.length === 0 ? (
              <tr><td colSpan={5} className="py-4 text-center text-text-muted">No size buckets registered.</td></tr>
            ) : (
              orderedKeys.map((k) => {
                const b = buckets[k] || {};
                const share = totalTrades > 0 ? (b.n || 0) / totalTrades : 0;
                const isBest = learning.best_multiplier === k && (b.n || 0) > 0;
                const isWorst = learning.worst_multiplier === k && (b.n || 0) > 0;
                return (
                  <tr key={k} className="border-b border-bg-border/30">
                    <td className="px-2 py-2 font-mono text-text-primary">{parseFloat(k).toFixed(1)}×</td>
                    <td className="px-2 py-2 text-right font-mono">{b.n ?? 0}</td>
                    <td className={clsx("px-2 py-2 text-right font-mono font-semibold", tone(b.mean_R))}>
                      {b.mean_R != null ? (b.mean_R >= 0 ? "+" : "") + b.mean_R.toFixed(3) : "—"}
                    </td>
                    <td className="px-2 py-2">
                      <div className="h-1.5 w-full overflow-hidden rounded bg-bg-border/30">
                        <div className="h-full bg-accent-blue/60" style={{ width: `${Math.min(share * 100, 100).toFixed(1)}%` }} />
                      </div>
                      <div className="mt-0.5 text-[10.5px] text-text-muted">{(share * 100).toFixed(1)}%</div>
                    </td>
                    <td className="px-2 py-2">
                      {isBest ? <StatusPill label="favored" color="green" /> : isWorst ? <StatusPill label="lagging" color="amber" /> : (b.n || 0) === 0 ? <StatusPill label="explore" color="blue" /> : <StatusPill label="trained" />}
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </Section>

      <Section title="Paper feedback loop" icon={<Activity size={16} />}>
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-6">
          <MetricTile size="sm" label="Open positions" value={String(paper.open_positions ?? 0)} detail={`Exposure ${formatMoney(paper.open_premium_value)}`} />
          <MetricTile size="sm" label="Closed trades" value={String(paper.total_trades ?? 0)} detail={`Policy ${paper.policy_trades ?? 0}`} />
          <MetricTile size="sm" label="Realized R" value={formatNumber(paper.realized_r_total, 2)} detail={`Avg ${formatNumber(paper.avg_r_multiple, 3)}R`} color={tone(paper.realized_r_total)} />
          <MetricTile size="sm" label="Profit factor" value={formatNumber(paper.profit_factor, 2)} detail={`Win ${paper.win_rate != null ? (paper.win_rate * 100).toFixed(1) + "%" : "—"}`} />
          <MetricTile size="sm" label="Open risk" value={formatMoney(paper.open_risk_budget)} detail={`${formatNumber(paper.open_risk_R, 2)}R live`} color={tone(paper.open_risk_R)} />
          <MetricTile size="sm" label="Drawdown" value={paper.max_drawdown != null ? `${(paper.max_drawdown * 100).toFixed(2)}%` : "—"} detail={`Worst ${formatSignedMoney(paper.worst_trade)}`} color={tone((paper.max_drawdown || 0) * -1)} />
        </div>
        {(data.pending_positions || []).length ? (
          <div className="mt-3 flex flex-wrap gap-2">
            {(data.pending_positions || []).slice(0, 12).map((id) => (
              <span key={id} className="rounded border border-bg-border bg-bg-primary/20 px-2 py-1 font-mono text-[10.5px] text-text-secondary">
                {id.slice(0, 10)}
              </span>
            ))}
          </div>
        ) : null}
      </Section>

      <Section title="Strategy parameters" icon={<ShieldCheck size={16} />}>
        <div className="grid gap-5 md:grid-cols-2 text-[11.5px]">
          <div>
            <Row label="Universe" value={(params.universe || []).join(" · ") || "—"} />
            <Row label="Starting equity" value={`₹${(params.starting_equity ?? 0).toLocaleString("en-IN")}`} />
            <Row label="Base risk per trade" value={fmtPct(params.risk_pct, 3)} />
            <Row label="Premium cap" value={params.premium_cap_pct == null ? "None (RL-managed)" : fmtPct(params.premium_cap_pct, 2)} accent={params.premium_cap_pct == null ? "ok" : undefined} />
            <Row label="One position per symbol" value={params.one_position_per_symbol ? "ON" : "OFF"} accent={params.one_position_per_symbol ? "ok" : "warn"} />
          </div>
          <div>
            <Row label="Planned stop" value={fmtPct(params.planned_stop_pct, 1)} />
            <Row label="Profit target" value={fmtPct(params.profit_target_pct, 1)} />
            <Row label="Trail giveback" value={fmtPct(params.trail_giveback_pct, 1)} />
            <Row label="Min hold bars" value={params.min_hold_bars != null ? `${params.min_hold_bars} bars` : "—"} />
            <Row label="Daily / weekly loss cap" value={`${params.daily_loss_cap_r ?? "—"}R / ${params.weekly_loss_cap_r ?? "—"}R`} accent="warn" />
          </div>
        </div>

        <div className="mt-4 inline-flex items-center gap-2 rounded-lg border border-accent-green/30 bg-accent-green/8 p-3 text-[11.5px] text-accent-green">
          <GitBranch size={13} />
          <strong>RL-managed:</strong> trade/skip, size multiplier ({"{"}0.5×, 1.0×, 1.5×, 2.0×{"}"}), strike choice all learned online.
        </div>
      </Section>
    </div>
  );
}

function StatusPill({ label, color }: { label: string; color?: "green" | "amber" | "blue" }) {
  return (
    <span className={clsx(
      "inline-flex rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.12em]",
      color === "green"
        ? "border-accent-green/30 bg-accent-green/8 text-accent-green"
        : color === "amber"
          ? "border-accent-amber/30 bg-accent-amber/8 text-accent-amber"
          : color === "blue"
            ? "border-accent-blue/30 bg-accent-blue/8 text-accent-blue"
            : "border-bg-border bg-bg-primary/20 text-text-secondary",
    )}>
      {label}
    </span>
  );
}

function Row({ label, value, accent }: { label: string; value: string; accent?: "ok" | "warn" }) {
  return (
    <div className="flex items-baseline justify-between gap-3 border-b border-bg-border/40 py-2 last:border-b-0">
      <span className="text-text-secondary">{label}</span>
      <span className={clsx(
        "font-mono",
        accent === "ok" ? "text-accent-green" : accent === "warn" ? "text-accent-amber" : "text-text-primary",
      )}>
        {value}
      </span>
    </div>
  );
}

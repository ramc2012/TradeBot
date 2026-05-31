"use client";

/**
 * Policy & Learning tab — the GLOBAL view of the RL policy state.
 *
 * Unlike PolicyDecisionPanel (per-symbol live decision), this tab
 * shows the bandit's accumulated learning across all symbols /
 * sessions:
 *
 *   - n_seen: number of closed trades that have fed the value posterior
 *   - per-size-bucket Mean R + trade count (which multiplier is winning)
 *   - pending positions: positions currently registered with the policy
 *     waiting for close (cross-check vs paper open count)
 *   - strategy params snapshot
 *
 * Reads `/api/directional-options/policy`.
 */
import { clsx } from "clsx";
import { useQuery } from "@tanstack/react-query";
import { Brain, GitBranch } from "lucide-react";

import { getDirectionalOptionsPolicy } from "@/lib/api";
import StrategyParamsPanel, {
  type StrategyParams,
} from "./StrategyParamsPanel";

type PolicySnapshot = {
  enabled?: boolean;
  reason?: string;
  n_seen?: number;
  feature_dim?: number;
  size_buckets?: Record<string, { mean_R?: number | null; n?: number }>;
  pending_positions?: string[];
  strategy_params?: StrategyParams;
};

function fmt(v: number | null | undefined, digits = 3, signed = false): string {
  if (v == null || Number.isNaN(v)) return "—";
  const s = v.toFixed(digits);
  return signed && v >= 0 ? `+${s}` : s;
}

function bucketTone(v: number | null | undefined): string {
  if (v == null) return "text-text-muted";
  if (v > 0.2) return "text-emerald-300";
  if (v > 0) return "text-emerald-200";
  if (v > -0.2) return "text-amber-300";
  return "text-rose-300";
}

export default function PolicyLearningTab() {
  const { data, isLoading } = useQuery({
    queryKey: ["do-policy"],
    queryFn: async () => (await getDirectionalOptionsPolicy()).data as PolicySnapshot,
    refetchInterval: 30_000,
    refetchOnWindowFocus: false,
  });

  if (isLoading) {
    return <div className="rounded-2xl border border-bg-border bg-bg-secondary/20 p-4 text-sm text-text-muted">Loading policy state…</div>;
  }

  if (!data?.enabled) {
    return (
      <div className="rounded-2xl border border-amber-500/30 bg-amber-500/8 p-4 text-sm text-amber-200">
        RL policy is disabled. {data?.reason || "Set config.rl_policy.enabled = True and restart the backend."}
      </div>
    );
  }

  const buckets = data.size_buckets || {};
  const totalTrades = Object.values(buckets).reduce((acc, b) => acc + (b?.n || 0), 0);
  const orderedKeys = Object.keys(buckets).sort((a, b) => parseFloat(a) - parseFloat(b));

  return (
    <div className="space-y-5">
      {/* Header tile-strip */}
      <section className="rounded-[26px] border border-bg-border bg-bg-secondary/24 p-5">
        <div className="mb-3 flex items-center gap-2 text-sm font-semibold text-text-primary">
          <Brain size={16} />
          Contextual Bandit · Global state
        </div>
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <div className="rounded-2xl border border-bg-border bg-bg-primary/16 p-3">
            <div className="text-[10.5px] uppercase tracking-[0.16em] text-text-muted">Trades fed to model</div>
            <div className="mt-1 font-mono text-xl font-semibold text-text-primary">{data.n_seen ?? 0}</div>
            <div className="mt-0.5 text-[11px] text-text-muted">{totalTrades} across size buckets</div>
          </div>
          <div className="rounded-2xl border border-bg-border bg-bg-primary/16 p-3">
            <div className="text-[10.5px] uppercase tracking-[0.16em] text-text-muted">Feature dimension</div>
            <div className="mt-1 font-mono text-xl font-semibold text-text-primary">{data.feature_dim ?? 0}</div>
            <div className="mt-0.5 text-[11px] text-text-muted">Continuous + one-hot</div>
          </div>
          <div className="rounded-2xl border border-bg-border bg-bg-primary/16 p-3">
            <div className="text-[10.5px] uppercase tracking-[0.16em] text-text-muted">Pending positions</div>
            <div className="mt-1 font-mono text-xl font-semibold text-text-primary">
              {(data.pending_positions || []).length}
            </div>
            <div className="mt-0.5 text-[11px] text-text-muted">Awaiting close → reward update</div>
          </div>
          <div className="rounded-2xl border border-bg-border bg-bg-primary/16 p-3">
            <div className="text-[10.5px] uppercase tracking-[0.16em] text-text-muted">Status</div>
            <div className="mt-1 inline-flex items-center gap-1.5 rounded-full border border-emerald-500/40 bg-emerald-500/10 px-2 py-0.5 text-xs font-semibold text-emerald-300">
              <GitBranch size={11} /> learning
            </div>
            <div className="mt-1 text-[11px] text-text-muted">Posterior updates on every close</div>
          </div>
        </div>
      </section>

      {/* Size-bucket convergence */}
      <section className="rounded-[26px] border border-bg-border bg-bg-secondary/24 p-5">
        <div className="mb-3 flex items-center gap-2 text-sm font-semibold text-text-primary">
          Size-multiplier posterior
        </div>
        <div className="mb-3 text-[11.5px] text-text-muted">
          Each size multiplier has its own Normal posterior on Mean R.
          Higher Mean R = the policy will Thompson-sample that bucket
          more often. With n=0 the bucket samples from a weak prior
          (1.0× boots with a small positive bias).
        </div>

        <div className="overflow-x-auto">
          <table className="w-full text-[12px]">
            <thead className="text-[10.5px] uppercase tracking-wide text-text-muted">
              <tr className="border-b border-bg-border/60">
                <th className="px-2 py-2 text-left">Multiplier</th>
                <th className="px-2 py-2 text-right">Trades closed</th>
                <th className="px-2 py-2 text-right">Mean R</th>
                <th className="px-2 py-2 text-left">Share of trades</th>
              </tr>
            </thead>
            <tbody>
              {orderedKeys.length === 0 ? (
                <tr>
                  <td colSpan={4} className="py-4 text-center text-text-muted">
                    No size buckets registered yet.
                  </td>
                </tr>
              ) : (
                orderedKeys.map((key) => {
                  const b = buckets[key] || {};
                  const share = totalTrades > 0 ? (b.n || 0) / totalTrades : 0;
                  return (
                    <tr key={key} className="border-b border-bg-border/30">
                      <td className="px-2 py-2 font-mono text-text-primary">{parseFloat(key).toFixed(1)}×</td>
                      <td className="px-2 py-2 text-right font-mono">{b.n ?? 0}</td>
                      <td className={clsx("px-2 py-2 text-right font-mono font-semibold", bucketTone(b.mean_R))}>
                        {fmt(b.mean_R, 3, true)}
                      </td>
                      <td className="px-2 py-2">
                        <div className="h-1.5 w-full overflow-hidden rounded bg-bg-border/30">
                          <div
                            className="h-full bg-accent-blue/60"
                            style={{ width: `${Math.min(share * 100, 100).toFixed(1)}%` }}
                          />
                        </div>
                        <div className="mt-0.5 text-[10.5px] text-text-muted">
                          {(share * 100).toFixed(1)}%
                        </div>
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      </section>

      {/* Pending positions list */}
      {(data.pending_positions || []).length > 0 ? (
        <section className="rounded-[26px] border border-bg-border bg-bg-secondary/24 p-5">
          <div className="mb-2 text-sm font-semibold text-text-primary">Pending reward attribution</div>
          <div className="text-[11.5px] text-text-muted mb-2">
            Each open position has a feature-vector + size-multiplier stashed in
            the policy's pending dict. On close, realised PnL becomes an
            R-multiple that updates both the value posterior and the size bucket.
          </div>
          <div className="flex flex-wrap gap-1.5">
            {(data.pending_positions || []).map((id) => (
              <span
                key={id}
                className="rounded border border-bg-border bg-bg-primary/15 px-2 py-1 font-mono text-[10.5px] text-text-secondary"
              >
                {id.slice(0, 12)}…
              </span>
            ))}
          </div>
        </section>
      ) : null}

      {/* Strategy params (reused) */}
      <StrategyParamsPanel params={data.strategy_params} />
    </div>
  );
}

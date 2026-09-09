"use client";

/**
 * Exit integrity — does each exit reason agree with the money it booked?
 *
 * A desk that shows only P&L cannot tell a working stop from a broken one, and
 * this lane had three different exit failures at once, none of them visible:
 *
 *   - 9 of 28 `target_hit` exits LOST money. The ladder tests a SPOT level but
 *     P&L is realised in PREMIUM, and the median target fired on a spot move of
 *     +0.01% — a level brushed by noise while theta ate the premium.
 *   - 3 stop exits MADE money, one paying Rs 2,29,110, because the stop sat on
 *     the WRONG SIDE of entry and fired the moment the position opened.
 *   - The premium hard stop is configured at -25% and realised at -63%: the
 *     mark cadence is slower than a short-dated option moves, so the limit is
 *     unenforceable rather than wrong.
 *
 * Each needs a different fix, so each is stated separately rather than rolled
 * into a win rate. The panel leads with payoff geometry because that is the
 * finding that survives every signal change: at an average win of 26,252
 * against an average loss of 58,356 the lane needs 69.0% of trades to win just
 * to break even, and hits 38.6%.
 */
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, ShieldAlert, Scale } from "lucide-react";

import {
  MetricTile,
  Section,
  REFRESH_MS,
  formatSignedMoney,
  formatMoney,
  formatPct,
} from "@/components/desk-ui";
import { getAuctionExitIntegrity } from "@/lib/api";

const pct1 = (v?: number | null) => (v == null ? "—" : `${v.toFixed(1)}%`);

export function ExitIntegrity() {
  const q = useQuery({
    queryKey: ["auction", "exit-integrity"],
    queryFn: (): Promise<any> => getAuctionExitIntegrity().then((r) => r.data),
    refetchInterval: REFRESH_MS.summary,
  });
  const d = q.data;

  if (!d) {
    return (
      <Section title="Exit integrity">
        <div className="text-[12px] text-text-muted">
          {q.isLoading ? "Loading…" : "No closed positions to audit."}
        </div>
      </Section>
    );
  }

  const p = d.payoff ?? {};
  const survivable = (p.edge_vs_breakeven ?? 0) >= 0;

  return (
    <div className="space-y-4">
      <Section
        title="Payoff geometry"
        icon={<Scale size={15} />}
        description="The finding that survives every signal change: what the lane must win to break even, against what it does win."
      >
        <div className="grid grid-cols-2 gap-3 md:grid-cols-6">
          <MetricTile label="Avg win" value={formatMoney(p.avg_win)} size="sm" color="text-emerald-400" />
          <MetricTile label="Avg loss" value={formatMoney(p.avg_loss)} size="sm" color="text-rose-400" />
          <MetricTile
            label="Reward : risk"
            value={p.reward_to_risk == null ? "—" : p.reward_to_risk.toFixed(2)}
            size="sm"
            detail={p.reward_to_risk != null && p.reward_to_risk < 1 ? "risking more than it makes" : undefined}
          />
          <MetricTile label="Win rate" value={formatPct(p.actual_win_rate)} size="sm" />
          <MetricTile label="Breakeven needs" value={formatPct(p.breakeven_win_rate)} size="sm" />
          <MetricTile
            label="Edge"
            value={p.edge_vs_breakeven == null ? "—" : formatPct(p.edge_vs_breakeven)}
            size="sm"
            color={survivable ? "text-emerald-400" : "text-rose-400"}
          />
        </div>
        {!survivable ? (
          <div className="mt-3 rounded-xl border border-rose-500/30 bg-rose-500/5 px-4 py-3 text-[12px] text-rose-200/90">
            The geometry is not survivable at the observed hit rate: the lane needs{" "}
            <span className="font-mono">{formatPct(p.breakeven_win_rate)}</span> of trades to win merely
            to break even and achieves <span className="font-mono">{formatPct(p.actual_win_rate)}</span>.
            No improvement in signal quality fixes a reward:risk of{" "}
            <span className="font-mono">{p.reward_to_risk?.toFixed(2)}</span> — the exit distances have to
            change.
          </div>
        ) : null}
      </Section>

      {d.configured_geometry ? (
        <Section
          title="Shipped geometry"
          description="What new positions get, beside what the book above already did — otherwise a shipped fix looks like it never happened."
        >
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <MetricTile
              label="Hard stop"
              value={pct1(-d.configured_geometry.hard_stop_fraction_pct)}
              size="sm"
            />
            <MetricTile
              label="Target floor"
              value={pct1(d.configured_geometry.target_floor_pct)}
              size="sm"
              detail={`${d.configured_geometry.target_min_reward_multiple}x the risk`}
            />
            <MetricTile
              label="Implied breakeven"
              value={formatPct(d.configured_geometry.implied_breakeven_win_rate)}
              size="sm"
            />
            <MetricTile
              label="vs actual win rate"
              value={formatPct(p.actual_win_rate)}
              size="sm"
              color={
                d.configured_geometry.implied_breakeven_win_rate != null &&
                p.actual_win_rate != null &&
                p.actual_win_rate > d.configured_geometry.implied_breakeven_win_rate
                  ? "text-emerald-400"
                  : "text-rose-400"
              }
            />
          </div>
          <div className="mt-3 text-[11.5px] leading-relaxed text-text-muted">
            {d.configured_geometry.evidence}
          </div>
        </Section>
      ) : null}

      <Section
        title="Does the exit reason agree with the money?"
        icon={<AlertTriangle size={15} />}
        description="A target that loses and a stop that pays are different bugs. Rolled into a win rate, both disappear."
      >
        <div className="grid gap-3 md:grid-cols-2">
          <div className="rounded-2xl border border-bg-border bg-bg-secondary/24 p-4">
            <div className="flex items-baseline justify-between">
              <span className="text-[13px] text-text-primary">
                <span className="font-mono text-amber-400">target_hit</span> that lost money
              </span>
              <span className="font-mono text-lg text-amber-400">
                {d.contradictions?.target_that_lost_count ?? 0}
              </span>
            </div>
            <div className="mt-1 text-[11.5px] text-text-muted">
              A spot level reached while the premium fell. Guarded at entry since 2026-09-09: a target
              must also clear its own round-trip cost before it is booked as one.
            </div>
          </div>
          <div className="rounded-2xl border border-bg-border bg-bg-secondary/24 p-4">
            <div className="flex items-baseline justify-between">
              <span className="text-[13px] text-text-primary">
                <span className="font-mono text-rose-400">stop</span> exits that made money
              </span>
              <span className="font-mono text-lg text-rose-400">
                {d.contradictions?.stop_that_paid_count ?? 0}
              </span>
            </div>
            <div className="mt-1 text-[11.5px] text-text-muted">
              A stop that pays was on the wrong side of entry and fired at open.
            </div>
            <div className="mt-2 space-y-1">
              {(d.contradictions?.stop_that_paid ?? []).map((x: any) => (
                <div key={x.label} className="flex justify-between text-[11.5px]">
                  <span className="truncate text-text-muted">{x.label}</span>
                  <span className="ml-2 font-mono text-emerald-400">{formatSignedMoney(x.pnl)}</span>
                </div>
              ))}
            </div>
          </div>
        </div>
      </Section>

      <Section
        title="Stops"
        icon={<ShieldAlert size={15} />}
        description="Whether the stop is well-formed, and whether it can enforce the limit it declares."
      >
        <div className="grid gap-3 md:grid-cols-2">
          <div className="rounded-2xl border border-bg-border bg-bg-secondary/24 p-4">
            <div className="flex items-baseline justify-between">
              <span className="text-[13px] text-text-primary">Wrong-side stops in the book</span>
              <span
                className={`font-mono text-lg ${
                  (d.wrong_side_stops?.count ?? 0) > 0 ? "text-rose-400" : "text-emerald-400"
                }`}
              >
                {d.wrong_side_stops?.count ?? 0}
              </span>
            </div>
            <div className="mt-1 text-[11.5px] text-text-muted">
              Historical rows. New positions drop such a level at entry and record the reason, so the
              malformed upstream decision stays visible.
            </div>
            <div className="mt-2 space-y-1">
              {(d.wrong_side_stops?.positions ?? []).map((x: any) => (
                <div key={x.label} className="flex justify-between text-[11.5px]">
                  <span className="truncate text-text-muted">{x.label}</span>
                  <span className="ml-2 font-mono">
                    entry {x.entry?.toFixed(0)} · stop {x.stop?.toFixed(0)}
                  </span>
                </div>
              ))}
            </div>
          </div>
          <div className="rounded-2xl border border-bg-border bg-bg-secondary/24 p-4">
            <div className="text-[13px] text-text-primary">Hard stop: declared vs realised</div>
            <div className="mt-2 space-y-1 text-[12px]">
              <div className="flex justify-between">
                <span className="text-text-muted">Configured</span>
                <span className="font-mono">{pct1(d.hard_stop?.configured_fraction_pct)}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-text-muted">Median realised</span>
                <span className="font-mono text-rose-400">{pct1(d.hard_stop?.median_realised_pct)}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-text-muted">Median slippage</span>
                <span className="font-mono">
                  {d.hard_stop?.slippage_samples ? pct1(d.hard_stop?.median_slippage_pct) : "— (no marked exits yet)"}
                </span>
              </div>
            </div>
            <div className="mt-2 text-[11.5px] text-text-muted">
              The gap is a mark-cadence problem, not a threshold problem: by the time a pass observes the
              declared drawdown the premium has already fallen much further. Slippage is recorded on every
              new hard-stop close so the gap is measured rather than inferred.
            </div>
          </div>
        </div>
      </Section>

      <Section title="By exit reason" description="Median premium change alongside the money, so a reason that never pays is obvious.">
        <div className="overflow-x-auto">
          <table className="w-full min-w-[560px] text-[12px]">
            <thead className="text-[10.5px] uppercase tracking-wider text-text-muted">
              <tr className="border-b border-bg-border">
                <th className="py-1.5 text-left font-medium">Reason</th>
                <th className="py-1.5 text-right font-medium">Trades</th>
                <th className="py-1.5 text-right font-medium">Wins</th>
                <th className="py-1.5 text-right font-medium">Avg P&amp;L</th>
                <th className="py-1.5 text-right font-medium">Total P&amp;L</th>
                <th className="py-1.5 text-right font-medium">Median premium</th>
              </tr>
            </thead>
            <tbody>
              {(d.by_reason ?? []).map((b: any) => (
                <tr key={b.reason} className="border-b border-bg-border/40 last:border-0">
                  <td className="py-1.5 font-mono text-text-primary">{b.reason}</td>
                  <td className="py-1.5 text-right font-mono">{b.n}</td>
                  <td className="py-1.5 text-right font-mono text-text-muted">{b.wins}</td>
                  <td className={`py-1.5 text-right font-mono ${b.avg_pnl >= 0 ? "text-emerald-400" : "text-rose-400"}`}>
                    {formatSignedMoney(b.avg_pnl)}
                  </td>
                  <td className={`py-1.5 text-right font-mono ${b.total_pnl >= 0 ? "text-emerald-400" : "text-rose-400"}`}>
                    {formatSignedMoney(b.total_pnl)}
                  </td>
                  <td className="py-1.5 text-right font-mono text-text-muted">
                    {pct1(b.median_premium_change_pct)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Section>
    </div>
  );
}

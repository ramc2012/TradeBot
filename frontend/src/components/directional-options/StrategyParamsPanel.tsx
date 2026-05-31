"use client";

/**
 * Active strategy parameters for the directional-options engine.
 *
 * Pulled from `/api/directional-options/policy.strategy_params` (the
 * backend serialises the relevant slices of config.risk and
 * config.paper_trading there).
 *
 * NOTE: This panel is intentionally read-only. The point is to make
 * what the engine is actually using visible to the user — most of
 * these knobs (universe, premium_cap=None, one_position_per_symbol)
 * are key engine-design choices, not interactive controls.
 */
import { clsx } from "clsx";
import { ShieldCheck } from "lucide-react";

export type StrategyParams = {
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

function fmtPct(v: number | null | undefined, digits = 2): string {
  if (v == null) return "—";
  return `${(v * 100).toFixed(digits)}%`;
}

function fmtMoney(v: number | null | undefined): string {
  if (v == null) return "—";
  return `₹${v.toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;
}

function Row({ label, value, sub, accent }: { label: string; value: string; sub?: string; accent?: "ok" | "warn" }) {
  return (
    <div className="flex items-baseline justify-between gap-3 border-b border-bg-border/40 py-2 last:border-b-0">
      <div>
        <div className="text-[11.5px] text-text-secondary">{label}</div>
        {sub ? <div className="text-[10.5px] text-text-muted">{sub}</div> : null}
      </div>
      <div className={clsx(
        "font-mono text-sm",
        accent === "ok" && "text-emerald-300",
        accent === "warn" && "text-amber-300",
        !accent && "text-text-primary",
      )}>
        {value}
      </div>
    </div>
  );
}

export default function StrategyParamsPanel({ params }: { params: StrategyParams | null | undefined }) {
  const p = params || {};
  const baseRiskBudget = (p.risk_pct ?? 0) * (p.starting_equity ?? 0);
  return (
    <div className="rounded-[26px] border border-bg-border bg-bg-secondary/24 p-5">
      <div className="mb-3 flex items-center gap-2 text-sm font-semibold text-text-primary">
        <ShieldCheck size={16} />
        Strategy Parameters
      </div>

      <div className="grid gap-5 md:grid-cols-2">
        <div>
          <div className="mb-1 text-[10.5px] uppercase tracking-[0.16em] text-text-muted">Universe & sizing</div>
          <Row
            label="Universe"
            value={(p.universe || []).join(" · ") || "—"}
            sub="NSE index options only"
          />
          <Row
            label="Starting equity"
            value={fmtMoney(p.starting_equity)}
            sub="Paper book funded baseline"
          />
          <Row
            label="Base risk per trade"
            value={fmtPct(p.risk_pct, 3)}
            sub={`≈ ${fmtMoney(baseRiskBudget)} at 1.0× multiplier`}
          />
          <Row
            label="Premium cap"
            value={p.premium_cap_pct == null ? "None (RL-managed)" : fmtPct(p.premium_cap_pct, 2)}
            sub="No fixed cap — size scales with policy multiplier"
            accent={p.premium_cap_pct == null ? "ok" : undefined}
          />
          <Row
            label="One position per symbol"
            value={p.one_position_per_symbol ? "ON" : "OFF"}
            sub="Strict guard at entry"
            accent={p.one_position_per_symbol ? "ok" : "warn"}
          />
        </div>

        <div>
          <div className="mb-1 text-[10.5px] uppercase tracking-[0.16em] text-text-muted">Exit & capital safety</div>
          <Row
            label="Planned stop"
            value={fmtPct(p.planned_stop_pct, 1)}
            sub="Premium % below entry"
          />
          <Row
            label="Profit target"
            value={fmtPct(p.profit_target_pct, 1)}
            sub="Premium % above entry"
          />
          <Row
            label="Trail giveback"
            value={fmtPct(p.trail_giveback_pct, 1)}
            sub="% of peak ceded before trail exit"
          />
          <Row
            label="Min hold (anti-churn)"
            value={p.min_hold_bars != null ? `${p.min_hold_bars} bars` : "—"}
            sub="Signal-flip suppressed below this"
          />
          <Row
            label="Daily / Weekly loss cap"
            value={`${p.daily_loss_cap_r ?? "—"}R / ${p.weekly_loss_cap_r ?? "—"}R`}
            sub="R-multiples of base risk; stops trading once hit"
            accent="warn"
          />
        </div>
      </div>

      <div className="mt-4 rounded-xl border border-emerald-500/20 bg-emerald-500/5 p-3 text-[11.5px] text-emerald-200/90">
        <strong>RL-managed:</strong> trade/skip threshold, size multiplier
        ({"{"}0.5×, 1.0×, 1.5×, 2.0×{"}"}), and strike choice are all
        learned online by the contextual bandit. No hand-tuned
        min_confidence, regime block, or delta-bucket block.
      </div>
    </div>
  );
}

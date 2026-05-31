"use client";

/**
 * Always-on engine calculations panel.
 *
 * Shows what the engine is computing under the hood for the currently
 * selected symbol — features, regime classification + reasons, signal
 * sub-fields (jump_score, timing_precision, tail probability, model
 * uncertainty…), and every candidate the selector surfaced with its
 * score, delta bucket, and edge metrics.
 *
 * Critically: this panel renders even when there is no signal and no
 * approved trade. The previous workspace collapsed to "No contract
 * cleared the hurdle" — that hid the fact that the engine IS computing
 * something, just not finding a tradeable setup right now. This panel
 * makes that activity visible.
 */
import { clsx } from "clsx";
import { Calculator } from "lucide-react";

type Snapshot = {
  underlying?: string;
  spot_price?: number | null;
  feature_snapshot?: Record<string, number | string | null> | null;
  regime?: {
    label?: string;
    confidence?: number;
    trade_allowed?: boolean;
    reasons?: string[];
    preferred_expiry_kind?: string;
    delta_target_min?: number;
    delta_target_max?: number;
  } | null;
  signal?: {
    direction?: string;
    confidence?: number;
    expected_move?: number;
    expected_move_pct?: number;
    expected_horizon_bars?: number;
    expected_horizon_hours?: number;
    direction_score?: number;
    expected_iv_change?: number;
    sleeve?: string;
    thesis?: string;
    p_up?: number;
    p_move_gt_1sigma?: number;
    p_move_gt_2sigma?: number;
    jump_score?: number;
    timing_precision?: number;
    tail_probability?: number;
    model_uncertainty?: number;
  } | null;
  contract_candidates?: Array<{
    trading_symbol: string;
    option_type: string;
    strike: number;
    expiry?: string;
    expiry_kind?: string;
    days_to_expiry?: number;
    option_price?: number;
    implied_vol?: number;
    delta?: number;
    delta_bucket?: string;
    contract_score?: number;
    expected_pnl?: number;
    p_trading_edge?: number;
    p_minus_q_tail?: number;
    probability_of_profit?: number;
    liquidity_score?: number;
    skew_tax?: number;
    timing_fit?: number;
    selected?: boolean;
    rejection_reasons?: string[];
  }>;
  selected_contract?: { trading_symbol?: string } | null;
  selection_reason?: string;
  as_of?: string | null;
  data_status?: { execution_ready?: boolean; degraded_reason?: string | null };
};

function fmt(v: number | string | null | undefined, digits = 3): string {
  if (v == null || (typeof v === "number" && Number.isNaN(v))) return "—";
  if (typeof v === "string") return v;
  return v.toFixed(digits);
}

function fmtPct(v: number | null | undefined, digits = 2): string {
  if (v == null || Number.isNaN(v)) return "—";
  return `${(v * 100).toFixed(digits)}%`;
}

function Row({ label, value, tone, sub }: { label: string; value: string; tone?: string; sub?: string }) {
  return (
    <div className="flex items-baseline justify-between gap-2 border-b border-bg-border/30 py-1.5 last:border-b-0">
      <div className="text-[11.5px] text-text-secondary">{label}</div>
      <div className="text-right">
        <div className={clsx("font-mono text-[12.5px]", tone || "text-text-primary")}>{value}</div>
        {sub ? <div className="text-[10.5px] text-text-muted">{sub}</div> : null}
      </div>
    </div>
  );
}

function tone(v: number | null | undefined): string {
  if (v == null) return "text-text-muted";
  if (v > 0) return "text-emerald-300";
  if (v < 0) return "text-rose-300";
  return "text-text-secondary";
}

export default function EngineCalculations({ snapshot }: { snapshot: Snapshot | null | undefined }) {
  const s = snapshot || {};
  const feat = s.feature_snapshot || {};
  const reg = s.regime || {};
  const sig = s.signal || {};
  const candidates = s.contract_candidates || [];

  const noSignal = !s.signal;
  const noFeatures = Object.keys(feat).length === 0;

  return (
    <section className="rounded-[26px] border border-bg-border bg-bg-secondary/24 p-5">
      <div className="mb-3 flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 text-sm font-semibold text-text-primary">
          <Calculator size={16} />
          Engine calculations · {s.underlying || "—"}
        </div>
        <div className="text-[11px] text-text-muted">
          {s.as_of ? `bar ${new Date(s.as_of).toLocaleString("en-IN", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", hour12: false })}` : "no bar yet"}
        </div>
      </div>

      {noFeatures ? (
        <div className="rounded-xl border border-amber-500/30 bg-amber-500/8 p-3 text-[12px] text-amber-200">
          No spot history loaded for this symbol yet — the feature engine has
          nothing to compute on. {s.data_status?.degraded_reason ? `(${s.data_status.degraded_reason.replaceAll("_", " ")})` : ""}
        </div>
      ) : null}

      <div className="grid gap-5 md:grid-cols-3">
        {/* Features */}
        <div>
          <div className="mb-1.5 text-[10.5px] uppercase tracking-[0.16em] text-text-muted">
            Feature snapshot
          </div>
          <Row label="Spot" value={fmt(s.spot_price, 2)} />
          <Row label="ADX" value={fmt(feat.adx as number, 1)} />
          <Row label="+DI / -DI" value={`${fmt(feat.plus_di as number, 1)} / ${fmt(feat.minus_di as number, 1)}`} />
          <Row label="EMA spread" value={fmtPct(feat.ema_spread_pct as number, 3)} tone={tone(feat.ema_spread_pct as number)} />
          <Row label="ATR" value={fmt(feat.atr as number, 1)} />
          <Row label="Momentum 3 / 8" value={`${fmtPct(feat.momentum_3 as number, 2)} / ${fmtPct(feat.momentum_8 as number, 2)}`} />
          <Row label="Range expansion" value={fmt(feat.range_expansion as number, 2)} />
          <Row label="Breakout ↑ / ↓" value={`${fmt(feat.breakout_up as number, 2)} / ${fmt(feat.breakout_down as number, 2)}`} />
          <Row label="RV annualised" value={fmtPct(feat.rv_annualized as number, 1)} />
          <Row label="RV percentile" value={fmtPct(feat.rv_percentile as number, 1)} />
        </div>

        {/* Regime + signal */}
        <div>
          <div className="mb-1.5 text-[10.5px] uppercase tracking-[0.16em] text-text-muted">
            Regime classification
          </div>
          <Row label="Label" value={reg.label || "—"} />
          <Row label="Confidence" value={fmt(reg.confidence, 3)} />
          <Row label="Preferred expiry" value={reg.preferred_expiry_kind || "—"} />
          <Row
            label="Delta target band"
            value={
              reg.delta_target_min != null
                ? `${reg.delta_target_min.toFixed(2)} – ${reg.delta_target_max?.toFixed(2) ?? "—"}`
                : "—"
            }
          />
          {reg.reasons && reg.reasons.length > 0 ? (
            <div className="mt-2 rounded-lg border border-bg-border/40 bg-bg-primary/10 p-2 text-[11px] text-text-secondary">
              <div className="mb-1 text-[10.5px] uppercase tracking-[0.14em] text-text-muted">
                Classifier reasons
              </div>
              <ul className="list-disc space-y-0.5 pl-4">
                {reg.reasons.map((r, i) => (
                  <li key={i}>{r}</li>
                ))}
              </ul>
            </div>
          ) : null}

          <div className="mt-4 mb-1.5 text-[10.5px] uppercase tracking-[0.16em] text-text-muted">
            Directional signal
          </div>
          {noSignal ? (
            <div className="rounded-lg border border-bg-border/40 bg-bg-primary/10 p-2 text-[11.5px] text-text-muted">
              No signal — direction score did not clear the dead-tape floor.
              The policy is not called on this bar.
            </div>
          ) : (
            <>
              <Row label="Direction" value={sig.direction || "—"} tone={sig.direction === "CE" ? "text-emerald-300" : sig.direction === "PE" ? "text-rose-300" : undefined} />
              <Row label="Confidence" value={fmt(sig.confidence, 3)} />
              <Row label="Direction score" value={fmt(sig.direction_score, 4)} />
              <Row label="Sleeve" value={sig.sleeve || "—"} />
              <Row label="Expected move" value={`${fmt(sig.expected_move, 1)} pts (${fmtPct(sig.expected_move_pct, 3)})`} />
              <Row label="Horizon" value={`${sig.expected_horizon_bars ?? "—"} bars / ${fmt(sig.expected_horizon_hours, 2)}h`} />
              <Row label="P(up)" value={fmtPct(sig.p_up, 1)} />
              <Row label="P(>1σ) / P(>2σ)" value={`${fmtPct(sig.p_move_gt_1sigma, 1)} / ${fmtPct(sig.p_move_gt_2sigma, 1)}`} />
              <Row label="Jump score" value={fmt(sig.jump_score, 3)} />
              <Row label="Timing precision" value={fmt(sig.timing_precision, 3)} />
              <Row label="Tail probability" value={fmt(sig.tail_probability, 3)} />
              <Row label="Model uncertainty" value={fmt(sig.model_uncertainty, 3)} />
              <Row label="Expected IV change" value={fmtPct(sig.expected_iv_change, 2)} />
            </>
          )}
        </div>

        {/* Candidates */}
        <div>
          <div className="mb-1.5 flex items-center justify-between text-[10.5px] uppercase tracking-[0.16em] text-text-muted">
            <span>Contract candidates ({candidates.length})</span>
            {candidates.length > 0 ? (
              <span className="normal-case tracking-normal text-[10.5px] text-text-muted">
                sorted by selector score
              </span>
            ) : null}
          </div>
          {candidates.length === 0 ? (
            <div className="rounded-lg border border-bg-border/40 bg-bg-primary/10 p-3 text-[11.5px] text-text-muted">
              No candidates surfaced — either no signal, or the option
              snapshot for this expiry / underlying isn't loaded.
            </div>
          ) : (
            <div className="space-y-1.5 max-h-[480px] overflow-y-auto pr-1">
              {candidates.map((c, i) => (
                <div
                  key={`${c.trading_symbol}-${i}`}
                  className={clsx(
                    "rounded-lg border p-2 text-[11px]",
                    c.selected
                      ? "border-accent-blue/50 bg-accent-blue/10"
                      : "border-bg-border/40 bg-bg-primary/10",
                  )}
                >
                  <div className="flex items-baseline justify-between gap-2">
                    <div className="font-mono font-semibold text-text-primary">
                      {c.option_type} {c.strike}
                      <span className="ml-1.5 text-[10px] text-text-muted">
                        {c.delta_bucket} · Δ{c.delta?.toFixed(2) ?? "—"}
                      </span>
                    </div>
                    <div className={clsx("font-mono text-[11.5px] font-semibold", tone(c.contract_score))}>
                      {fmt(c.contract_score, 1)}
                    </div>
                  </div>
                  <div className="mt-1 grid grid-cols-3 gap-x-2 gap-y-0.5 text-[10.5px] text-text-secondary">
                    <span>LTP {fmt(c.option_price, 2)}</span>
                    <span>IV {fmtPct(c.implied_vol, 1)}</span>
                    <span>{c.expiry_kind ?? "—"} {c.days_to_expiry?.toFixed(1) ?? ""}d</span>
                    <span>edge {fmt(c.p_trading_edge, 1)}</span>
                    <span>p-q {fmt(c.p_minus_q_tail, 3)}</span>
                    <span>PoP {fmtPct(c.probability_of_profit, 0)}</span>
                    <span>liq {fmt(c.liquidity_score, 2)}</span>
                    <span>skew {fmt(c.skew_tax, 3)}</span>
                    <span>fit {fmt(c.timing_fit, 2)}</span>
                  </div>
                  {c.rejection_reasons && c.rejection_reasons.length > 0 ? (
                    <div className="mt-1 text-[10px] text-amber-200/80">
                      ⚠ {c.rejection_reasons.join(" · ")}
                    </div>
                  ) : null}
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* Selection reason — show even when nothing approved */}
      <div className="mt-4 rounded-xl border border-bg-border bg-bg-secondary/30 p-3 text-[11.5px] text-text-secondary">
        <span className="text-text-muted">selection reason: </span>
        {s.selection_reason || "—"}
      </div>
    </section>
  );
}

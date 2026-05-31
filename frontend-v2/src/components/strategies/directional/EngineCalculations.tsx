"use client";

/**
 * Engine calculations breakdown — feature snapshot, regime reasons,
 * signal sub-fields, every candidate. Slimmer v2 rebuild using
 * desk-ui primitives.
 */
import { clsx } from "clsx";
import { Calculator } from "lucide-react";

import { Section, formatIST, formatNumber, formatPct, tone } from "@/components/desk-ui";

type Snapshot = {
  underlying?: string;
  spot_price?: number | null;
  feature_snapshot?: Record<string, number> | null;
  regime?: {
    label?: string;
    confidence?: number;
    preferred_expiry_kind?: string;
    delta_target_min?: number;
    delta_target_max?: number;
    reasons?: string[];
  } | null;
  signal?: Record<string, number | string> | null;
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
    p_trading_edge?: number;
    p_minus_q_tail?: number;
    probability_of_profit?: number;
    liquidity_score?: number;
    skew_tax?: number;
    timing_fit?: number;
    selected?: boolean;
    rejection_reasons?: string[];
  }>;
  selection_reason?: string;
  as_of?: string | null;
};

function Row({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div className="flex items-baseline justify-between gap-2 border-b border-bg-border/30 py-1 last:border-b-0">
      <div className="text-[11.5px] text-text-secondary">{label}</div>
      <div className={clsx("font-mono text-[12px]", color || "text-text-primary")}>{value}</div>
    </div>
  );
}

export default function EngineCalculations({ snapshot }: { snapshot: Snapshot | null }) {
  const s = snapshot || {};
  const feat = s.feature_snapshot || {};
  const reg = s.regime || {};
  // `signal` mixes a string `direction` field with otherwise-numeric
  // metrics; accept `unknown` per field and coerce at use site.
  const sig = (s.signal || {}) as Record<string, number | string>;
  const candidates = s.contract_candidates || [];
  const noFeatures = Object.keys(feat).length === 0;

  return (
    <Section
      title={`Engine calculations · ${s.underlying || "—"}`}
      icon={<Calculator size={16} />}
      rightSlot={<span className="text-[11px] text-text-muted">{s.as_of ? `bar ${formatIST(s.as_of)}` : "no bar yet"}</span>}
    >
      {noFeatures ? (
        <div className="rounded-lg border border-accent-amber/30 bg-accent-amber/8 p-3 text-[12px] text-accent-amber">
          No spot history loaded for this symbol — the feature engine has nothing to compute on.
        </div>
      ) : null}

      <div className="grid gap-5 md:grid-cols-3">
        <div>
          <div className="mb-1 text-[10.5px] uppercase tracking-[0.16em] text-text-muted">Feature snapshot</div>
          <Row label="Spot" value={formatNumber(s.spot_price, 2)} />
          <Row label="ADX" value={formatNumber(feat.adx, 1)} />
          <Row label="+DI / -DI" value={`${formatNumber(feat.plus_di, 1)} / ${formatNumber(feat.minus_di, 1)}`} />
          <Row label="EMA spread" value={formatPct(feat.ema_spread_pct, 3)} color={tone(feat.ema_spread_pct)} />
          <Row label="ATR" value={formatNumber(feat.atr, 1)} />
          <Row label="Mom 3 / 8" value={`${formatPct(feat.momentum_3, 2)} / ${formatPct(feat.momentum_8, 2)}`} />
          <Row label="Range expansion" value={formatNumber(feat.range_expansion, 2)} />
          <Row label="Breakout ↑ / ↓" value={`${formatNumber(feat.breakout_up, 2)} / ${formatNumber(feat.breakout_down, 2)}`} />
          <Row label="RV ann / pct" value={`${formatPct(feat.rv_annualized, 1)} / ${formatPct(feat.rv_percentile, 1)}`} />
        </div>

        <div>
          <div className="mb-1 text-[10.5px] uppercase tracking-[0.16em] text-text-muted">Regime + signal</div>
          <Row label="Label" value={reg.label || "—"} />
          <Row label="Confidence" value={formatNumber(reg.confidence, 3)} />
          <Row label="Preferred expiry" value={reg.preferred_expiry_kind || "—"} />
          <Row label="Delta band" value={reg.delta_target_min != null ? `${reg.delta_target_min.toFixed(2)} – ${reg.delta_target_max?.toFixed(2) ?? "—"}` : "—"} />
          {reg.reasons && reg.reasons.length > 0 ? (
            <div className="mt-2 rounded-md border border-bg-border/40 bg-bg-primary/10 p-2 text-[11px] text-text-secondary">
              <div className="mb-1 text-[10px] uppercase tracking-[0.14em] text-text-muted">Classifier reasons</div>
              <ul className="list-disc space-y-0.5 pl-4">{reg.reasons.map((r, i) => <li key={i}>{r}</li>)}</ul>
            </div>
          ) : null}
          {sig.direction ? (
            <>
              <div className="mt-3 mb-1 text-[10.5px] uppercase tracking-[0.16em] text-text-muted">Signal</div>
              <Row label="Direction" value={String(sig.direction)} color={sig.direction === "CE" ? "text-accent-green" : "text-accent-red"} />
              <Row label="Confidence" value={formatNumber(sig.confidence as number, 3)} />
              <Row label="Direction score" value={formatNumber(sig.direction_score as number, 4)} />
              <Row label="Expected move" value={`${formatNumber(sig.expected_move as number, 1)} pts (${formatPct(sig.expected_move_pct as number, 3)})`} />
              <Row label="Horizon" value={`${sig.expected_horizon_bars ?? "—"} bars`} />
              <Row label="P(up)" value={formatPct(sig.p_up as number, 1)} />
              <Row label="Jump score" value={formatNumber(sig.jump_score as number, 3)} />
              <Row label="Timing precision" value={formatNumber(sig.timing_precision as number, 3)} />
              <Row label="Model uncertainty" value={formatNumber(sig.model_uncertainty as number, 3)} />
            </>
          ) : (
            <div className="mt-3 rounded-md border border-bg-border/40 bg-bg-primary/10 p-2 text-[11.5px] text-text-muted">
              No signal — direction score did not clear the dead-tape floor.
            </div>
          )}
        </div>

        <div>
          <div className="mb-1 text-[10.5px] uppercase tracking-[0.16em] text-text-muted">
            Candidates ({candidates.length})
          </div>
          {candidates.length === 0 ? (
            <div className="rounded-lg border border-bg-border/40 bg-bg-primary/10 p-3 text-[11.5px] text-text-muted">
              No candidates surfaced.
            </div>
          ) : (
            <div className="max-h-[440px] space-y-1.5 overflow-y-auto pr-1">
              {candidates.map((c, i) => (
                <div
                  key={`${c.trading_symbol}-${i}`}
                  className={clsx(
                    "rounded-md border p-2 text-[11px]",
                    c.selected ? "border-accent-blue/45 bg-accent-blue/10" : "border-bg-border/40 bg-bg-primary/10",
                  )}
                >
                  <div className="flex items-baseline justify-between gap-2">
                    <div className="font-mono font-semibold text-text-primary">
                      {c.option_type} {c.strike}
                      <span className="ml-1.5 text-[10px] text-text-muted">{c.delta_bucket} · Δ{c.delta?.toFixed(2) ?? "—"}</span>
                    </div>
                    <div className={clsx("font-mono text-[11.5px] font-semibold", tone(c.contract_score))}>
                      {formatNumber(c.contract_score, 1)}
                    </div>
                  </div>
                  <div className="mt-0.5 grid grid-cols-3 gap-x-2 text-[10.5px] text-text-secondary">
                    <span>LTP {formatNumber(c.option_price, 2)}</span>
                    <span>IV {formatPct(c.implied_vol, 1)}</span>
                    <span>{c.expiry_kind ?? ""} {c.days_to_expiry?.toFixed(1) ?? ""}d</span>
                    <span>edge {formatNumber(c.p_trading_edge, 1)}</span>
                    <span>p-q {formatNumber(c.p_minus_q_tail, 3)}</span>
                    <span>PoP {formatPct(c.probability_of_profit, 0)}</span>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      <div className="mt-3 rounded-lg border border-bg-border bg-bg-secondary/25 p-2.5 text-[11.5px] text-text-secondary">
        <span className="text-text-muted">selection reason: </span>
        {s.selection_reason || "—"}
      </div>
    </Section>
  );
}

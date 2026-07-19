"use client";

/**
 * Microstructure panel — richer than the commodity CVD chip.
 *
 * Two stacked surfaces, both off the same live snapshot:
 *   1. Order-flow spar-bars — signed cumulative delta + delta, top / depth
 *      imbalance as −1..+1 diverging bars, vwap & drift, queue pressure, an
 *      aggressive buy-vs-sell split bar, and spread / micro-price. We reuse
 *      the shared OrderFlowPanel for the full execution-quality surface, then
 *      layer the auction-specific extras OrderFlowPanel doesn't break out.
 *   2. Regime strip — label + confidence + allowed-direction chips + each
 *      scorecard factor as a mini-bar.
 *
 * HONESTY (2026-07-19): every buy/sell-ATTRIBUTED number here (cumulative
 * delta, delta, the aggression split) is INFERRED from the quote stream. No
 * wired Indian retail broker pushes public aggressor-tagged trade prints
 * (`backend/analytics/orderflow.py`) and `market_ticks` stores no per-trade
 * size or side, so nothing on this panel may be described as a tape read.
 */
import { Activity, Gauge } from "lucide-react";

import {
  MetricTile,
  Section,
  StatusBadge,
  formatNumber,
  formatSignedNumber,
  formatPct,
  regimeTone,
  tone,
} from "@/components/desk-ui";
import { OrderFlowPanel, type OrderFlow } from "@/components/strategies/shared";

import type { Regime } from "../types";

const GREEN = "rgb(var(--accent-green))";
const RED = "rgb(var(--accent-red))";

/** Diverging −range..+range bar, centred at zero. */
function DivBar({ label, value, range = 1, digits = 2 }: { label: string; value?: number | null; range?: number; digits?: number }) {
  const raw = Number(value ?? 0);
  const v = Math.max(-range, Math.min(range, raw));
  const pct = (Math.abs(v) / range) * 50;
  return (
    <div>
      <div className="flex items-center justify-between text-[10.5px]">
        <span className="text-text-muted">{label}</span>
        <span className={`font-mono ${tone(value == null ? null : raw)}`}>
          {value == null ? "—" : formatSignedNumber(raw, digits)}
        </span>
      </div>
      <div className="relative mt-1 h-2 overflow-hidden rounded-full bg-bg-primary/40">
        <div className="absolute left-1/2 top-0 h-full w-px bg-white/25" />
        <div
          className="absolute top-0 h-full rounded-full transition-all duration-500"
          style={{ background: v >= 0 ? GREEN : RED, left: v >= 0 ? "50%" : `${50 - pct}%`, width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

/** A scorecard factor as a 0..1-ish mini-bar (magnitude-scaled, signed-coloured). */
function ScoreBar({ label, value }: { label: string; value: number }) {
  const v = Number(value ?? 0);
  // Scorecard factors are typically small signed scores; scale magnitude to a
  // 0..1 bar by clamping at 1 and colour by sign.
  const mag = Math.min(1, Math.abs(v));
  const color = v > 0 ? GREEN : v < 0 ? RED : "rgb(var(--accent-blue))";
  return (
    <div className="rounded-lg border border-bg-border bg-bg-primary/15 px-2.5 py-1.5">
      <div className="flex items-center justify-between text-[9.5px]">
        <span className="uppercase tracking-[0.08em] text-text-muted">{label.replace(/_/g, " ")}</span>
        <span className="font-mono text-text-secondary">{formatNumber(v, 3)}</span>
      </div>
      <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-bg-primary/40">
        <div className="h-full rounded-full" style={{ width: `${mag * 100}%`, background: color }} />
      </div>
    </div>
  );
}

function RegimeStrip({ regime }: { regime?: Regime }) {
  const scorecard = regime?.scorecard || {};
  const dirs = (regime?.allowed_directions || []).filter(Boolean);
  const factors = Object.entries(scorecard);
  return (
    <Section
      title="Regime"
      icon={<Gauge size={16} />}
      description="Classification, conviction, permitted directions and the scoring factors behind the call"
      rightSlot={<StatusBadge label={regime?.label?.replace(/_/g, " ") || "—"} tone={regimeTone(regime?.label)} />}
    >
      <div className="space-y-3">
        <div className="flex flex-wrap items-center gap-3">
          <div className="flex items-baseline gap-2">
            <span className="text-2xl font-semibold text-text-primary">{formatPct(regime?.confidence, 0)}</span>
            <span className="text-[11px] text-text-muted">confidence</span>
          </div>
          <span className="text-[11px] text-text-muted">allows</span>
          {dirs.length ? (
            <div className="flex gap-1.5">
              {dirs.map((dir) => {
                const s = dir.toUpperCase();
                const v = s === "LONG" || s === "BUY" ? "success" : s === "SHORT" || s === "SELL" ? "error" : "neutral";
                return <StatusBadge key={dir} label={dir} variant={v} />;
              })}
            </div>
          ) : (
            <span className="text-[11.5px] text-text-muted">no direction permitted</span>
          )}
        </div>

        {(regime?.reasons || []).filter(Boolean).length ? (
          <div className="space-y-1">
            {(regime?.reasons || []).filter(Boolean).map((r, i) => (
              <div key={i} className="flex items-start gap-2 text-[12px] text-text-secondary">
                <span className="mt-1 h-1.5 w-1.5 shrink-0 rounded-full bg-accent-blue/70" />
                {r}
              </div>
            ))}
          </div>
        ) : null}

        {factors.length ? (
          <div className="grid grid-cols-2 gap-2 pt-1 sm:grid-cols-3 xl:grid-cols-4">
            {factors.map(([k, v]) => (
              <ScoreBar key={k} label={k} value={Number(v)} />
            ))}
          </div>
        ) : (
          <div className="text-[11.5px] text-text-muted">No scorecard factors this snapshot.</div>
        )}
      </div>
    </Section>
  );
}

export function MicrostructurePanel({ of, regime }: { of?: OrderFlow | null; regime?: Regime }) {
  const f = of || {};
  const cvd = Number(f.cumulative_delta ?? 0);
  const hasCvd = f.cumulative_delta != null;
  const aggBuy = Number(f.aggressive_buy_volume ?? 0);
  const aggSell = Number(f.aggressive_sell_volume ?? 0);
  const aggTot = aggBuy + aggSell || 1;

  return (
    <div className="space-y-4">
      {/* Auction-specific microstructure header — the fields OrderFlowPanel
          doesn't break out, rendered prominent and signed. */}
      <Section
        title="Microstructure"
        icon={<Activity size={16} />}
        description="Signed flow, book imbalance and aggression behind the regime call — buy/sell sides are inferred from the quote stream, not read off an aggressor tape"
      >
        <div className="grid gap-4 lg:grid-cols-3">
          {/* signed CVD headline + price tiles */}
          <div className="grid grid-cols-2 gap-2.5 self-start">
            <MetricTile
              size="sm"
              label="Cum. delta"
              value={hasCvd ? formatSignedNumber(cvd, 0) : "—"}
              detail={cvd > 0 ? "net buying" : cvd < 0 ? "net selling" : "balanced"}
              color={tone(hasCvd ? cvd : null)}
            />
            <MetricTile size="sm" label="Delta" value={f.delta == null ? "—" : formatSignedNumber(f.delta, 0)} color={tone(f.delta)} />
            <MetricTile
              size="sm"
              label="VWAP"
              value={formatNumber(f.vwap, 1)}
              detail={`drift ${f.vwap_drift == null ? "—" : formatSignedNumber(f.vwap_drift, 2)}`}
              color={tone(f.vwap_drift)}
            />
            <MetricTile size="sm" label="Micro price" value={formatNumber(f.micro_price, 1)} detail={`spread ${formatNumber(f.spread, 2)}`} />
          </div>

          {/* imbalance & queue spar-bars */}
          <div className="space-y-2.5 self-start">
            <DivBar label="Top imbalance" value={f.top_imbalance} />
            <DivBar label="Depth imbalance" value={f.depth_imbalance} />
            <DivBar label="Queue pressure" value={f.queue_pressure} />
            {f.book_pressure != null ? <DivBar label="Book pressure" value={f.book_pressure} /> : null}
          </div>

          {/* aggressive buy vs sell split */}
          <div className="flex flex-col justify-start gap-2.5 self-start">
            <div>
              <div className="flex items-center justify-between text-[10.5px]">
                <span
                  className="text-text-muted"
                  title="Aggressive buy vs sell volume, with both sides INFERRED from the quote stream — market_ticks carries no per-trade size and no broker aggressor flag (backend/analytics/orderflow.py)."
                >
                  Aggression split (inferred)
                </span>
                <span className="font-mono text-text-secondary">
                  {((aggBuy / aggTot) * 100).toFixed(0)}/{((aggSell / aggTot) * 100).toFixed(0)}
                </span>
              </div>
              <div className="mt-1.5 flex h-3 overflow-hidden rounded-full bg-bg-primary/40">
                <div className="transition-all duration-500" style={{ width: `${(aggBuy / aggTot) * 100}%`, background: GREEN }} />
                <div className="transition-all duration-500" style={{ width: `${(aggSell / aggTot) * 100}%`, background: RED }} />
              </div>
              <div className="mt-1 flex justify-between text-[10px]">
                <span className="text-accent-green">buy {aggBuy.toFixed(0)}</span>
                <span className="text-accent-red">sell {aggSell.toFixed(0)}</span>
              </div>
            </div>
            <MetricTile
              size="sm"
              label="Toxicity"
              value={formatPct(f.toxicity_score, 0)}
              detail={`intensity ${formatNumber(f.trade_intensity_per_minute, 0)}/min`}
              color={Number(f.toxicity_score ?? 0) > 0.5 ? "text-accent-red" : "text-text-primary"}
            />
          </div>
        </div>
      </Section>

      {/* Full shared order-flow surface (execution-quality gauges, etc.) */}
      <OrderFlowPanel of={of} />

      {/* Regime strip */}
      <RegimeStrip regime={regime} />
    </div>
  );
}

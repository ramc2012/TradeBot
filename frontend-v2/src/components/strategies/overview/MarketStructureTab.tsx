"use client";

/**
 * Market-structure tab — the shared TPO profile + order-flow panel fed from the
 * Auction Intelligence NIFTY live-snapshot, plus a "regime consensus" strip that
 * counts how many lanes currently read bullish / bearish / neutral.
 *
 * Robust to missing fields: when the snapshot is null the viz components show
 * their own empty states, and the consensus simply counts what it can.
 */
import { useMemo } from "react";
import { clsx } from "clsx";
import { Compass, Scale } from "lucide-react";

import {
  MetricTile,
  Section,
  StatusBadge,
  formatNumber,
  formatPct,
  regimeTone,
  tone,
} from "@/components/desk-ui";
import { MarketProfileChart, OrderFlowPanel } from "@/components/strategies/shared";
import type { Snapshot as AuctionSnapshot } from "@/components/strategies/auction/types";
import { biasBucket, type LaneView } from "./types";

function ConsensusBar({
  bullish,
  bearish,
  neutral,
}: {
  bullish: number;
  bearish: number;
  neutral: number;
}) {
  const total = bullish + bearish + neutral || 1;
  const seg = (n: number, color: string) =>
    n > 0 ? <div style={{ width: `${(n / total) * 100}%`, background: color }} /> : null;
  return (
    <div className="flex h-2.5 overflow-hidden rounded-full">
      {seg(bullish, "rgb(var(--accent-green))")}
      {seg(neutral, "rgb(var(--text-muted))")}
      {seg(bearish, "rgb(var(--accent-red))")}
    </div>
  );
}

export function MarketStructureTab({
  snapshot,
  lanes,
}: {
  snapshot?: AuctionSnapshot;
  lanes: LaneView[];
}) {
  const analysis = snapshot?.analysis;
  const mp = analysis?.market_profile;
  const of = analysis?.order_flow;
  const regime = analysis?.regime;
  const spot = snapshot?.request?.session?.last_price ?? mp?.close_price ?? null;

  // Regime consensus: count lanes by their signal direction (preferred) or
  // regime label, bucketed into bullish / bearish / neutral.
  const consensus = useMemo(() => {
    let bullish = 0;
    let bearish = 0;
    let neutral = 0;
    const votes: Array<{ label: string; bucket: "bullish" | "bearish" | "neutral"; raw: string }> = [];
    for (const l of lanes) {
      const raw = l.signal?.direction ?? l.regime ?? null;
      if (!raw) continue;
      const bucket = biasBucket(raw);
      if (bucket === "bullish") bullish += 1;
      else if (bucket === "bearish") bearish += 1;
      else neutral += 1;
      votes.push({ label: l.label, bucket, raw });
    }
    return { bullish, bearish, neutral, votes };
  }, [lanes]);

  const net = consensus.bullish - consensus.bearish;

  return (
    <div className="space-y-4">
      <section className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-6">
        <MetricTile label="NIFTY spot" value={formatNumber(spot, 1)} detail={snapshot?.symbol_code} />
        <MetricTile
          label="Regime"
          value={regime?.label?.replace(/_/g, " ") || "—"}
          detail={`conf ${formatPct(regime?.confidence, 0)}`}
        />
        <MetricTile label="POC" value={formatNumber(mp?.poc, 1)} />
        <MetricTile label="VAH / VAL" value={`${formatNumber(mp?.vah, 0)} / ${formatNumber(mp?.val, 0)}`} color="text-accent-blue" />
        <MetricTile label="CVD" value={formatNumber(of?.cumulative_delta, 0)} color={tone(of?.cumulative_delta)} />
        <MetricTile
          label="Bias net"
          value={net > 0 ? `+${net}` : String(net)}
          color={tone(net)}
          detail={`${consensus.bullish}▲ ${consensus.bearish}▼`}
        />
      </section>

      <Section
        title="Regime consensus"
        icon={<Scale size={16} className="text-accent-blue" />}
        description="How many strategy lanes currently read bullish / bearish / neutral (by signal direction, else regime)."
        rightSlot={
          <StatusBadge
            label={
              consensus.bullish > consensus.bearish
                ? "net bullish"
                : consensus.bearish > consensus.bullish
                  ? "net bearish"
                  : "balanced"
            }
            variant={
              consensus.bullish > consensus.bearish
                ? "success"
                : consensus.bearish > consensus.bullish
                  ? "error"
                  : "neutral"
            }
          />
        }
      >
        {consensus.votes.length ? (
          <div className="space-y-3">
            <ConsensusBar
              bullish={consensus.bullish}
              bearish={consensus.bearish}
              neutral={consensus.neutral}
            />
            <div className="flex flex-wrap gap-2">
              {consensus.votes.map((v) => (
                <span
                  key={v.label}
                  className={clsx(
                    "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-[11px] font-medium",
                    v.bucket === "bullish"
                      ? "border-accent-green/30 bg-accent-green/10 text-accent-green"
                      : v.bucket === "bearish"
                        ? "border-accent-red/30 bg-accent-red/10 text-accent-red"
                        : "border-bg-border bg-bg-primary/15 text-text-muted",
                  )}
                >
                  {v.label}
                  <span className="text-[9.5px] uppercase tracking-wide opacity-70">{v.raw.replace(/_/g, " ")}</span>
                </span>
              ))}
            </div>
          </div>
        ) : (
          <div className="py-6 text-center text-sm text-text-muted">
            No lane is reporting a directional read right now.
          </div>
        )}
      </Section>

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1.05fr)_minmax(0,1fr)]">
        <Section
          title="Market profile (TPO) · NIFTY"
          icon={<Compass size={16} />}
          description={
            mp
              ? `${mp.session_date || ""} · POC ${formatNumber(mp.poc, 1)} · VA ${formatNumber(mp.val, 0)}–${formatNumber(mp.vah, 0)}`
              : "Waiting for the Auction Intelligence NIFTY snapshot"
          }
          rightSlot={mp?.bracket_state ? <StatusBadge label={mp.bracket_state} variant="info" /> : null}
        >
          <MarketProfileChart profile={mp} lastPrice={spot} height={420} />
        </Section>

        <Section
          title="Regime"
          icon={<Scale size={16} />}
          rightSlot={
            regime?.label ? (
              <span
                className={clsx(
                  "inline-flex rounded-full border px-2.5 py-0.5 text-[10.5px] font-semibold uppercase tracking-[0.12em]",
                  regimeTone(regime.label),
                )}
              >
                {regime.label.replace(/_/g, " ")}
              </span>
            ) : null
          }
        >
          {regime ? (
            <div className="space-y-3">
              <div className="flex items-center gap-3">
                <span className="text-2xl font-semibold text-text-primary">
                  {formatPct(regime.confidence, 0)}
                </span>
                <span className="text-[11.5px] text-text-muted">
                  confidence · allows {(regime.allowed_directions || []).join(", ") || "—"}
                </span>
              </div>
              {(regime.reasons || []).slice(0, 5).map((r, i) => (
                <div key={i} className="flex items-start gap-2 text-[12.5px] text-text-secondary">
                  <span className="mt-1 h-1.5 w-1.5 shrink-0 rounded-full bg-accent-blue/70" />
                  {r}
                </div>
              ))}
            </div>
          ) : (
            <div className="py-6 text-center text-sm text-text-muted">
              No regime classification in this snapshot.
            </div>
          )}
        </Section>
      </div>

      <OrderFlowPanel of={of} />
    </div>
  );
}

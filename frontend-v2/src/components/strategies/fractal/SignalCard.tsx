"use client";

/**
 * Current-setup signal card for the Fractal desk.
 *
 * Surfaces the live setup: action, confidence, entry/stop/target with a
 * risk:reward read, the hourly-vs-daily shape pairing, the value-migration
 * score, the trade bucket + trajectory (drifting / accepting / rejecting
 * and improving / deteriorating), order-flow alignment, plus the rationale
 * and the filters that gate the setup from firing.
 */
import { CheckCircle2, Filter, ShieldAlert, Target } from "lucide-react";

import {
  MetricTile,
  Section,
  StatusBadge,
  formatNumber,
  formatPct,
  formatSignedNumber,
  tone,
} from "@/components/desk-ui";
import type { OrderFlow } from "@/components/strategies/shared";

export type CurrentSignal = {
  underlying?: string;
  signal_time?: string;
  setup_name?: string;
  action?: string;
  confidence?: number | null;
  horizon?: string;
  actionable?: boolean;
  latest_close?: number | null;
  entry_trigger?: number | null;
  stop_level?: number | null;
  target_level?: number | null;
  hourly_shape?: string | null;
  daily_shape?: string | null;
  hourly_number?: number | null;
  value_migration_score?: number | null;
  daily_context?: string | null;
  rationale?: string[] | null;
  filters?: string[] | null;
  order_flow_bias?: OrderFlow | null;
  bucket?: string | null;
  trajectory?: string | null;
  proximity_pct?: number | null;
  bucket_rationale?: string | null;
  metadata?: {
    daily_direction?: string | null;
    order_flow_direction?: string | null;
    order_flow_alignment?: number | null;
    advisories?: string[] | null;
  } | null;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  [k: string]: any;
};

function actionVariant(action?: string): "success" | "error" | "warn" | "neutral" {
  const a = String(action || "").toUpperCase();
  if (a === "LONG" || a === "BUY" || a === "CALL") return "success";
  if (a === "SHORT" || a === "SELL" || a === "PUT") return "error";
  if (a === "FLAT" || a === "NO_TRADE") return "neutral";
  return "warn";
}

function trajectoryTone(t?: string | null): string {
  const s = String(t || "").toLowerCase();
  if (s.includes("improv") || s.includes("accept")) return "text-accent-green";
  if (s.includes("deterior") || s.includes("reject")) return "text-accent-red";
  return "text-text-secondary";
}

function bucketVariant(b?: string | null): "success" | "error" | "warn" | "info" | "neutral" {
  const s = String(b || "").toLowerCase();
  if (s.includes("accept")) return "success";
  if (s.includes("reject")) return "error";
  if (s.includes("drift")) return "warn";
  return "info";
}

export function SignalCard({ signal, lastPrice }: { signal?: CurrentSignal; lastPrice?: number | null }) {
  const s = signal || {};
  const conf = Number(s.confidence ?? 0);
  const entry = s.entry_trigger;
  const stop = s.stop_level;
  const target = s.target_level;
  const risk = entry != null && stop != null ? Math.abs(Number(entry) - Number(stop)) : null;
  const reward = entry != null && target != null ? Math.abs(Number(target) - Number(entry)) : null;
  const rr = risk && reward && risk > 1e-9 ? reward / risk : null;
  const vms = s.value_migration_score;
  const align = s.metadata?.order_flow_alignment;

  return (
    <div className="space-y-4">
      <Section
        title={s.setup_name ? prettySetup(s.setup_name) : "Current signal"}
        icon={<Target size={16} />}
        description={s.signal_time ? `Generated ${new Date(s.signal_time).toLocaleString("en-IN", { timeZone: "Asia/Kolkata" })}` : undefined}
        rightSlot={
          <div className="flex flex-wrap items-center gap-1.5">
            <StatusBadge label={s.action || "—"} variant={actionVariant(s.action)} />
            <StatusBadge label={s.actionable ? "actionable" : "wait"} variant={s.actionable ? "success" : "neutral"} />
            {s.horizon ? <StatusBadge label={s.horizon} variant="info" /> : null}
          </div>
        }
      >
        <div className="grid gap-4 lg:grid-cols-3">
          {/* confidence + R:R block */}
          <div className="space-y-3">
            <div>
              <div className="flex items-end justify-between">
                <span className="text-2xl font-semibold text-text-primary">{formatPct(conf, 0)}</span>
                <span className="text-[11px] text-text-muted">confidence</span>
              </div>
              <div className="mt-2 h-2 overflow-hidden rounded-full bg-bg-primary/40">
                <div
                  className="h-full rounded-full"
                  style={{
                    width: `${Math.min(100, conf * 100)}%`,
                    background: conf >= 0.6 ? "rgb(var(--accent-green))" : conf >= 0.4 ? "rgb(var(--accent-amber))" : "rgb(var(--accent-red))",
                  }}
                />
              </div>
            </div>
            <div className="grid grid-cols-3 gap-2 text-center">
              <LevelTile label="Entry" value={formatNumber(entry, 1)} />
              <LevelTile label="Stop" value={formatNumber(stop, 1)} accent="red" />
              <LevelTile label="Target" value={formatNumber(target, 1)} accent="green" />
            </div>
            <div className="grid grid-cols-3 gap-2 text-center text-[11px]">
              <MiniStat label="Risk" value={risk != null ? formatNumber(risk, 1) : "—"} />
              <MiniStat label="Reward" value={reward != null ? formatNumber(reward, 1) : "—"} />
              <MiniStat label="R:R" value={rr != null ? `${rr.toFixed(2)}` : "—"} color={tone(rr != null ? rr - 1 : null)} />
            </div>
          </div>

          {/* structure block */}
          <div className="grid grid-cols-2 gap-2.5 self-start">
            <MetricTile size="sm" label="Daily shape" value={s.daily_shape || "—"} detail={s.daily_context || ""} />
            <MetricTile size="sm" label={`Hourly H${s.hourly_number ?? "—"}`} value={s.hourly_shape || "—"} />
            <MetricTile size="sm" label="Value migration" value={formatSignedNumber(vms, 0)} detail="VA drift score" color={tone(vms)} />
            <MetricTile size="sm" label="Proximity" value={formatPct((s.proximity_pct ?? 0) / 100, 1)} detail="to trigger" />
          </div>

          {/* bucket / trajectory / order-flow alignment */}
          <div className="space-y-2.5 self-start">
            <div className="flex items-center justify-between rounded-lg border border-bg-border bg-bg-primary/15 px-3 py-2">
              <span className="text-[10.5px] uppercase tracking-[0.14em] text-text-muted">Bucket</span>
              <StatusBadge label={s.bucket || "—"} variant={bucketVariant(s.bucket)} />
            </div>
            <div className="flex items-center justify-between rounded-lg border border-bg-border bg-bg-primary/15 px-3 py-2">
              <span className="text-[10.5px] uppercase tracking-[0.14em] text-text-muted">Trajectory</span>
              <span className={`text-[12px] font-semibold ${trajectoryTone(s.trajectory)}`}>{s.trajectory || "—"}</span>
            </div>
            <div className="rounded-lg border border-bg-border bg-bg-primary/15 px-3 py-2">
              <div className="flex items-center justify-between">
                <span className="text-[10.5px] uppercase tracking-[0.14em] text-text-muted">OF alignment</span>
                <span className={`font-mono text-[12px] ${tone(align)}`}>{formatPct(align, 0)}</span>
              </div>
              <div className="mt-1.5 flex items-center gap-3 text-[10px] text-text-muted">
                <span>daily {s.metadata?.daily_direction || "—"}</span>
                <span>·</span>
                <span>flow {s.metadata?.order_flow_direction || "—"}</span>
              </div>
            </div>
            {lastPrice != null ? (
              <div className="text-[11px] text-text-muted">
                Last <span className="font-mono text-text-secondary">{formatNumber(lastPrice, 1)}</span>
              </div>
            ) : null}
          </div>
        </div>

        {s.bucket_rationale ? (
          <p className="mt-3 border-t border-bg-border/40 pt-3 text-[12px] text-text-secondary">{s.bucket_rationale}</p>
        ) : null}
      </Section>

      <div className="grid gap-4 lg:grid-cols-2">
        <Section title="Rationale" icon={<CheckCircle2 size={16} />}>
          {(s.rationale || []).length ? (
            <ul className="space-y-1.5">
              {s.rationale!.map((r, i) => (
                <li key={i} className="flex items-start gap-2 text-[12.5px] text-text-secondary">
                  <span className="mt-1 h-1.5 w-1.5 shrink-0 rounded-full bg-accent-green/70" />
                  {r}
                </li>
              ))}
            </ul>
          ) : (
            <div className="text-sm text-text-muted">No rationale for the current bar.</div>
          )}
        </Section>

        <Section title="Filters" icon={<Filter size={16} />} description="Gates the setup must clear before firing">
          {(s.filters || []).length ? (
            <ul className="space-y-1.5">
              {s.filters!.map((f, i) => (
                <li key={i} className="flex items-start gap-2 text-[12.5px] text-text-secondary">
                  <ShieldAlert size={13} className="mt-0.5 shrink-0 text-accent-amber" />
                  {f}
                </li>
              ))}
            </ul>
          ) : (
            <div className="text-sm text-text-muted">No blocking filters — setup is clear.</div>
          )}
          {(s.metadata?.advisories || []).length ? (
            <div className="mt-3 border-t border-bg-border/40 pt-3">
              <div className="text-[10px] uppercase tracking-[0.14em] text-text-muted">Advisories</div>
              <ul className="mt-1.5 space-y-1">
                {s.metadata!.advisories!.map((a, i) => (
                  <li key={i} className="text-[12px] text-text-muted">{a}</li>
                ))}
              </ul>
            </div>
          ) : null}
        </Section>
      </div>
    </div>
  );
}

function prettySetup(name: string): string {
  return name
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

function LevelTile({ label, value, accent }: { label: string; value: string; accent?: "red" | "green" }) {
  const color = accent === "red" ? "text-accent-red" : accent === "green" ? "text-accent-green" : "text-text-primary";
  return (
    <div className="rounded-lg border border-bg-border bg-bg-primary/15 px-2 py-1.5">
      <div className="text-[9.5px] uppercase tracking-[0.12em] text-text-muted">{label}</div>
      <div className={`mt-0.5 font-mono text-[13px] ${color}`}>{value}</div>
    </div>
  );
}

function MiniStat({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div>
      <div className="text-[9px] uppercase tracking-[0.1em] text-text-muted">{label}</div>
      <div className={`font-mono ${color ?? "text-text-secondary"}`}>{value}</div>
    </div>
  );
}

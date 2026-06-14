"use client";

/**
 * Decision Theater — the auction lane's signature live view.
 *
 * A left-to-right narrative: the three sleeve agents (positional / swing /
 * scalp) each PROPOSE an action, then the Governor (risk gate) DISPOSES —
 * allows or blocks, and at what size. Reads as "agents propose → governor
 * disposes" so an operator watching a single 8s push can see the whole
 * decision form in one glance.
 *
 * The 0.55 confidence FLOOR is drawn into every agent's confidence bar: an
 * agent below the floor is dimmed/struck because the governor will veto it
 * regardless of how good its levels look.
 */
import { ArrowRight, Gavel, Bot } from "lucide-react";

import { StatusBadge, directionTone, formatNumber, formatPct } from "@/components/desk-ui";

import type { AgentDecision, Risk } from "../types";

/** Conviction floor below which a sleeve is vetoed by the governor. */
export const CONFIDENCE_FLOOR = 0.55;

const AGENT_ORDER = ["positional", "swing", "scalp"];

function actionTone(action?: string): { chip: string; label: string } {
  const s = (action || "").toUpperCase();
  if (s === "LONG" || s === "BUY") {
    return { chip: "border-accent-green/40 bg-accent-green/12 text-accent-green", label: s };
  }
  if (s === "SHORT" || s === "SELL") {
    return { chip: "border-accent-red/40 bg-accent-red/12 text-accent-red", label: s };
  }
  return { chip: "border-bg-border bg-bg-primary/20 text-text-muted", label: s || "FLAT" };
}

/** Direction colour for the small numeric R:R / sleeve accents. */
function dirAccent(action?: string): string {
  const s = (action || "").toUpperCase();
  if (s === "LONG" || s === "BUY") return "text-accent-green";
  if (s === "SHORT" || s === "SELL") return "text-accent-red";
  return "text-text-muted";
}

/** Compute R:R = |target-entry| / |entry-stop|, guarding nulls / zero risk. */
function riskReward(entry?: number | null, stop?: number | null, target?: number | null): number | null {
  if (entry == null || stop == null || target == null) return null;
  const risk = Math.abs(entry - stop);
  if (!Number.isFinite(risk) || risk <= 0) return null;
  const reward = Math.abs(target - entry);
  if (!Number.isFinite(reward)) return null;
  return reward / risk;
}

function ConfidenceBar({ confidence, vetoed }: { confidence?: number | null; vetoed: boolean }) {
  const conf = Math.max(0, Math.min(1, Number(confidence ?? 0)));
  const pct = conf * 100;
  const floorPct = CONFIDENCE_FLOOR * 100;
  const above = conf >= CONFIDENCE_FLOOR;
  const fill = vetoed ? "rgb(var(--accent-red))" : above ? "rgb(var(--accent-green))" : "rgb(var(--accent-amber))";
  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between text-[10.5px]">
        <span className="uppercase tracking-[0.12em] text-text-muted">Conviction</span>
        <span className={`font-mono ${above ? "text-text-primary" : "text-accent-amber"}`}>
          {confidence == null ? "—" : formatPct(conf, 0)}
        </span>
      </div>
      <div className="relative h-2 overflow-hidden rounded-full bg-bg-primary/40">
        <div className="h-full rounded-full transition-[width] duration-500" style={{ width: `${pct}%`, background: fill }} />
        {/* 0.55 floor marker */}
        <div className="absolute top-0 h-full w-px bg-white/55" style={{ left: `${floorPct}%` }} title="0.55 conviction floor" />
      </div>
      <div className="text-[9.5px] text-text-muted">floor {Math.round(floorPct)}%{above ? "" : " · below → veto"}</div>
    </div>
  );
}

function Level({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div className="rounded-lg border border-bg-border bg-bg-primary/15 px-2 py-1.5">
      <div className="text-[9px] uppercase tracking-[0.1em] text-text-muted">{label}</div>
      <div className={`font-mono text-[12px] ${color || "text-text-primary"}`}>{value}</div>
    </div>
  );
}

function AgentCard({ d }: { d: AgentDecision }) {
  const conf = Number(d.confidence ?? 0);
  const belowFloor = conf < CONFIDENCE_FLOOR;
  const isFlat = (d.action || "FLAT").toUpperCase() === "FLAT";
  // Below-floor non-flat proposals get vetoed → dim + struck. Flat agents are
  // simply inactive (already waiting), shown muted but not "struck".
  const vetoed = belowFloor && !isFlat;
  const action = actionTone(d.action);
  const rr = riskReward(d.entry_price, d.stop_price, d.target_price);
  const rationale = (d.rationale || []).filter(Boolean).slice(0, 2);

  return (
    <div
      className={`relative flex w-full min-w-[210px] flex-1 flex-col gap-3 rounded-2xl border border-bg-border bg-bg-secondary/30 p-4 transition-opacity ${
        vetoed ? "opacity-55" : "opacity-100"
      }`}
    >
      {vetoed ? (
        <span className="absolute right-3 top-3">
          <StatusBadge label="vetoed" variant="error" />
        </span>
      ) : null}

      <div className="flex items-center gap-2">
        <Bot size={14} className="text-text-muted" />
        <span className="text-[12.5px] font-semibold capitalize text-text-primary">{d.agent_name || "—"}</span>
      </div>

      <div className="flex items-center gap-2">
        <span
          className={`inline-flex items-center rounded-lg border px-3 py-1 text-[15px] font-bold tracking-wide ${action.chip} ${
            vetoed ? "line-through decoration-2" : ""
          }`}
        >
          {action.label}
        </span>
        {rr != null ? (
          <span className="font-mono text-[11px] text-text-muted">
            R:R <span className={rr >= 1.5 ? "text-accent-green" : "text-text-secondary"}>{rr.toFixed(2)}</span>
          </span>
        ) : null}
      </div>

      <ConfidenceBar confidence={d.confidence} vetoed={vetoed} />

      <div className="grid grid-cols-3 gap-1.5">
        <Level label="Entry" value={formatNumber(d.entry_price, 1)} color={dirAccent(d.action)} />
        <Level label="Stop" value={formatNumber(d.stop_price, 1)} color="text-accent-red" />
        <Level label="Target" value={formatNumber(d.target_price, 1)} color="text-accent-green" />
      </div>

      <div className="flex items-center justify-between text-[10.5px] text-text-muted">
        <span>
          book <span className="font-mono text-text-secondary">{formatPct(d.sleeve_fraction, 1)}</span>
        </span>
        <span>
          qty <span className="font-mono text-text-secondary">{formatNumber(d.quantity, 0)}</span>
        </span>
      </div>

      {rationale.length ? (
        <ul className="space-y-1 border-t border-bg-border/40 pt-2">
          {rationale.map((r, i) => (
            <li key={i} className="flex items-start gap-1.5 text-[11px] leading-snug text-text-secondary">
              <span className="mt-1 h-1 w-1 shrink-0 rounded-full bg-accent-blue/70" />
              {r}
            </li>
          ))}
        </ul>
      ) : (
        <div className="border-t border-bg-border/40 pt-2 text-[11px] text-text-muted">no signal — waiting for a setup</div>
      )}
    </div>
  );
}

/** The governor verdict — allow / block + size multiplier gauge + gate reasons. */
function GovernorCard({ risk, proposals }: { risk?: Risk; proposals: AgentDecision[] }) {
  const allowed = risk?.allowed === true;
  const killSwitch = risk?.kill_switch === true;
  const mult = Math.max(0, Math.min(1, Number(risk?.max_size_multiplier ?? 0)));
  const reasons = (risk?.reasons || []).filter(Boolean);
  // Count proposals that actually cleared the floor (the ones the governor is
  // really arbitrating over).
  const live = proposals.filter(
    (p) => Number(p.confidence ?? 0) >= CONFIDENCE_FLOOR && (p.action || "FLAT").toUpperCase() !== "FLAT",
  );

  const verdict = killSwitch ? "KILL SWITCH" : allowed ? "ALLOWED" : "BLOCKED";
  const verdictChip = killSwitch
    ? "border-accent-red/50 bg-accent-red/15 text-accent-red"
    : allowed
      ? "border-accent-green/45 bg-accent-green/12 text-accent-green"
      : "border-accent-amber/45 bg-accent-amber/12 text-accent-amber";
  const multColor = mult >= 0.66 ? "rgb(var(--accent-green))" : mult >= 0.33 ? "rgb(var(--accent-amber))" : "rgb(var(--accent-red))";

  return (
    <div className="flex w-full min-w-[230px] flex-1 flex-col gap-3 rounded-2xl border border-bg-border bg-bg-secondary/40 p-4">
      <div className="flex items-center gap-2">
        <Gavel size={14} className="text-text-muted" />
        <span className="text-[12.5px] font-semibold text-text-primary">Governor</span>
        <span className="ml-auto text-[10px] uppercase tracking-[0.12em] text-text-muted">disposes</span>
      </div>

      <span className={`inline-flex w-fit items-center rounded-lg border px-3 py-1 text-[15px] font-bold tracking-wide ${verdictChip}`}>
        {verdict}
      </span>

      {/* size-multiplier gauge */}
      <div className="space-y-1">
        <div className="flex items-center justify-between text-[10.5px]">
          <span className="uppercase tracking-[0.12em] text-text-muted">Size multiplier</span>
          <span className="font-mono text-text-secondary">{formatNumber(mult, 2)}×</span>
        </div>
        <div className="h-2.5 overflow-hidden rounded-full bg-bg-primary/40">
          <div className="h-full rounded-full transition-[width] duration-500" style={{ width: `${mult * 100}%`, background: multColor }} />
        </div>
      </div>

      <div className="text-[10.5px] text-text-muted">
        {live.length ? (
          <span>
            arbitrating <span className="font-mono text-text-secondary">{live.length}</span> live proposal{live.length > 1 ? "s" : ""}
          </span>
        ) : (
          <span>no proposal cleared the floor</span>
        )}
      </div>

      {reasons.length ? (
        <div className="space-y-1 border-t border-bg-border/40 pt-2">
          <div className="text-[9.5px] uppercase tracking-[0.12em] text-text-muted">Gates fired</div>
          {reasons.map((r, i) => (
            <div key={i} className="flex items-start gap-1.5 text-[11px] leading-snug text-text-secondary">
              <span className={`mt-1 h-1 w-1 shrink-0 rounded-full ${allowed ? "bg-accent-green/70" : "bg-accent-red/70"}`} />
              {r}
            </div>
          ))}
        </div>
      ) : (
        <div className="border-t border-bg-border/40 pt-2 text-[11px] text-text-muted">
          {allowed ? "clean — no gates fired" : "blocked (no reason reported)"}
        </div>
      )}
    </div>
  );
}

export function DecisionTheater({ decisions, risk }: { decisions?: AgentDecision[]; risk?: Risk }) {
  const list = decisions || [];
  // Order positional → swing → scalp; append any unknown agent names after.
  const ordered = [...list].sort((a, b) => {
    const ai = AGENT_ORDER.indexOf((a.agent_name || "").toLowerCase());
    const bi = AGENT_ORDER.indexOf((b.agent_name || "").toLowerCase());
    return (ai < 0 ? 99 : ai) - (bi < 0 ? 99 : bi);
  });

  return (
    <section className="rounded-2xl border border-bg-border bg-bg-secondary/22 p-5">
      <div className="mb-3 flex items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2 text-sm font-semibold text-text-primary">
            <Bot size={16} />
            Decision theater
          </div>
          <div className="mt-1 text-xs text-text-muted">
            Sleeves propose → the governor disposes. Conviction below the 0.55 floor is vetoed.
          </div>
        </div>
        <StatusBadge label={`${ordered.length} sleeves`} variant="info" />
      </div>

      {ordered.length ? (
        <div className="flex flex-col items-stretch gap-3 xl:flex-row xl:items-stretch">
          {ordered.map((d, i) => (
            <div key={`${d.agent_name}-${i}`} className="flex items-stretch gap-3 xl:contents">
              <AgentCard d={d} />
              <div className="hidden items-center self-center text-text-muted xl:flex">
                <ArrowRight size={16} />
              </div>
            </div>
          ))}
          <GovernorCard risk={risk} proposals={ordered} />
        </div>
      ) : (
        <div className="py-8 text-center text-sm text-text-muted">
          No agent decisions this snapshot — the sleeves are flat, waiting for a qualifying auction setup.
        </div>
      )}
    </section>
  );
}

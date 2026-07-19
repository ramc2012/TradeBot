"use client";

/**
 * ProfileWorkbench — ONE configurable market-profile surface.
 *
 * ─── What this replaces, and why ────────────────────────────────────────────
 *
 * The app shipped TWO profile visualisations with no stated analytical
 * difference between them:
 *
 *   components/strategies/shared/MarketProfileChart.tsx  (144 L)
 *       horizontal TPO histogram. Takes ONE opaque `profile` object and
 *       normalises `tpo_rows | tpo_counts` itself.
 *   components/mpof/ProfileLadder.tsx                    (360 L)
 *       vertical level ladder. Takes ~16 flat props, and renders everything the
 *       histogram does PLUS the volume-profile overlay, prior-session ghosts,
 *       HVN dots, zoom-to-value-area and an expand modal.
 *
 * The ladder is a strict functional superset of the histogram except for the
 * payload-shape normaliser. So the Workbench is exactly that pairing: the
 * ladder's renderer, fed by the histogram's adapter, behind ONE prop API —
 * `mode` (tpo / volume / combined), `orientation`, overlay toggles, and an
 * explicit availability statement for the overlays no lane emits.
 *
 * MIGRATION (deliberate, and this pass does only step A):
 *   A. Build the Workbench; use it in the Structure view ONLY.       ← here
 *   B. Re-export MarketProfileChart / ProfileLadder as thin adapters over it,
 *      prop-for-prop, so the 9 existing desk call sites compile unchanged.
 *   C. Delete the adapters.
 * The nine live desks are NOT touched here. That is where the regression risk
 * lives, and a silent blank profile on the Auction or Convergence desk would
 * cost more than the duplication does.
 *
 * ─── Honesty rules this surface enforces ────────────────────────────────────
 *
 *   · Naked POC and LVN are NOT computed by any wired lane. They render as
 *     explicitly unavailable, never as a client-side derivation. Deriving them
 *     here would be inventing structure the lanes never asserted.
 *   · The weekly / composite overlay has an index-scoped source only
 *     (/api/auction-intelligence/mp-multi-tf-profile, /api/commodity/
 *     profile-history/{root}); it is declared as not wired in this pass rather
 *     than shown as an empty toggle that does nothing.
 *   · Value migration is a real field pair (`value_migration_state` /
 *     `value_migration_direction`). Absent ⇒ no arrow AND a statement that the
 *     lane did not emit one, which is different from "no migration".
 */
import { ArrowDown, ArrowUp, Minus } from "lucide-react";
import { useMemo, useState } from "react";

import { StatusBadge, formatNumber } from "@/components/desk-ui";
import { ProfileLadder } from "@/components/mpof";
import { MarketProfileChart, normalizeTpo } from "@/components/strategies/shared";

/* eslint-disable @typescript-eslint/no-explicit-any */

export type ProfileMode = "tpo" | "volume" | "combined";
export type ProfileOrientation = "ladder" | "histogram";

export type ProfileMigration = {
  state?: string | null;
  direction?: string | null;
} | null;

const MODES: Array<{ key: ProfileMode; label: string; note: string }> = [
  { key: "tpo", label: "TPO", note: "time-at-price letter counts" },
  { key: "volume", label: "Volume", note: "traded volume at price" },
  { key: "combined", label: "Both", note: "TPO bars with the volume profile overlaid" },
];

/**
 * Overlays with NO source in any wired payload. Declared as data so the panel
 * cannot drift from what it says, and so a future field is a one-line change.
 */
const UNAVAILABLE_OVERLAYS: Array<{ label: string; reason: string }> = [
  {
    label: "Naked POC",
    reason:
      "no wired lane emits an untested/naked POC list — neither the convergence profile block nor the commodity index monitor carries one. Deriving it here would assert structure no lane computed.",
  },
  {
    label: "LVN",
    reason:
      "low-volume nodes are not emitted. The convergence profile carries hvn_prices only, so the high-volume side is real and the low-volume side has no source.",
  },
  {
    label: "Weekly / composite",
    reason:
      "the only sources are /api/auction-intelligence/mp-multi-tf-profile and /api/commodity/profile-history/{root}, both index/commodity-scoped and not wired into this view in this pass.",
  },
];

export function ProfileWorkbench({
  profile,
  spot,
  digits = 2,
  height = 340,
  migration,
  unavailableReason,
  title,
  sourceNote,
}: {
  /** Raw lane profile payload — shape-normalised here, not by the caller. */
  profile: any | null;
  spot?: number | null;
  digits?: number;
  height?: number;
  migration?: ProfileMigration;
  /** Non-null ⇒ render the statement instead of an empty frame. */
  unavailableReason?: string | null;
  title?: string;
  /**
   * Which payload this distribution came from. Rendered, because another panel
   * on the same screen may print the SAME level name off a different payload.
   */
  sourceNote?: string | null;
}) {
  const [mode, setMode] = useState<ProfileMode>("tpo");
  const [orientation, setOrientation] = useState<ProfileOrientation>("ladder");
  const [showPrior, setShowPrior] = useState(true);

  const tpoRows = useMemo(() => normalizeTpo(profile), [profile]);

  const volumeByPrice = useMemo(() => {
    const raw =
      profile?.volume_by_price ??
      profile?.volume_profile ??
      profile?.volume_at_price ??
      null;
    if (Array.isArray(raw)) {
      return raw
        .map((r: any) => ({ price: Number(r?.price ?? r?.[0]), volume: Number(r?.volume ?? r?.[1]) }))
        .filter((r) => Number.isFinite(r.price) && Number.isFinite(r.volume) && r.volume > 0);
    }
    if (raw && typeof raw === "object") {
      return Object.entries(raw)
        .map(([p, v]) => ({ price: Number(p), volume: Number(v) }))
        .filter((r) => Number.isFinite(r.price) && Number.isFinite(r.volume) && r.volume > 0);
    }
    return [];
  }, [profile]);

  const tpoCounts = useMemo(() => {
    if (!tpoRows.length) return null;
    const out: Record<string, number> = {};
    for (const r of tpoRows) out[String(r.price)] = r.count;
    return out;
  }, [tpoRows]);

  const tpoLetters = useMemo(() => {
    const out: Record<string, string> = {};
    let any = false;
    for (const r of tpoRows) {
      if (r.letters) {
        out[String(r.price)] = r.letters;
        any = true;
      }
    }
    return any ? out : null;
  }, [tpoRows]);

  const hasTpo = tpoRows.length > 0;
  const hasVolume = volumeByPrice.length > 0;
  const hasPrior =
    profile?.prior != null &&
    [profile.prior.vah, profile.prior.val, profile.prior.poc].some(
      (v: any) => Number.isFinite(Number(v)) && Number(v) !== 0,
    );

  if (unavailableReason) {
    return (
      <div className="rounded-xl border border-dashed border-bg-border/70 bg-bg-primary/10 p-4">
        <div className="flex items-center gap-2">
          <StatusBadge label="profile unavailable" variant="neutral" />
        </div>
        <p className="mt-2 max-w-prose text-[11.5px] leading-5 text-text-muted">{unavailableReason}</p>
      </div>
    );
  }

  const effectiveMode: ProfileMode = mode === "tpo" && !hasTpo && hasVolume ? "volume" : mode;

  return (
    <div className="space-y-3">
      {sourceNote ? (
        <p className="font-mono text-[10px] leading-4 text-text-muted">
          distribution + levels from {sourceNote}
        </p>
      ) : null}
      <div className="flex flex-wrap items-center gap-1.5">
        {MODES.map((m) => {
          const disabled =
            (m.key === "tpo" && !hasTpo) ||
            (m.key === "volume" && !hasVolume) ||
            (m.key === "combined" && !(hasTpo && hasVolume));
          return (
            <button
              key={m.key}
              type="button"
              disabled={disabled}
              onClick={() => setMode(m.key)}
              title={
                disabled
                  ? m.key === "tpo"
                    ? "no TPO distribution in this payload"
                    : m.key === "volume"
                      ? "no volume-at-price in this payload"
                      : "needs both a TPO distribution and volume-at-price"
                  : m.note
              }
              className={
                "rounded-lg border px-2 py-0.5 text-[10.5px] font-semibold uppercase tracking-[0.08em] transition-colors " +
                (disabled
                  ? "cursor-not-allowed border-bg-border/50 text-text-muted/50"
                  : effectiveMode === m.key
                    ? "border-accent-blue/60 bg-accent-blue/15 text-accent-blue"
                    : "border-bg-border text-text-muted hover:text-text-primary")
              }
            >
              {m.label}
            </button>
          );
        })}

        <span className="mx-1 h-3 w-px bg-bg-border" aria-hidden />

        {(["ladder", "histogram"] as ProfileOrientation[]).map((o) => (
          <button
            key={o}
            type="button"
            onClick={() => setOrientation(o)}
            className={
              "rounded-lg border px-2 py-0.5 text-[10.5px] font-semibold uppercase tracking-[0.08em] transition-colors " +
              (orientation === o
                ? "border-accent-blue/60 bg-accent-blue/15 text-accent-blue"
                : "border-bg-border text-text-muted hover:text-text-primary")
            }
          >
            {o}
          </button>
        ))}

        <span className="mx-1 h-3 w-px bg-bg-border" aria-hidden />

        <button
          type="button"
          disabled={!hasPrior}
          onClick={() => setShowPrior((v) => !v)}
          title={hasPrior ? "prior-session VAH / VAL / POC ghosts" : "the payload carries no prior-session levels"}
          className={
            "rounded-lg border px-2 py-0.5 text-[10.5px] font-semibold uppercase tracking-[0.08em] transition-colors " +
            (!hasPrior
              ? "cursor-not-allowed border-bg-border/50 text-text-muted/50"
              : showPrior
                ? "border-accent-blue/60 bg-accent-blue/15 text-accent-blue"
                : "border-bg-border text-text-muted hover:text-text-primary")
          }
        >
          prior
        </button>
      </div>

      {orientation === "ladder" ? (
        <ProfileLadder
          spot={spot}
          vah={profile?.vah}
          val={profile?.val}
          poc={profile?.poc}
          ibHigh={profile?.initial_balance_high ?? profile?.ib_high}
          ibLow={profile?.initial_balance_low ?? profile?.ib_low}
          dayHigh={profile?.high_price}
          dayLow={profile?.low_price}
          prior={showPrior ? profile?.prior : null}
          hvnPrices={profile?.hvn_prices}
          singlePrints={profile?.single_prints}
          poorHigh={profile?.poor_high}
          poorLow={profile?.poor_low}
          tpoCounts={tpoCounts}
          tpoLetters={tpoLetters}
          volumeByPrice={volumeByPrice.length ? volumeByPrice : null}
          defaultShowTpo={effectiveMode !== "volume"}
          defaultShowVol={effectiveMode !== "tpo"}
          height={height}
          digits={digits}
          expandTitle={title}
          // Remount on mode/orientation change so the histogram defaults are
          // re-applied — the ladder owns its toggles as internal state, and this
          // is the seam that keeps the existing desks' behaviour untouched.
          key={`${effectiveMode}:${orientation}`}
        />
      ) : (
        <MarketProfileChart profile={profile} lastPrice={spot ?? null} height={height} />
      )}

      <MigrationRow migration={migration} />

      <div className="rounded-lg border border-bg-border/60 bg-bg-primary/10 px-2.5 py-2">
        <div className="text-[9.5px] uppercase tracking-[0.14em] text-text-muted">
          Overlays with no source
        </div>
        <ul className="mt-1 space-y-1">
          {UNAVAILABLE_OVERLAYS.map((o) => (
            <li key={o.label} className="text-[10.5px] leading-4 text-text-muted">
              <span className="font-semibold text-text-secondary/80">{o.label}</span>
              <span className="mx-1 text-text-muted">·</span>
              <span>unavailable — {o.reason}</span>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}

function MigrationRow({ migration }: { migration?: ProfileMigration }) {
  const state = migration?.state ?? null;
  const direction = String(migration?.direction ?? "").toLowerCase();
  if (!state && !direction) {
    return (
      <p className="text-[10.5px] leading-4 text-text-muted">
        Value migration not emitted for this instrument — the field pair is
        served by the commodity/index MP monitor only.
      </p>
    );
  }
  const Icon = direction.includes("up") || direction.includes("high")
    ? ArrowUp
    : direction.includes("down") || direction.includes("low")
      ? ArrowDown
      : Minus;
  return (
    <div className="flex items-center gap-2 rounded-lg border border-bg-border/60 px-2.5 py-1.5">
      <Icon size={13} className="text-accent-blue" />
      <span className="text-[11px] text-text-secondary">
        value migration: <span className="font-semibold">{state ?? "state not reported"}</span>
        {direction ? <span className="text-text-muted"> · {direction}</span> : null}
      </span>
    </div>
  );
}

/** Compact level readout — used beside the workbench in the Structure rail. */
export function ProfileLevelReadout({
  levels,
  digits = 2,
}: {
  levels: Record<string, number | null>;
  digits?: number;
}) {
  const entries = Object.entries(levels);
  return (
    <dl className="grid grid-cols-2 gap-x-3 gap-y-1">
      {entries.map(([label, value]) => (
        <div key={label} className="flex items-baseline justify-between gap-2">
          <dt className="text-[10px] uppercase tracking-[0.1em] text-text-muted">{label}</dt>
          <dd
            className={
              "font-mono text-[11.5px] " + (value == null ? "text-text-muted" : "text-text-secondary")
            }
            title={value == null ? "no source for this level in the served payload" : undefined}
          >
            {value == null ? "UNAVAILABLE" : formatNumber(value, digits)}
          </dd>
        </div>
      ))}
    </dl>
  );
}

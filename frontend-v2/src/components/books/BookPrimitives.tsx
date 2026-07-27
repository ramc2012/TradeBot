"use client";

/**
 * BookPrimitives — the honesty affordances shared by all four book views.
 *
 * Everything here exists to keep four facts visually distinct, because
 * collapsing them is how a terminal starts lying:
 *
 *   UNAVAILABLE  — the book does not carry this. Never a 0, never an em-dash
 *                  on its own; it says the word and carries the reason.
 *   measured 0   — the book carries it and it is zero. That is information.
 *   stale        — the observation exists but is too old to act on. A stale
 *                  mark makes unrealised P&L UNKNOWN, not "last known".
 *   never fired  — a running lane that has genuinely never traded. Not missing.
 *
 * The colour ladder is the shipped one (StatusBadge variants): GREEN only for
 * running / healthy-live / actionable-confirmed, ARMED is blue.
 */
import { clsx } from "clsx";
import { AlertTriangle, Database, FileJson, Info } from "lucide-react";

import { MetricTile, Section, StatusBadge, formatIST, formatMoney, formatSignedMoney } from "@/components/desk-ui";
import {
  MARK_CLOCK_LABEL,
  MARK_STALE_LABEL,
  ORDER_LAYER_LABEL,
  ORDER_LAYER_VARIANT,
  type DayFigure,
  type FieldAvailability,
  type LaneBook,
  type MarkVerdict,
  type Quantity,
  type UnrealizedVerdict,
  unavailableReason,
} from "@/lib/lane-books";
import { formatAgeShort } from "@/lib/market-semantics";

// ─── UNAVAILABLE ────────────────────────────────────────────────────────────

/**
 * The one renderer for "this book does not carry that". It always says the
 * word and always carries the reason on hover, so a reader can tell an absent
 * field from a measured zero without opening the payload.
 */
export function Unavailable({
  reason,
  label = "UNAVAILABLE",
  className,
}: {
  reason: string;
  label?: string;
  className?: string;
}) {
  return (
    <span
      title={reason}
      className={clsx(
        "cursor-help border-b border-dotted border-text-muted/60 font-mono text-[10.5px] uppercase tracking-[0.1em] text-text-muted",
        className,
      )}
    >
      {label}
    </span>
  );
}

/** A number, or UNAVAILABLE with the book's declared reason for its absence. */
export function FieldValue({
  value,
  field,
  format,
  className,
}: {
  value: number | null | undefined;
  /** The book's declared availability for this field — supplies the reason. */
  field: FieldAvailability;
  format: (v: number) => string;
  className?: string;
}) {
  if (value == null || !Number.isFinite(Number(value))) {
    return (
      <Unavailable
        reason={
          unavailableReason(field) ??
          "This row does not carry the field. It is absent, not zero."
        }
        className={className}
      />
    );
  }
  const n = Number(value);
  return (
    <span className={clsx("font-mono", n === 0 ? "text-text-secondary" : "text-text-primary", className)}
      title={n === 0 ? "measured zero" : undefined}
    >
      {format(n)}
    </span>
  );
}

// ─── Source banner ──────────────────────────────────────────────────────────

/**
 * Every page states its source. Not "paper book" — the actual table name or the
 * actual repo path, the endpoints that served it, and the row counts read, so a
 * future reader can tell exactly where a number came from.
 */
export function BookSourceBanner({
  book,
  counts,
  lastWriteAt,
  errors = [],
}: {
  book: LaneBook;
  /** What was actually read, e.g. {orders: 91, trades: 41, open: 0}. */
  counts?: Record<string, number | null>;
  lastWriteAt?: string | null;
  errors?: string[];
}) {
  const Icon = book.source.kind === "postgres_table" ? Database : FileJson;
  return (
    <div className="rounded-2xl border border-bg-border bg-bg-primary/16 px-4 py-3">
      <div className="flex flex-wrap items-center gap-2">
        <Icon size={14} className="shrink-0 text-text-muted" />
        <span className="text-[10.5px] font-semibold uppercase tracking-[0.16em] text-text-muted">
          Source book
        </span>
        <span className="font-mono text-[11.5px] text-text-primary">{book.source.path}</span>
        <StatusBadge
          label={book.source.kind === "postgres_table" ? "postgres" : "runtime json"}
          variant="neutral"
        />
        <StatusBadge label={book.market} variant="neutral" />
        <StatusBadge label={ORDER_LAYER_LABEL[book.orderLayer]} variant={ORDER_LAYER_VARIANT[book.orderLayer]} />
      </div>
      <p className="mt-1.5 text-[11px] leading-relaxed text-text-secondary">{book.source.note}</p>
      <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[10px] text-text-muted">
        <span>served by {book.source.servedBy.join(" · ")}</span>
        {counts
          ? Object.entries(counts).map(([k, v]) => (
              <span key={k}>
                {k}: {v == null ? "UNAVAILABLE" : v}
              </span>
            ))
          : null}
        <span>last write: {lastWriteAt ? formatIST(lastWriteAt) : "UNAVAILABLE"}</span>
      </div>
      {errors.length ? (
        <div className="mt-2 flex flex-wrap items-center gap-2 rounded-lg border border-accent-amber/35 bg-accent-amber/8 px-2.5 py-1.5">
          <AlertTriangle size={12} className="text-accent-amber" />
          <span className="text-[11px] text-accent-amber">
            {errors.length} source{errors.length > 1 ? "s" : ""} did not answer: {errors.join("; ")}. Anything they
            feed reads UNAVAILABLE below — it is not an empty book.
          </span>
        </div>
      ) : null}
    </div>
  );
}

// ─── Never fired ────────────────────────────────────────────────────────────

/**
 * A running lane that has never opened a position. This is a MEASURED ZERO and
 * must never look like a fetch that failed, so it renders as its own state with
 * the capital it has not touched.
 */
export function NeverFiredState({
  book,
  initialCapital,
  what,
}: {
  book: LaneBook;
  initialCapital: number | null;
  /** "orders" | "trades" | "positions" — what specifically is zero here. */
  what: string;
}) {
  return (
    <div className="rounded-2xl border border-accent-blue/30 bg-accent-blue/6 p-5">
      <div className="flex flex-wrap items-center gap-2">
        <Info size={14} className="text-accent-blue" />
        <StatusBadge label="measured zero · never fired" variant="info" />
        <span className="text-sm font-semibold text-text-primary">0 {what} since inception</span>
      </div>
      <p className="mt-2 max-w-3xl text-[12px] leading-relaxed text-text-secondary">{book.neverFired}</p>
      <div className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
        <MetricTile size="sm" label={`${what} recorded`} value="0" detail="measured, not missing" />
        <MetricTile
          size="sm"
          label="Capital"
          value={initialCapital == null ? "UNAVAILABLE" : formatMoney(initialCapital)}
          detail="untouched since inception"
        />
      </div>
    </div>
  );
}

// ─── Quantity: lots AND units ───────────────────────────────────────────────

/**
 * Lots and units, always both. `qty = lots × lot_size` is STRUCTURAL, so a
 * four-digit unit count on a cheap name is a fact about the contract and is
 * never flagged as an anomaly.
 */
export function QtyCell({ qty, className }: { qty: Quantity; className?: string }) {
  if (qty.lots == null && qty.units == null) {
    return <Unavailable reason="Neither a lot count nor a unit count is recorded on this row." />;
  }
  return (
    <span className={clsx("font-mono text-[11px]", className)}>
      {qty.lots != null ? (
        <span className="text-text-primary">
          {qty.lots} lot{Math.abs(qty.lots) === 1 ? "" : "s"}
        </span>
      ) : (
        <span className="text-text-muted" title="no lot count on this row">
          lots —
        </span>
      )}
      <span className="text-text-muted">
        {" · "}
        {qty.units != null ? `${qty.units.toLocaleString("en-IN")} units` : "units —"}
        {qty.lotSize != null ? ` (×${qty.lotSize})` : ""}
      </span>
    </span>
  );
}

// ─── Mark freshness ─────────────────────────────────────────────────────────

/**
 * Mark age, on the clock the book actually has. An auction position has no
 * mark_time at all, so its chip says BOOK-SYNC AGE — reading it as a tick clock
 * is how a stale premium from a different strike passed for a live mark.
 */
export function MarkAgeChip({ verdict }: { verdict: MarkVerdict }) {
  const label = MARK_CLOCK_LABEL[verdict.clock];
  if (verdict.state === "absent") {
    return <StatusBadge label={`${label} unknown`} variant="neutral" />;
  }
  return (
    <StatusBadge
      label={`${label} ${formatAgeShort(verdict.ageSeconds)}`}
      variant={verdict.state === "fresh" ? "success" : "warn"}
    />
  );
}

/**
 * Unrealised P&L under the stale-mark rule: past the staleness cutoff the
 * number is not shown at all. "Last known" is not a P&L.
 */
export function UnrealizedCell({
  value,
  verdict,
}: {
  value: number | null;
  verdict: MarkVerdict;
}) {
  if (verdict.state === "absent") {
    return (
      <Unavailable
        label="UNKNOWN"
        reason="There is no timestamp for the mark this P&L was computed against, so its age cannot be established and the number cannot be trusted."
      />
    );
  }
  if (verdict.state === "stale") {
    // Named on the clock that produced it: a book with no mark_time has a
    // BOOK-SYNC clock, and calling that a "mark" is the mislabel that let a
    // last-synced timestamp pass for a tick clock.
    return (
      <span
        className="cursor-help border-b border-dotted border-accent-amber/60 font-mono text-[10.5px] uppercase tracking-[0.1em] text-accent-amber"
        title={`The ${MARK_CLOCK_LABEL[verdict.clock]} is ${formatAgeShort(verdict.ageSeconds)}. Unrealised P&L against an observation this old is unknown, not "last known".`}
      >
        UNKNOWN · {MARK_STALE_LABEL[verdict.clock]} {formatAgeShort(verdict.ageSeconds)}
      </span>
    );
  }
  if (value == null) {
    return <Unavailable reason="The book carries no unrealised P&L for this row." />;
  }
  return (
    <span className={clsx("font-mono font-semibold", value > 0 ? "text-accent-green" : value < 0 ? "text-accent-red" : "text-text-secondary")}>
      {formatSignedMoney(value)}
    </span>
  );
}

/**
 * The portfolio's unrealised tile, under the SAME stale-mark rule the positions
 * view applies. A roll-up that prints +9,760 while every position beneath it
 * reads UNKNOWN is the same lie one screen away.
 */
export function UnrealizedRollupTile({ verdict }: { verdict: UnrealizedVerdict }) {
  if (verdict.state === "known") {
    return (
      <MetricTile
        label="Unrealised (open)"
        value={formatSignedMoney(verdict.value)}
        detail={verdict.note}
        color={verdict.value > 0 ? "text-accent-green" : verdict.value < 0 ? "text-accent-red" : undefined}
      />
    );
  }
  if (verdict.state === "measured_zero") {
    return <MetricTile label="Unrealised (open)" value={formatSignedMoney(0)} detail={verdict.note} />;
  }
  const isUnknown = verdict.state === "unknown";
  const detail =
    isUnknown && verdict.totalRows
      ? `${verdict.staleRows} of ${verdict.totalRows} open marks are too old to act on, so the roll-up cannot be stated — the positions view suppresses each of them for the same reason`
      : verdict.note;
  return (
    <div
      className={clsx(
        "rounded-2xl border px-4 py-3",
        isUnknown ? "border-accent-amber/35 bg-accent-amber/6" : "border-bg-border bg-bg-secondary/28",
      )}
      title={detail}
    >
      <div className="text-[10.5px] uppercase tracking-[0.16em] text-text-muted">Unrealised (open)</div>
      <div className={clsx("mt-1 font-mono text-lg font-semibold", isUnknown ? "text-accent-amber" : "text-text-muted")}>
        {isUnknown ? "UNKNOWN" : "UNAVAILABLE"}
      </div>
      <div className="mt-0.5 text-[11px] leading-tight text-text-muted">{detail}</div>
    </div>
  );
}

// ─── Day vs lifetime ────────────────────────────────────────────────────────

/**
 * The day tile. It never borrows a lifetime accessor, and a served daily series
 * whose newest row is not today resolves to "no session today" — the defect
 * being fixed here is a dashboard that printed LIFETIME realized P&L under a
 * "today" heading.
 */
export function DayTile({ day, label = "Realized TODAY" }: { day: DayFigure; label?: string }) {
  switch (day.state) {
    case "derived":
      return (
        <MetricTile
          label={`${label} · derived`}
          value={formatSignedMoney(day.realized)}
          detail={`${day.trades} close${day.trades === 1 ? "" : "s"} on ${day.dayKey} IST · ${day.wins}W`}
          color={day.realized >= 0 ? "text-accent-green" : "text-accent-red"}
        />
      );
    case "served":
      return (
        <MetricTile
          label={`${label} · served`}
          value={formatSignedMoney(day.realized)}
          detail={`dated ${day.dayKey}${day.trades != null ? ` · ${day.trades} trades` : ""}`}
          color={day.realized >= 0 ? "text-accent-green" : "text-accent-red"}
        />
      );
    case "no_session_today":
      return (
        <MetricTile
          label={label}
          value="NO SESSION TODAY"
          detail={
            day.lastSessionDay
              ? `nothing closed on ${day.dayKey} IST; last session was ${day.lastSessionDay}`
              : `nothing closed on ${day.dayKey} IST`
          }
        />
      );
    case "never_traded":
      return <MetricTile label={label} value="NEVER TRADED" detail="no day figure exists to show" />;
    default:
      return <MetricTile label={label} value="UNAVAILABLE" detail={day.note} />;
  }
}

/** The explicit day-vs-lifetime caption every portfolio view carries. */
export function DayLifetimeNote({ book, day }: { book: LaneBook; day: DayFigure }) {
  return (
    <p className="mt-2 text-[11px] leading-relaxed text-text-muted">
      <span className="font-semibold uppercase tracking-[0.12em] text-text-secondary">Day vs lifetime · </span>
      {book.day.note}
      {day.state === "no_session_today"
        ? " Today resolves to NO SESSION TODAY rather than the last session's number."
        : ""}
    </p>
  );
}

// ─── Field-availability legend ──────────────────────────────────────────────

/** Names, per view, what this book structurally cannot report — and why. */
export function AbsentFieldsNote({
  book,
  fields,
}: {
  book: LaneBook;
  fields: (keyof LaneBook["fields"])[];
}) {
  const rows = fields
    .map((f) => ({ key: f, value: book.fields[f] }))
    .filter((r) => r.value.state !== "available");
  if (!rows.length) return null;
  return (
    <div className="mt-3 rounded-xl border border-bg-border bg-bg-primary/12 px-3 py-2">
      <div className="text-[10px] font-semibold uppercase tracking-[0.16em] text-text-muted">
        Not reportable on this book
      </div>
      <ul className="mt-1 space-y-1">
        {rows.map((r) => (
          <li key={String(r.key)} className="text-[11px] leading-relaxed text-text-secondary">
            <StatusBadge
              label={r.value.state === "unavailable" ? "unavailable" : "partial"}
              variant={r.value.state === "unavailable" ? "neutral" : "warn"}
              className="mr-1.5 align-middle"
            />
            <span className="font-mono text-[10.5px] text-text-primary">{String(r.key)}</span>
            {" — "}
            {unavailableReason(r.value)}
          </li>
        ))}
      </ul>
    </div>
  );
}

// ─── Empty (but measured) states ────────────────────────────────────────────

export function MeasuredEmpty({ what, detail }: { what: string; detail: string }) {
  return (
    <div className="rounded-xl border border-bg-border bg-bg-primary/10 px-4 py-8 text-center">
      <StatusBadge label="measured zero" variant="info" />
      <div className="mt-2 text-sm text-text-secondary">No {what} on this book.</div>
      <div className="mt-1 text-[11px] text-text-muted">{detail}</div>
    </div>
  );
}

export function SourceUnavailable({ what, reason }: { what: string; reason: string }) {
  return (
    <div className="rounded-xl border border-accent-amber/35 bg-accent-amber/8 px-4 py-8 text-center">
      <StatusBadge label="source did not answer" variant="warn" />
      <div className="mt-2 text-sm text-accent-amber">{what} UNAVAILABLE.</div>
      <div className="mt-1 text-[11px] text-text-muted">{reason} This is not an empty book.</div>
    </div>
  );
}

export { Section };

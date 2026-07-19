"use client";

/**
 * Matrix column definitions — one declarative table so the header, the row
 * renderer, the sort comparator and the keyboard column-focus all agree.
 *
 * `sortValue` returns null for "no source"; the comparator always sinks nulls
 * to the bottom regardless of direction, so sorting can never make a missing
 * cell look like a small number.
 */
import { formatNumber, formatAgeShortish } from "./format";
import { IntentGlyph, ReadinessGlyph, RiskGlyph, SignalGlyph, StageGlyph, Unavailable } from "../glyphs";
import type { MatrixRow } from "./useUniverseMatrix";

export type MatrixColumn = {
  key: string;
  label: string;
  /** CSS grid track. */
  width: string;
  align?: "left" | "right";
  /** null ⇒ not sortable. */
  sortValue: ((r: MatrixRow) => number | string | null) | null;
  render: (r: MatrixRow) => React.ReactNode;
  title?: string;
};

export const MATRIX_COLUMNS: MatrixColumn[] = [
  {
    key: "symbol",
    label: "Instrument",
    width: "minmax(120px, 1.1fr)",
    sortValue: (r) => r.symbol,
    title: "Underlying / root. Selecting a row re-pins the whole workspace.",
    render: (r) => (
      // The symbol never truncates — the kind label yields first, because a
      // half-rendered ticker is unreadable while a clipped "commodity" is not.
      <span className="flex min-w-0 items-baseline gap-1.5">
        <span className="shrink-0 font-mono font-semibold text-text-primary">{r.symbol}</span>
        <span className="min-w-0 truncate text-[9.5px] uppercase tracking-[0.12em] text-text-muted">
          {String(r.kind || "").toLowerCase()}
        </span>
      </span>
    ),
  },
  {
    key: "spot",
    label: "Spot",
    width: "minmax(80px, 0.7fr)",
    align: "right",
    sortValue: (r) => r.spot,
    render: (r) =>
      r.spot == null ? (
        <Unavailable reason="no spot in the universe payload" />
      ) : (
        <span className="font-mono text-text-secondary">{formatNumber(r.spot, 2)}</span>
      ),
  },
  {
    key: "readiness",
    label: "Readiness",
    width: "minmax(96px, 0.85fr)",
    sortValue: (r) => (r.ageSeconds == null ? null : -r.ageSeconds),
    title:
      "Age of this instrument's own observation, plus whether it is usable. Rows whose only source is a CVD/footprint stream grade INFERRED FROM QUOTES, never OBSERVED.",
    render: (r) => (
      <ReadinessGlyph
        freshness={r.freshness}
        sufficiency={r.sufficiency}
        age={formatAgeShortish(r.ageSeconds)}
        reasons={r.readinessReasons}
      />
    ),
  },
  {
    key: "mp",
    label: "MP regime",
    width: "minmax(140px, 1.1fr)",
    sortValue: (r) => r.mp.regime ?? null,
    title: "Market-profile regime and value migration.",
    render: (r) =>
      !r.mp.available ? (
        <Unavailable reason={r.mp.reason} />
      ) : (
        <span
          className="truncate font-mono text-text-secondary"
          title={[
            `day type: ${r.mp.dayType ?? "not reported"}`,
            `POC/VAH/VAL: ${formatNumber(r.mp.poc, 1)} / ${formatNumber(r.mp.vah, 1)} / ${formatNumber(r.mp.val, 1)}`,
            `value migration: ${r.mp.migrationState ?? "not reported"} ${r.mp.migrationDirection ?? ""}`,
          ].join("\n")}
        >
          {(r.mp.regime ?? "—").replace(/_/g, " ")}
          {r.mp.migrationDirection ? (
            <span className="ml-1 text-text-muted">{r.mp.migrationDirection === "up" ? "↑" : r.mp.migrationDirection === "down" ? "↓" : "·"}</span>
          ) : null}
        </span>
      ),
  },
  {
    key: "auction",
    label: "Auction",
    width: "minmax(110px, 0.9fr)",
    sortValue: (r) => r.auction.regime ?? null,
    title:
      "Auction-intelligence state. No universe-scale endpoint exists — this fills in per selection.",
    render: (r) =>
      !r.auction.available ? (
        <Unavailable reason={r.auction.reason} />
      ) : (
        <span
          className="truncate font-mono text-text-secondary"
          title={r.auction.reasons.length ? r.auction.reasons.join("; ") : undefined}
        >
          {(r.auction.regime ?? "loaded").replace(/_/g, " ")}
        </span>
      ),
  },
  {
    key: "mpof",
    label: "MP + OF",
    width: "minmax(150px, 1.2fr)",
    sortValue: (r) => r.mpof.confidence,
    title:
      "Order-flow-confirmed setup and its strength, with the block reason when there is none. Order flow here is inferred from the quote stream — the feed carries no aggressor-tagged trade prints.",
    render: (r) =>
      !r.mpof.available ? (
        <Unavailable reason={r.mpof.reason} />
      ) : (
        <SignalGlyph
          signal={r.mpof.signal}
          candidate={r.mpof.candidate}
          reason={r.mpof.blockReason}
          confidence={r.mpof.confidence}
          detail={r.mpof.detail}
        />
      ),
  },
  {
    key: "convergence",
    label: "Convergence",
    width: "minmax(150px, 1.2fr)",
    sortValue: (r) => r.convergence.score,
    title:
      "Institutional-convergence stage, confirmations met/required, and blocker count. The CVD and footprint confirmations are inferred from quotes — no aggressor trade tape exists on this feed.",
    render: (r) =>
      !r.convergence.available ? (
        <Unavailable reason={r.convergence.reason} />
      ) : (
        <StageGlyph
          stage={r.convergence.setupState}
          confirmations={r.convergence.confirmations}
          required={r.convergence.required}
          blocked={r.convergence.blocked}
        />
      ),
  },
  {
    key: "score",
    label: "Score",
    width: "minmax(60px, 0.5fr)",
    align: "right",
    sortValue: (r) => r.convergence.score,
    render: (r) =>
      r.convergence.score == null ? (
        <Unavailable reason={r.convergence.available ? "no score in this cycle" : r.convergence.reason} />
      ) : (
        <span className="font-mono text-text-secondary">{formatNumber(r.convergence.score, 0)}</span>
      ),
  },
  {
    key: "pcr",
    label: "PCR",
    width: "minmax(64px, 0.55fr)",
    align: "right",
    sortValue: (r) => r.options.pcr,
    title: "ATM put/call open interest ratio, from the ATM watchlist row.",
    render: (r) =>
      !r.options.available ? (
        <Unavailable reason={r.options.reason} />
      ) : r.options.pcr == null ? (
        <Unavailable reason="CE open interest is zero or absent" />
      ) : (
        <span className="font-mono text-text-secondary">{formatNumber(r.options.pcr, 2)}</span>
      ),
  },
  {
    key: "oi",
    label: "ΔOI ce/pe",
    width: "minmax(104px, 0.85fr)",
    align: "right",
    sortValue: (r) =>
      r.options.peOiChangePct == null || r.options.ceOiChangePct == null
        ? null
        : r.options.peOiChangePct - r.options.ceOiChangePct,
    title: "ATM CE / PE open-interest change since the previous session.",
    render: (r) => {
      if (!r.options.available) return <Unavailable reason={r.options.reason} />;
      const { ceOiChangePct: ce, peOiChangePct: pe } = r.options;
      if (ce == null && pe == null) return <Unavailable reason="no OI change reported" />;
      return (
        <span className="font-mono text-text-secondary">
          {ce == null ? "—" : `${ce > 0 ? "+" : ""}${formatNumber(ce, 0)}`}
          <span className="text-text-muted"> / </span>
          {pe == null ? "—" : `${pe > 0 ? "+" : ""}${formatNumber(pe, 0)}`}
        </span>
      );
    },
  },
  {
    key: "risk",
    label: "Risk plan",
    width: "minmax(104px, 0.85fr)",
    sortValue: (r) => r.risk.rr,
    title:
      "R/R only when entry, stop and target-1 all exist. Otherwise the missing fields are named.",
    render: (r) => (
      <RiskGlyph
        available={r.risk.available}
        reason={r.risk.reason}
        planComplete={r.risk.planComplete}
        missing={r.risk.missing}
        rrText={r.risk.rrText}
      />
    ),
  },
  {
    key: "intent",
    label: "Intent",
    width: "minmax(90px, 0.7fr)",
    sortValue: (r) => r.intent.legs,
    title: "Net portfolio intent across the polled books (real book + auction paper book).",
    render: (r) => <IntentGlyph side={r.intent.side} legs={r.intent.legs} lanes={r.intent.lanes} />,
  },
];

export const GRID_TEMPLATE = MATRIX_COLUMNS.map((c) => c.width).join(" ");

/** Nulls always sink, in both directions — a missing cell is never "smallest". */
export function compareRows(a: MatrixRow, b: MatrixRow, key: string, dir: "asc" | "desc"): number {
  const col = MATRIX_COLUMNS.find((c) => c.key === key);
  if (!col?.sortValue) return a.symbol.localeCompare(b.symbol);
  const av = col.sortValue(a);
  const bv = col.sortValue(b);
  if (av == null && bv == null) return a.symbol.localeCompare(b.symbol);
  if (av == null) return 1;
  if (bv == null) return -1;
  const cmp = typeof av === "number" && typeof bv === "number" ? av - bv : String(av).localeCompare(String(bv));
  return dir === "asc" ? cmp : -cmp;
}

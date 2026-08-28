"use client";

/**
 * Capture desk — the read-only surface over the candidate-capture pipeline.
 *
 * The lanes board already answers "are the two runners alive". This answers the
 * harder question: is what they produced worth training on? During the
 * collection window that is the only question that matters, and it is not
 * visible from a row count — a session can look healthy while emitting nothing
 * but abstain rows, which is exactly how the capture lane failed once already.
 *
 * Four tabs, in the order the questions get asked:
 *   Readiness    — is there enough data yet, and what is blocking?
 *   Decidability — can any horizon x contract class clear its own cost?
 *   Explorer     — the individual rows, snapshots and their labelled outcomes.
 *   Models       — what was fitted, and which promotion gate refused it.
 */
import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { clsx } from "clsx";
import { Activity, Boxes, Compass, Database, Gauge, Layers, ScanSearch, Sigma } from "lucide-react";

import { Section, MetricTile, StatusBadge, useUrlChoice } from "@/components/desk-ui";
import { api } from "@/lib/api";

// ── API ────────────────────────────────────────────────────────────────────
interface Readiness {
  sessions: number;
  label_rows: number;
  ok_rows: number;
  underlyings: number;
  first_session: string | null;
  last_session: string | null;
  ready_to_train: boolean;
  blockers: string[];
  min_sessions: number;
}

interface SessionRow {
  session_date: string;
  snapshots: number;
  underlyings: number;
  decision_sets: number;
  eligible: number;
  outcomes: number;
}

interface Stratum {
  horizon_seconds: number;
  expiry_class: string | null;
  moneyness: string | null;
  liquidity_bucket: string | null;
  rows: number;
  avg_breakeven_pct: string | number | null;
  avg_abs_mfe_pct: string | number | null;
  decidable_rows: number;
  avg_gross_pct: string | number | null;
  avg_net_pct: string | number | null;
  never_traded: number;
  decidable_rate: number | null;
  decidable_rate_withheld?: string;
}

interface SnapshotRow {
  time: string;
  underlying: string;
  expiry: string | null;
  expiry_class: string;
  days_to_expiry: number | null;
  option_type: string;
  strike: number | null;
  moneyness: string;
  moneyness_steps: number | null;
  liquidity_bucket: string;
  ltp: number | null;
  bid: number | null;
  ask: number | null;
  spread_pct: number | null;
  volume: number | null;
  oi: number | null;
  iv: number | null;
  lot_size: number | null;
  eligibility_status: string;
  eligibility_reason: string | null;
  missing_fields: string[];
}

interface OutcomeRow {
  time: string;
  underlying: string;
  strike: number | null;
  option_type: string;
  horizon_seconds: number;
  label_status: string;
  label_reason: string | null;
  spot_return_pct: number | null;
  spot_barrier_hit: string | null;
  spot_forward_lag_seconds: number | null;
  spot_window_complete: boolean | null;
  option_gross_return_pct: number | null;
  option_net_return_pct: number | null;
  forward_lag_seconds: number | null;
  forward_sample_count: number | null;
  forward_source: string | null;
  trade_arrived: boolean | null;
  entry_half_spread_measured: boolean | null;
  exit_half_spread_measured: boolean | null;
  cost_pct_of_notional: number | null;
  breakeven_move_pct: number | null;
  economically_decidable: boolean | null;
}

interface GateRow {
  name: string;
  passed: boolean;
  threshold: unknown;
  measured: unknown;
  detail: string;
}

interface ModelRow {
  version_name: string;
  status: string;
  model_family: string;
  horizon_seconds: number;
  underlying_class: string | null;
  expiry_class: string | null;
  target: string;
  train_rows: number | null;
  train_sessions: number | null;
  eval_rows: number | null;
  eval_sessions: number | null;
  eval_start: string | null;
  eval_end: string | null;
  metrics: Record<string, unknown> | null;
  promotion_gates: GateRow[] | null;
  gates_passed: boolean | null;
  promotion_reason: string | null;
  created_at: string;
}

interface CompositionRow {
  underlying: string;
  rows: number;
  eligible: number;
  expiries: number;
}

interface Filters {
  underlyings: string[];
  expiry_classes: string[];
  moneyness: string[];
  liquidity_buckets: string[];
  eligibility_statuses: string[];
  horizons: number[];
  label_statuses: string[];
}

const BASE = "/api/candidate-capture";
const captureApi = {
  readiness: () => api.get<Readiness>(`${BASE}/readiness`).then((r) => r.data),
  sessions: () =>
    api.get<{ sessions: SessionRow[] }>(`${BASE}/sessions`).then((r) => r.data.sessions),
  decidability: () =>
    api.get<{ strata: Stratum[]; strata_with_enough_rows: number; min_stratum_rows: number }>(
      `${BASE}/decidability`,
    ).then((r) => r.data),
  filters: (d?: string) =>
    api.get<Filters>(`${BASE}/filters`, { params: d ? { session_date: d } : {} }).then((r) => r.data),
  snapshots: (params: Record<string, unknown>) =>
    api.get<{
      rows: SnapshotRow[]; count: number; total: number;
      truncated: boolean; composition: CompositionRow[];
    }>(`${BASE}/snapshots`, { params }).then((r) => r.data),
  outcomes: (params: Record<string, unknown>) =>
    api.get<{ rows: OutcomeRow[]; count: number; total: number; truncated: boolean }>(
      `${BASE}/outcomes`, { params },
    ).then((r) => r.data),
  models: () => api.get<{ models: ModelRow[] }>(`${BASE}/models`).then((r) => r.data.models),
  method: () => api.get<MethodCard>(`${BASE}/method`).then((r) => r.data),
  direction: () => api.get<DirectionReport>(`${BASE}/direction`).then((r) => r.data),
};

interface MethodCard {
  features: {
    count: number;
    groups: { name: string; count: number; columns: string[] }[];
    leakage_guard: string;
    normalization: { rule: string; why: string }[];
  };
  targets: {
    concrete: { name: string; asks: string; definition: string; cost_free: boolean; caveat?: string }[];
    cost_dependent: { name: string; asks: string; requires: string }[];
    unmeasurable_is_null: string;
  };
  model: Record<string, unknown>;
  evaluation: Record<string, unknown>;
  ranking: { monotonicity: { expected_log_slope: number; probability_varying_penalty_slope: number; holds: boolean; why: string } };
  limits: string[];
}

interface DirectionReport {
  definition: { min_sigma: number; min_efficiency: number; sigma: string; efficiency: string; note: string };
  by_horizon: {
    horizon_seconds: number; rows: number; avg_abs_sigma: string | number;
    avg_efficiency: string | number; up: number; down: number; confirmed: number;
    confirmed_rate: number | null; up_rate: number | null; down_rate: number | null;
  }[];
}

// ── formatting ─────────────────────────────────────────────────────────────
const pct = (v: number | string | null | undefined, digits = 2) =>
  v === null || v === undefined ? "—" : `${(Number(v) * 100).toFixed(digits)}%`;
const rawPct = (v: number | string | null | undefined, digits = 2) =>
  v === null || v === undefined ? "—" : `${Number(v).toFixed(digits)}%`;
const num = (v: number | null | undefined, digits = 2) =>
  v === null || v === undefined ? "—" : Number(v).toLocaleString("en-IN", { maximumFractionDigits: digits });
const secs = (v: number | null | undefined) => (v === null || v === undefined ? "—" : `${Math.round(v)}s`);
const hhmm = (iso: string) =>
  new Date(iso).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", timeZone: "Asia/Kolkata" });
const horizonLabel = (s: number) => (s % 3600 === 0 ? `${s / 3600}h` : `${s / 60}m`);

const TABS = [
  { key: "readiness", label: "Readiness", icon: Gauge },
  { key: "decidability", label: "Decidability", icon: Activity },
  { key: "explorer", label: "Explorer", icon: ScanSearch },
  { key: "direction", label: "Direction", icon: Compass },
  { key: "models", label: "Models", icon: Boxes },
  { key: "method", label: "Method", icon: Sigma },
] as const;
type TabKey = (typeof TABS)[number]["key"];
const TAB_KEYS = TABS.map((t) => t.key) as readonly TabKey[];

export default function CandidateCaptureDesk() {
  // URL-addressable, matching the parent page's own useUrlTab: a view someone
  // wants to point a colleague at ("look at the decidability table") has to
  // survive being pasted into a message. A second param, so it never collides
  // with the Research page's own `tab`.
  const [tab, setTab] = useUrlChoice<TabKey>("capture", TAB_KEYS, "readiness");

  return (
    <div className="space-y-4">
      <nav className="flex flex-wrap items-center gap-1">
        {TABS.map(({ key, label, icon: Icon }) => (
          <button
            key={key}
            type="button"
            onClick={() => setTab(key)}
            className={clsx(
              "inline-flex items-center gap-1.5 rounded-lg border px-3 py-1.5 text-[12px] font-semibold transition-colors",
              tab === key
                ? "border-accent-blue/55 bg-accent-blue/15 text-accent-blue"
                : "border-bg-border bg-bg-primary/30 text-text-secondary hover:border-bg-active hover:text-text-primary",
            )}
          >
            <Icon size={13} />
            {label}
          </button>
        ))}
      </nav>

      {tab === "readiness" ? <ReadinessTab /> : null}
      {tab === "decidability" ? <DecidabilityTab /> : null}
      {tab === "explorer" ? <ExplorerTab /> : null}
      {tab === "direction" ? <DirectionTab /> : null}
      {tab === "models" ? <ModelsTab /> : null}
      {tab === "method" ? <MethodTab /> : null}
    </div>
  );
}

// ══════════════════════════════════════════════════════════════════════════
function ReadinessTab() {
  const readiness = useQuery({
    queryKey: ["cc-readiness"],
    queryFn: captureApi.readiness,
    refetchInterval: 60_000,
  });
  const sessions = useQuery({ queryKey: ["cc-sessions"], queryFn: captureApi.sessions });

  const r = readiness.data;

  return (
    <div className="space-y-4">
      <Section
        title="Training readiness"
        icon={<Gauge size={16} />}
        description="Whether enough labelled data exists to justify training a first baseline. This reports readiness — it never trains anything."
      >
        {readiness.isLoading ? <Loading label="Reading readiness…" /> : null}
        {readiness.isError ? <ErrorPanel what="readiness" /> : null}
        {r ? (
          <div className="space-y-4">
            <div className="flex flex-wrap items-center gap-3">
              <StatusBadge
                label={r.ready_to_train ? "READY TO TRAIN" : "COLLECTING"}
                variant={r.ready_to_train ? "success" : "warn"}
              />
              <span className="text-sm text-text-muted">
                {r.first_session ? (
                  <>
                    {r.first_session} → {r.last_session}
                  </>
                ) : (
                  "No sessions captured yet"
                )}
              </span>
            </div>

            <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
              <MetricTile label="Sessions" value={`${r.sessions} / ${r.min_sessions}`} />
              <MetricTile label="Label rows" value={num(r.label_rows, 0)} />
              <MetricTile label="Successfully labelled" value={num(r.ok_rows, 0)} />
              <MetricTile label="Underlyings" value={num(r.underlyings, 0)} />
            </div>

            {r.blockers.length > 0 ? (
              <div className="rounded-xl border border-accent-amber/30 bg-accent-amber/8 p-4">
                <p className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-accent-amber">
                  Blocking a first baseline
                </p>
                <ul className="space-y-1 text-sm text-text-secondary">
                  {r.blockers.map((b) => (
                    <li key={b}>· {b}</li>
                  ))}
                </ul>
              </div>
            ) : null}
          </div>
        ) : null}
      </Section>

      <Section
        title="Captured sessions"
        icon={<Database size={16} />}
        description="Eligible counts matter more than row counts: a session can be full of abstain rows and still look busy."
      >
        {sessions.isLoading ? <Loading label="Reading sessions…" /> : null}
        {sessions.data?.length === 0 ? (
          <Empty label="No sessions captured yet. Enable CANDIDATE_CAPTURE_ENABLED and let one session run." />
        ) : null}
        {sessions.data && sessions.data.length > 0 ? (
          <Scroll>
            <table className="w-full text-sm">
              <THead cols={["Session", "Decision sets", "Snapshots", "Eligible", "Underlyings", "Outcomes"]} />
              <tbody>
                {sessions.data.map((s) => (
                  <tr key={s.session_date} className="border-b border-bg-border/40 last:border-0">
                    <Td mono>{s.session_date}</Td>
                    <Td right>{num(s.decision_sets, 0)}</Td>
                    <Td right>{num(s.snapshots, 0)}</Td>
                    <Td right>
                      <span className={s.eligible === 0 ? "text-accent-red" : "text-accent-green"}>
                        {num(s.eligible, 0)}
                      </span>
                    </Td>
                    <Td right>{num(s.underlyings, 0)}</Td>
                    <Td right>{num(s.outcomes, 0)}</Td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Scroll>
        ) : null}
      </Section>
    </div>
  );
}

// ══════════════════════════════════════════════════════════════════════════
function DecidabilityTab() {
  const q = useQuery({ queryKey: ["cc-decidability"], queryFn: captureApi.decidability });
  const strata = q.data?.strata ?? [];

  return (
    <Section
      title="Economic decidability"
      icon={<Activity size={16} />}
      description={
        <>
          The fraction of rows whose own best excursion could clear their own round-trip cost. A stratum near zero
          means the <em>horizon</em> cannot decide anything for that contract class — not that the signal is weak.
          Rates over fewer than {q.data?.min_stratum_rows ?? 30} rows are withheld rather than quoted.
        </>
      }
    >
      {q.isLoading ? <Loading label="Computing decidability…" /> : null}
      {q.isError ? <ErrorPanel what="decidability" /> : null}
      {q.data && strata.length === 0 ? (
        <Empty label="No labelled outcomes yet. Decidability needs candidate_outcomes rows." />
      ) : null}
      {strata.length > 0 ? (
        <Scroll>
          <table className="w-full text-sm">
            <THead
              cols={[
                "Horizon",
                "Expiry",
                "Moneyness",
                "Liquidity",
                "Rows",
                "Breakeven",
                "Avg |MFE|",
                "Decidable",
                "Avg net",
                "Never traded",
              ]}
            />
            <tbody>
              {strata.map((s, i) => {
                const withheld = s.decidable_rate === null;
                return (
                  <tr key={i} className="border-b border-bg-border/40 last:border-0">
                    <Td mono>{horizonLabel(s.horizon_seconds)}</Td>
                    <Td>{s.expiry_class ?? "—"}</Td>
                    <Td>{s.moneyness ?? "—"}</Td>
                    <Td>{s.liquidity_bucket ?? "—"}</Td>
                    <Td right>{num(s.rows, 0)}</Td>
                    <Td right>{rawPct(s.avg_breakeven_pct)}</Td>
                    <Td right>{rawPct(s.avg_abs_mfe_pct)}</Td>
                    <Td right>
                      {withheld ? (
                        <span className="text-text-muted" title={s.decidable_rate_withheld}>
                          n&lt;{30}
                        </span>
                      ) : (
                        <span
                          className={clsx(
                            (s.decidable_rate ?? 0) >= 0.3
                              ? "text-accent-green"
                              : (s.decidable_rate ?? 0) > 0
                                ? "text-accent-amber"
                                : "text-accent-red",
                          )}
                        >
                          {pct(s.decidable_rate, 1)}
                        </span>
                      )}
                    </Td>
                    <Td right>{rawPct(s.avg_net_pct)}</Td>
                    <Td right>{num(s.never_traded, 0)}</Td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </Scroll>
      ) : null}
    </Section>
  );
}

// ══════════════════════════════════════════════════════════════════════════
function ExplorerTab() {
  const [view, setView] = useUrlChoice<"snapshots" | "outcomes">(
    "view", ["snapshots", "outcomes"] as const, "snapshots",
  );
  const sessions = useQuery({ queryKey: ["cc-sessions"], queryFn: captureApi.sessions });
  const [sessionDate, setSessionDate] = useState<string | null>(null);
  const active = sessionDate ?? sessions.data?.[0]?.session_date ?? null;

  const filters = useQuery({
    queryKey: ["cc-filters", active],
    queryFn: () => captureApi.filters(active ?? undefined),
    enabled: Boolean(active),
  });

  const [underlying, setUnderlying] = useState("");
  const [expiryClass, setExpiryClass] = useState("");
  const [moneyness, setMoneyness] = useState("");
  const [horizon, setHorizon] = useState("");

  const snapParams = useMemo(
    () => ({
      session_date: active,
      ...(underlying ? { underlying } : {}),
      ...(expiryClass ? { expiry_class: expiryClass } : {}),
      ...(moneyness ? { moneyness } : {}),
      limit: 300,
    }),
    [active, underlying, expiryClass, moneyness],
  );
  const outParams = useMemo(
    () => ({
      session_date: active,
      ...(underlying ? { underlying } : {}),
      ...(horizon ? { horizon_seconds: Number(horizon) } : {}),
      limit: 300,
    }),
    [active, underlying, horizon],
  );

  const snaps = useQuery({
    queryKey: ["cc-snaps", snapParams],
    queryFn: () => captureApi.snapshots(snapParams),
    enabled: Boolean(active) && view === "snapshots",
  });
  const outs = useQuery({
    queryKey: ["cc-outs", outParams],
    queryFn: () => captureApi.outcomes(outParams),
    enabled: Boolean(active) && view === "outcomes",
  });

  if (!sessions.isLoading && (sessions.data?.length ?? 0) === 0) {
    return (
      <Section title="Explorer" icon={<Layers size={16} />}>
        <Empty label="Nothing captured yet. The explorer fills in once a session has run." />
      </Section>
    );
  }

  return (
    <Section
      title="Explorer"
      icon={<Layers size={16} />}
      description="Individual candidates and their labelled outcomes. Rejected contracts are present with their reason — they were recorded, not dropped."
    >
      <div className="mb-3 flex flex-wrap items-end gap-2">
        <Field label="Session">
          <Select value={active ?? ""} onChange={setSessionDate}>
            {(sessions.data ?? []).map((s) => (
              <option key={s.session_date} value={s.session_date}>
                {s.session_date}
              </option>
            ))}
          </Select>
        </Field>
        <Field label="Underlying">
          <Select value={underlying} onChange={setUnderlying} anyLabel="All">
            {(filters.data?.underlyings ?? []).map((u) => (
              <option key={u} value={u}>
                {u}
              </option>
            ))}
          </Select>
        </Field>
        {view === "snapshots" ? (
          <>
            <Field label="Expiry class">
              <Select value={expiryClass} onChange={setExpiryClass} anyLabel="All">
                {(filters.data?.expiry_classes ?? []).map((v) => (
                  <option key={v} value={v}>
                    {v}
                  </option>
                ))}
              </Select>
            </Field>
            <Field label="Moneyness">
              <Select value={moneyness} onChange={setMoneyness} anyLabel="All">
                {(filters.data?.moneyness ?? []).map((v) => (
                  <option key={v} value={v}>
                    {v}
                  </option>
                ))}
              </Select>
            </Field>
          </>
        ) : (
          <Field label="Horizon">
            <Select value={horizon} onChange={setHorizon} anyLabel="All">
              {(filters.data?.horizons ?? []).map((h) => (
                <option key={h} value={String(h)}>
                  {horizonLabel(h)}
                </option>
              ))}
            </Select>
          </Field>
        )}
        <div className="ml-auto flex gap-1">
          {(["snapshots", "outcomes"] as const).map((v) => (
            <button
              key={v}
              type="button"
              onClick={() => setView(v)}
              className={clsx(
                "rounded-lg border px-3 py-1.5 text-[12px] font-semibold capitalize transition-colors",
                view === v
                  ? "border-accent-blue/55 bg-accent-blue/15 text-accent-blue"
                  : "border-bg-border bg-bg-primary/30 text-text-secondary hover:text-text-primary",
              )}
            >
              {v}
            </button>
          ))}
        </div>
      </div>

      {view === "snapshots" && snaps.data ? (
        <ViewSummary
          composition={snaps.data.composition}
          shown={snaps.data.count}
          total={snaps.data.total}
          truncated={snaps.data.truncated}
          active={underlying}
          onPick={setUnderlying}
        />
      ) : null}
      {view === "outcomes" && outs.data ? (
        <ViewSummary
          shown={outs.data.count}
          total={outs.data.total}
          truncated={outs.data.truncated}
          active={underlying}
          onPick={setUnderlying}
        />
      ) : null}

      {view === "snapshots" ? (
        snaps.isLoading ? (
          <Loading label="Reading snapshots…" />
        ) : (
          <Scroll>
            <table className="w-full text-sm">
              <THead
                cols={[
                  "Time", "Under", "Type", "Strike", "Moneyness", "Liquidity",
                  "LTP", "Bid", "Ask", "Spread", "OI", "IV", "Status",
                ]}
              />
              <tbody>
                {(snaps.data?.rows ?? []).map((r, i) => (
                  <tr key={i} className="border-b border-bg-border/40 last:border-0">
                    <Td mono>{hhmm(r.time)}</Td>
                    <Td>{r.underlying}</Td>
                    <Td>
                      <span className={r.option_type === "NO_TRADE" ? "text-text-muted italic" : ""}>
                        {r.option_type}
                      </span>
                    </Td>
                    <Td right>{r.strike === null ? "—" : num(r.strike, 1)}</Td>
                    <Td>{r.moneyness}</Td>
                    <Td>{r.liquidity_bucket}</Td>
                    <Td right>{num(r.ltp)}</Td>
                    <Td right>{num(r.bid)}</Td>
                    <Td right>{num(r.ask)}</Td>
                    <Td right>{pct(r.spread_pct)}</Td>
                    <Td right>{num(r.oi, 0)}</Td>
                    <Td right>{num(r.iv)}</Td>
                    <Td>
                      <EligibilityChip status={r.eligibility_status} reason={r.eligibility_reason} />
                    </Td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Scroll>
        )
      ) : outs.isLoading ? (
        <Loading label="Reading outcomes…" />
      ) : (
        <Scroll>
          <table className="w-full text-sm">
            <THead
              cols={[
                "Time", "Type", "Strike", "H", "Status", "Spot ret", "Barrier",
                "Opt gross", "Opt net", "Cost", "Breakeven", "Lag", "Samples", "Traded", "Decidable",
              ]}
            />
            <tbody>
              {(outs.data?.rows ?? []).map((r, i) => (
                <tr key={i} className="border-b border-bg-border/40 last:border-0">
                  <Td mono>{hhmm(r.time)}</Td>
                  <Td>{r.option_type}</Td>
                  <Td right>{r.strike === null ? "—" : num(r.strike, 1)}</Td>
                  <Td mono>{horizonLabel(r.horizon_seconds)}</Td>
                  <Td>
                    <LabelChip status={r.label_status} reason={r.label_reason} />
                  </Td>
                  <Td right>{pct(r.spot_return_pct, 3)}</Td>
                  <Td>{r.spot_barrier_hit ?? "—"}</Td>
                  <Td right>{pct(r.option_gross_return_pct)}</Td>
                  <Td right>
                    <Signed v={r.option_net_return_pct} />
                  </Td>
                  <Td right>{pct(r.cost_pct_of_notional)}</Td>
                  <Td right>{pct(r.breakeven_move_pct)}</Td>
                  <Td right title={`spot lag ${secs(r.spot_forward_lag_seconds)}`}>
                    {secs(r.forward_lag_seconds)}
                  </Td>
                  <Td right>{num(r.forward_sample_count, 0)}</Td>
                  <Td>
                    {r.trade_arrived === null ? (
                      "—"
                    ) : r.trade_arrived ? (
                      <span className="text-accent-green">yes</span>
                    ) : (
                      <span className="text-accent-red" title="No trade arrived — a zero return here is not a market observation">
                        no
                      </span>
                    )}
                  </Td>
                  <Td>
                    {r.economically_decidable === null ? (
                      "—"
                    ) : r.economically_decidable ? (
                      <span className="text-accent-green">yes</span>
                    ) : (
                      <span className="text-text-muted">no</span>
                    )}
                  </Td>
                </tr>
              ))}
            </tbody>
          </table>
        </Scroll>
      )}
    </Section>
  );
}

// ══════════════════════════════════════════════════════════════════════════
function DirectionTab() {
  const q = useQuery({ queryKey: ["cc-direction"], queryFn: captureApi.direction });
  const d = q.data;

  return (
    <Section
      title="Confirmed direction with strength"
      icon={<Compass size={16} />}
      description={
        d ? (
          <>
            A move counts only when it clears BOTH bars: strength ≥{" "}
            <strong>{d.definition.min_sigma}σ</strong> of its own horizon volatility, and efficiency ≥{" "}
            <strong>{d.definition.min_efficiency}</strong>, where efficiency is |return| ÷ (MFE − MAE).
            Chop that happens to close positive is <em>unconfirmed</em>; so is a clean move too small for
            its own volatility.
          </>
        ) : null
      }
    >
      {q.isLoading ? <Loading label="Reading direction outcomes…" /> : null}
      {q.isError ? <ErrorPanel what="direction" /> : null}
      {d && d.by_horizon.length === 0 ? (
        <Empty label="No spot outcomes yet — direction needs a labelled spot leg." />
      ) : null}
      {d && d.by_horizon.length > 0 ? (
        <Scroll>
          <table className="w-full text-sm">
            <THead cols={["Horizon", "Rows", "Avg |σ|", "Avg efficiency", "Up", "Down", "Confirmed", "Rate"]} />
            <tbody>
              {d.by_horizon.map((r) => {
                const rate = r.confirmed_rate ?? 0;
                const degenerate = rate < 0.02 || rate > 0.9;
                return (
                  <tr key={r.horizon_seconds} className="border-b border-bg-border/40 last:border-0">
                    <Td mono>{horizonLabel(r.horizon_seconds)}</Td>
                    <Td right>{num(r.rows, 0)}</Td>
                    <Td right>{num(Number(r.avg_abs_sigma), 3)}</Td>
                    <Td right>{num(Number(r.avg_efficiency), 3)}</Td>
                    <Td right>{num(r.up, 0)}</Td>
                    <Td right>{num(r.down, 0)}</Td>
                    <Td right>{num(r.confirmed, 0)}</Td>
                    <Td right>
                      <span className={clsx(degenerate ? "text-accent-red" : "text-accent-green")}>
                        {pct(rate, 1)}
                      </span>
                    </Td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </Scroll>
      ) : null}
      <p className="mt-2 text-[12px] text-text-muted">
        A rate near 0% or 100% is shown in red: the label is degenerate there and nothing can be learned
        from it. A collapsing rate as the horizon grows is a volatility-scaling problem, not a market fact.
      </p>
    </Section>
  );
}

function MethodTab() {
  const q = useQuery({ queryKey: ["cc-method"], queryFn: captureApi.method });
  const m = q.data;
  if (q.isLoading) return <Loading label="Reading method card…" />;
  if (q.isError || !m) return <ErrorPanel what="method" />;

  return (
    <div className="space-y-4">
      <Section
        title={`Inputs — ${m.features.count} features`}
        icon={<Sigma size={16} />}
        description={m.features.leakage_guard}
      >
        <div className="grid gap-2 md:grid-cols-2">
          {m.features.groups.map((g) => (
            <div key={g.name} className="rounded-lg border border-bg-border bg-bg-primary/30 p-3">
              <p className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-accent-blue">
                {g.name.replace(/_/g, " ")} · {g.count}
              </p>
              <p className="font-mono text-[11px] leading-relaxed text-text-muted">
                {g.columns.join(", ")}
              </p>
            </div>
          ))}
        </div>
      </Section>

      <Section title="How inputs are coded" icon={<Sigma size={16} />}>
        <ul className="space-y-2">
          {m.features.normalization.map((n) => (
            <li key={n.rule} className="text-sm">
              <span className="font-semibold text-text-primary">{n.rule}</span>
              <span className="text-text-muted"> — {n.why}</span>
            </li>
          ))}
        </ul>
      </Section>

      <Section
        title="Labels"
        icon={<Compass size={16} />}
        description={m.targets.unmeasurable_is_null}
      >
        <div className="space-y-2">
          {m.targets.concrete.map((t) => (
            <div key={t.name} className="rounded-lg border border-bg-border bg-bg-primary/30 p-3">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-mono text-[12px] text-text-primary">{t.name}</span>
                <span className="rounded bg-accent-green/12 px-1.5 py-0.5 text-[11px] text-accent-green">
                  cost-free
                </span>
              </div>
              <p className="mt-1 text-sm text-text-secondary">{t.asks}</p>
              <p className="mt-0.5 font-mono text-[11px] text-text-muted">{t.definition}</p>
              {t.caveat ? (
                <p className="mt-1 text-[12px] text-accent-amber">⚠ {t.caveat}</p>
              ) : null}
            </div>
          ))}
          {m.targets.cost_dependent.map((t) => (
            <div key={t.name} className="rounded-lg border border-accent-amber/30 bg-accent-amber/8 p-3">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-mono text-[12px] text-text-primary">{t.name}</span>
                <span className="rounded bg-accent-amber/15 px-1.5 py-0.5 text-[11px] text-accent-amber">
                  needs measured spread
                </span>
              </div>
              <p className="mt-1 text-sm text-text-secondary">{t.asks}</p>
              <p className="mt-0.5 text-[12px] text-text-muted">{t.requires}</p>
            </div>
          ))}
        </div>
      </Section>

      <Section title="Model, evaluation and ranking" icon={<Activity size={16} />}>
        <div className="grid gap-3 md:grid-cols-2">
          <KeyValues title="Model" data={m.model} />
          <KeyValues title="Evaluation" data={m.evaluation} />
        </div>
        <div
          className={clsx(
            "mt-3 rounded-lg border p-3 text-sm",
            m.ranking.monotonicity.holds
              ? "border-accent-green/30 bg-accent-green/8"
              : "border-accent-red/30 bg-accent-red/8",
          )}
        >
          <p className="text-[11px] font-semibold uppercase tracking-wider text-text-muted">
            Ranking monotonicity
          </p>
          <p className="mt-1 text-text-secondary">
            signal slope{" "}
            <span className="font-mono text-text-primary">
              {m.ranking.monotonicity.expected_log_slope}
            </span>{" "}
            vs penalty slope{" "}
            <span className="font-mono text-text-primary">
              {m.ranking.monotonicity.probability_varying_penalty_slope}
            </span>{" "}
            → {m.ranking.monotonicity.holds ? "utility rises with probability" : "INVERTED"}
          </p>
          <p className="mt-1 text-[12px] text-text-muted">{m.ranking.monotonicity.why}</p>
        </div>
      </Section>

      <Section title="Known limits" icon={<Layers size={16} />}>
        <ul className="space-y-2">
          {m.limits.map((l, i) => (
            <li key={i} className="text-sm text-text-secondary">· {l}</li>
          ))}
        </ul>
      </Section>
    </div>
  );
}

function KeyValues({ title, data }: { title: string; data: Record<string, unknown> }) {
  return (
    <div className="rounded-lg border border-bg-border bg-bg-primary/30 p-3">
      <p className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-accent-blue">{title}</p>
      <dl className="space-y-1">
        {Object.entries(data).map(([k, v]) => (
          <div key={k} className="flex gap-2 text-[12px]">
            <dt className="shrink-0 font-mono text-text-muted">{k}</dt>
            <dd className="text-text-secondary">{String(v)}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

// ══════════════════════════════════════════════════════════════════════════
function ModelsTab() {
  const q = useQuery({ queryKey: ["cc-models"], queryFn: captureApi.models });
  const models = q.data ?? [];

  return (
    <Section
      title="Model versions"
      icon={<Boxes size={16} />}
      description={
        <>
          Every fitted specialist, promoted or refused. Refusals are kept and show which gate failed and by how much —
          a model refused on one threshold stays re-judgeable if that threshold later moves. Evaluation is
          walk-forward with <strong>session-clustered</strong> standard errors.
        </>
      }
    >
      {q.isLoading ? <Loading label="Reading model versions…" /> : null}
      {q.isError ? <ErrorPanel what="models" /> : null}
      {q.data && models.length === 0 ? (
        <Empty label="No models fitted yet. Training refuses until enough labelled sessions exist — that is the expected state during collection." />
      ) : null}
      <div className="space-y-3">
        {models.map((m) => (
          <ModelCard key={m.version_name} model={m} />
        ))}
      </div>
    </Section>
  );
}

function ModelCard({ model }: { model: ModelRow }) {
  const promoted = model.status === "champion";
  const gates = model.promotion_gates ?? [];
  const failed = gates.filter((g) => !g.passed);
  const clustered = (model.metrics?.clustered ?? {}) as Record<string, number | null>;

  return (
    <div className="rounded-xl border border-bg-border bg-bg-primary/30 p-4">
      <div className="mb-3 flex flex-wrap items-center gap-3">
        <StatusBadge
          label={model.status.toUpperCase()}
          variant={promoted ? "success" : model.status === "refused" ? "warn" : "neutral"}
        />
        <span className="font-mono text-[12px] text-text-primary">{model.version_name}</span>
        <span className="text-[12px] text-text-muted">
          {horizonLabel(model.horizon_seconds)} · {model.expiry_class ?? "any expiry"} ·{" "}
          {model.underlying_class ?? "any class"} · {model.target}
        </span>
      </div>

      <div className="mb-3 grid grid-cols-2 gap-3 md:grid-cols-4">
        <MetricTile label="Eval sessions" value={num(model.eval_sessions, 0)} />
        <MetricTile label="Eval rows" value={num(model.eval_rows, 0)} />
        <MetricTile label="Mean net (clustered)" value={pct(clustered.mean as number | null)} />
        <MetricTile label="t-stat" value={num(clustered.t_stat as number | null)} />
      </div>

      <div className="flex flex-wrap gap-1.5">
        {gates.map((g) => (
          <span
            key={g.name}
            title={`${g.detail}\nmeasured: ${String(g.measured)} · threshold: ${String(g.threshold)}`}
            className={clsx(
              "rounded px-1.5 py-0.5 text-[11px] font-medium",
              g.passed ? "bg-accent-green/12 text-accent-green" : "bg-accent-red/12 text-accent-red",
            )}
          >
            {g.passed ? "✓" : "✗"} {g.name.replace(/_/g, " ")}
          </span>
        ))}
      </div>

      {failed.length > 0 ? (
        <p className="mt-2 text-[12px] text-text-muted">
          Refused on {failed.length} gate{failed.length === 1 ? "" : "s"}. This is the normal outcome until enough
          decidable data exists.
        </p>
      ) : null}
    </div>
  );
}

/**
 * What is actually in this view.
 *
 * The listing is ordered by underlying, so the first screens are all one name
 * and a reader genuinely cannot tell whether the others are present — the
 * first version of this desk looked like it had captured BANKNIFTY only, when
 * all three were there. These chips answer that before any scrolling, and
 * double as filters.
 *
 * The row count is stated as "shown of total" because the query is capped. A
 * silent cap presents a page as the whole result, which is the same quiet
 * dishonesty the pipeline refuses everywhere else.
 */
function ViewSummary({
  composition,
  shown,
  total,
  truncated,
  active,
  onPick,
}: {
  composition?: CompositionRow[];
  shown: number;
  total: number;
  truncated: boolean;
  active: string;
  onPick: (v: string) => void;
}) {
  return (
    <div className="mb-3 flex flex-wrap items-center gap-2">
      {composition?.map((c) => (
        <button
          key={c.underlying}
          type="button"
          onClick={() => onPick(active === c.underlying ? "" : c.underlying)}
          title={`${c.eligible} eligible · ${c.expiries} expir${c.expiries === 1 ? "y" : "ies"}`}
          className={clsx(
            "rounded-lg border px-2.5 py-1 text-[12px] font-semibold transition-colors",
            active === c.underlying
              ? "border-accent-blue/55 bg-accent-blue/15 text-accent-blue"
              : "border-bg-border bg-bg-primary/30 text-text-secondary hover:border-bg-active hover:text-text-primary",
          )}
        >
          {c.underlying}
          <span className="ml-1.5 font-mono text-[11px] text-text-muted">{num(c.rows, 0)}</span>
        </button>
      ))}
      <span className="ml-auto text-[12px] text-text-muted">
        showing <span className="font-mono text-text-secondary">{num(shown, 0)}</span> of{" "}
        <span className="font-mono text-text-secondary">{num(total, 0)}</span>
        {truncated ? (
          <span className="ml-1 text-accent-amber">· capped, narrow a filter to see the rest</span>
        ) : null}
      </span>
    </div>
  );
}

// ── small shared pieces ────────────────────────────────────────────────────
function Loading({ label }: { label: string }) {
  return (
    <div className="rounded-xl border border-dashed border-bg-border/60 px-4 py-10 text-center text-sm text-text-muted">
      <span className="animate-pulse">{label}</span>
    </div>
  );
}

function Empty({ label }: { label: string }) {
  return (
    <div className="rounded-xl border border-accent-amber/30 bg-accent-amber/8 p-4 text-sm text-text-secondary">
      {label}
    </div>
  );
}

function ErrorPanel({ what }: { what: string }) {
  return (
    <div className="rounded-xl border border-accent-red/30 bg-accent-red/8 p-4 text-sm text-text-secondary">
      Could not reach the {what} endpoint. The capture pipeline may not be deployed on this backend yet.
    </div>
  );
}

function Scroll({ children }: { children: React.ReactNode }) {
  return <div className="overflow-x-auto">{children}</div>;
}

function THead({ cols }: { cols: string[] }) {
  return (
    <thead>
      <tr className="border-b border-bg-border/60">
        {cols.map((c) => (
          <th
            key={c}
            className="whitespace-nowrap px-2 py-1.5 text-left text-[10.5px] font-semibold uppercase tracking-wider text-text-muted"
          >
            {c}
          </th>
        ))}
      </tr>
    </thead>
  );
}

function Td({
  children,
  right,
  mono,
  title,
}: {
  children: React.ReactNode;
  right?: boolean;
  mono?: boolean;
  title?: string;
}) {
  return (
    <td
      title={title}
      className={clsx(
        "whitespace-nowrap px-2 py-1.5 text-text-secondary",
        right && "text-right tabular-nums",
        mono && "font-mono text-[12px]",
      )}
    >
      {children}
    </td>
  );
}

function Signed({ v }: { v: number | null }) {
  if (v === null || v === undefined) return <>—</>;
  return <span className={v >= 0 ? "text-accent-green" : "text-accent-red"}>{pct(v)}</span>;
}

function EligibilityChip({ status, reason }: { status: string; reason: string | null }) {
  const ok = status === "eligible";
  return (
    <span
      title={reason ?? undefined}
      className={clsx(
        "rounded px-1.5 py-0.5 text-[11px] font-medium",
        ok ? "bg-accent-green/12 text-accent-green" : "bg-accent-amber/12 text-accent-amber",
      )}
    >
      {status}
    </span>
  );
}

function LabelChip({ status, reason }: { status: string; reason: string | null }) {
  const tone =
    status === "ok"
      ? "bg-accent-green/12 text-accent-green"
      : status === "no_trade"
        ? "bg-bg-active/60 text-text-muted"
        : "bg-accent-amber/12 text-accent-amber";
  return (
    <span title={reason ?? undefined} className={clsx("rounded px-1.5 py-0.5 text-[11px] font-medium", tone)}>
      {status.replace("unlabellable_", "")}
    </span>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-[10.5px] font-semibold uppercase tracking-wider text-text-muted">{label}</span>
      {children}
    </label>
  );
}

function Select({
  value,
  onChange,
  children,
  anyLabel,
}: {
  value: string;
  onChange: (v: string) => void;
  children: React.ReactNode;
  anyLabel?: string;
}) {
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className="rounded-lg border border-bg-border bg-bg-primary/40 px-2 py-1.5 text-[12px] text-text-primary focus:border-accent-blue focus:outline-none"
    >
      {anyLabel ? <option value="">{anyLabel}</option> : null}
      {children}
    </select>
  );
}

"use client";

/**
 * Index directional swing desk — long premium, NIFTY/BANKNIFTY/SENSEX,
 * 1–5 trading sessions.
 *
 * ─── Why this desk leads with refusals ──────────────────────────────────────
 *
 * This lane will decline on most sessions, and that is the designed output: it
 * has four sessions of wide chain history and no direction factor with measured
 * skill, so standing aside is the honest answer rather than a malfunction.
 *
 * But an empty positions table renders identically whether the lane reasoned
 * its way to no-trade or whether its feed died — and those two have OPPOSITE
 * meanings. So the landing tab is the FUNNEL, not the book, and every panel
 * distinguishes "nothing happened" from "nothing was recorded".
 *
 *   Funnel       where candidates die, gate by gate, per index. The tab that
 *                answers "did it even look today, and what stopped it?".
 *   Factors      every factor's value at the last decision — including the
 *                ones carrying shadow weight that did NOT act. A shadow factor
 *                is still journalled so it can eventually be graded against
 *                this lane's own outcomes instead of re-fitted on the history
 *                that suggested it.
 *   Surface      vol-substrate health: fit quality, staleness, and what the
 *                IV solver refused. The quarantine count is not a footnote —
 *                a day it jumps is a data incident.
 *   Book         positions, marked each pass off the chain or, when the
 *                contract has not printed, off the fitted surface. Unrealized
 *                P&L is genuinely marked here, so it is shown rather than
 *                declared absent.
 *   Attribution  which greek actually paid. Without it a correct directional
 *                call cannot be told from a wrong one rescued by a vol
 *                expansion, and factor selection runs on noise.
 */
import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { Activity, Filter, Layers, BookOpen, PieChart } from "lucide-react";

import {
  DeskShell,
  MetricTile,
  Section,
  REFRESH_MS,
  formatMoney,
  formatSignedMoney,
  formatNumber,
  formatPct,
  formatIST,
  useUrlTab,
  type DeskTab,
} from "@/components/desk-ui";
import {
  getIndexSwingSummary,
  getIndexSwingFunnel,
  getIndexSwingFactors,
  getIndexSwingSurface,
  getIndexSwingPositions,
  getIndexSwingAttribution,
} from "@/lib/api";

const TABS: DeskTab[] = [
  { key: "funnel", label: "Funnel", icon: Filter },
  { key: "factors", label: "Factors", icon: Layers },
  { key: "surface", label: "Surface", icon: Activity },
  { key: "book", label: "Book", icon: BookOpen },
  { key: "attribution", label: "Attribution", icon: PieChart },
];

const num = (v: unknown, digits = 2) =>
  v === null || v === undefined ? "—" : formatNumber(Number(v), digits);

/** A value that is genuinely absent must never render as a plausible zero. */
const absent = <span className="text-text-muted">—</span>;

function Empty({ title, detail }: { title: string; detail: string }) {
  return (
    <div className="rounded-2xl border border-dashed border-bg-border px-5 py-8 text-center">
      <div className="text-sm font-medium text-text-primary">{title}</div>
      <div className="mx-auto mt-1 max-w-xl text-[12px] leading-relaxed text-text-muted">{detail}</div>
    </div>
  );
}

function Bar({ value, max, tone }: { value: number; max: number; tone: string }) {
  const pct = max > 0 ? Math.max(2, Math.round((value / max) * 100)) : 0;
  return (
    <div className="h-2 w-full rounded-full bg-bg-secondary/50">
      <div className={`h-2 rounded-full ${tone}`} style={{ width: `${pct}%` }} />
    </div>
  );
}

export default function IndexSwingDesk() {
  const [activeTab, setActiveTab] = useUrlTab("funnel");

  const summary = useQuery({
    queryKey: ["index-swing", "summary"],
    queryFn: (): Promise<any> => getIndexSwingSummary().then((r) => r.data),
    refetchInterval: REFRESH_MS.summary,
  });
  const funnel = useQuery({
    queryKey: ["index-swing", "funnel"],
    queryFn: (): Promise<any> => getIndexSwingFunnel().then((r) => r.data),
    refetchInterval: REFRESH_MS.summary,
    enabled: activeTab === "funnel",
  });
  const factors = useQuery({
    queryKey: ["index-swing", "factors"],
    queryFn: (): Promise<any> => getIndexSwingFactors().then((r) => r.data),
    refetchInterval: REFRESH_MS.summary,
    enabled: activeTab === "factors",
  });
  const surface = useQuery({
    queryKey: ["index-swing", "surface"],
    queryFn: (): Promise<any> => getIndexSwingSurface().then((r) => r.data),
    refetchInterval: REFRESH_MS.summary,
    enabled: activeTab === "surface",
  });
  const positions = useQuery({
    queryKey: ["index-swing", "positions"],
    queryFn: (): Promise<any> => getIndexSwingPositions().then((r) => r.data),
    refetchInterval: REFRESH_MS.summary,
    enabled: activeTab === "book",
  });
  const attribution = useQuery({
    queryKey: ["index-swing", "attribution"],
    queryFn: (): Promise<any> => getIndexSwingAttribution().then((r) => r.data),
    refetchInterval: REFRESH_MS.summary,
    enabled: activeTab === "attribution",
  });

  const book = summary.data?.book;
  const gates = summary.data?.gates;
  const maxStage = useMemo(
    () => Math.max(1, ...((funnel.data?.stages ?? []).map((s: any) => s.count) as number[])),
    [funnel.data],
  );

  return (
    <DeskShell
      title="Index Swing · long premium"
      description={
        summary.data
          ? `${summary.data.universe.join(" · ")} — ${summary.data.horizon_label}, long premium only. Declines by design on most sessions; the funnel says why.`
          : "NIFTY · BANKNIFTY · SENSEX — long premium, 1–5 trading sessions."
      }
      asOf={summary.data?.latest_decision_session ?? null}
      asOfLabel="Last decision"
      asOfStaleSeconds={36 * 3600}
      asOfCriticalSeconds={96 * 3600}
      paperMode
      isFetching={summary.isFetching}
      tabs={TABS}
      activeTab={activeTab}
      onTabChange={setActiveTab}
      beforeTabs={
        <div className="grid grid-cols-2 gap-3 md:grid-cols-6">
          <MetricTile label="Equity" value={book ? formatMoney(book.equity) : "—"} />
          <MetricTile
            label="Realised"
            value={book ? formatSignedMoney(book.realizedPnl) : "—"}
            color={book && book.realizedPnl < 0 ? "text-rose-400" : "text-emerald-400"}
          />
          <MetricTile
            label="Unrealised"
            value={book ? formatSignedMoney(book.unrealizedPnl) : "—"}
            detail="marked each pass"
          />
          <MetricTile label="Open" value={book ? String(book.openPositions) : "—"} />
          <MetricTile
            label="Closed"
            value={book ? String(book.closedPositions) : "—"}
            detail={book?.winRate != null ? `${formatPct(book.winRate)} win` : "no closed trades"}
          />
          <MetricTile label="Costs" value={book ? formatMoney(book.totalCosts) : "—"} />
        </div>
      }
    >
      {/* ── FUNNEL ─────────────────────────────────────────────────────────── */}
      {activeTab === "funnel" && (
        <div className="space-y-4">
          {funnel.data && funnel.data.evaluated === 0 ? (
            <Empty
              title="The lane has not recorded a decision for this session"
              detail="This is not a reasoned no-trade — it means no pass ran, or it ran before the market opened. A lane that declined would still appear below with the gate that stopped it."
            />
          ) : null}

          {funnel.data?.mixes_runs ? (
            <div className="rounded-2xl border border-amber-500/30 bg-amber-500/5 px-4 py-3 text-[12px] text-amber-200/90">
              This session's tally spans {funnel.data.runs.length} runs — the decision journal is
              append-only and survives a replay reset, so historical replays are counted alongside
              the live pass. Counts here overstate live activity.
            </div>
          ) : null}

          <Section
            title="Where candidates die"
            description="Gates in the order a candidate meets them. The tallest bar is the lane's real strategy, whatever the design document says."
          >
            {(funnel.data?.stages ?? []).length === 0 ? (
              <Empty title="No decisions recorded" detail="Nothing to attribute yet." />
            ) : (
              <div className="space-y-3">
                {funnel.data.stages.map((s: any) => (
                  <div key={s.reason_code} className="grid grid-cols-[1fr_auto] items-center gap-3">
                    <div>
                      <div className="flex items-baseline justify-between gap-3">
                        <span
                          className={
                            s.reason_code === "entered"
                              ? "text-[13px] font-medium text-emerald-400"
                              : "text-[13px] text-text-primary"
                          }
                        >
                          {s.label}
                        </span>
                        <span className="font-mono text-[11px] text-text-muted">{s.reason_code}</span>
                      </div>
                      <div className="mt-1.5">
                        <Bar
                          value={s.count}
                          max={maxStage}
                          tone={s.reason_code === "entered" ? "bg-emerald-500/70" : "bg-sky-500/40"}
                        />
                      </div>
                    </div>
                    <div className="w-12 text-right font-mono text-sm text-text-primary">{s.count}</div>
                  </div>
                ))}
              </div>
            )}
          </Section>

          <Section title="Per index" description="Evaluations and entries by underlying.">
            <div className="grid gap-3 md:grid-cols-3">
              {(funnel.data?.by_underlying ?? []).map((u: any) => (
                <div key={u.underlying} className="rounded-2xl border border-bg-border bg-bg-secondary/24 p-4">
                  <div className="flex items-baseline justify-between">
                    <span className="text-sm font-semibold text-text-primary">{u.underlying}</span>
                    <span className="font-mono text-[11px] text-text-muted">
                      {u.entered} / {u.evaluated}
                    </span>
                  </div>
                  <div className="mt-2 space-y-1">
                    {Object.entries(u.reasons)
                      .sort((a: any, b: any) => b[1] - a[1])
                      .slice(0, 4)
                      .map(([code, n]: any) => (
                        <div key={code} className="flex justify-between text-[11.5px]">
                          <span className="text-text-muted">{code}</span>
                          <span className="font-mono text-text-primary">{n}</span>
                        </div>
                      ))}
                  </div>
                </div>
              ))}
            </div>
          </Section>

          {gates ? (
            <Section title="Gates in force" description="The thresholds the funnel above was measured against.">
              <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
                <MetricTile label="Min direction" value={num(gates.min_direction_score)} size="sm" />
                <MetricTile label="Min agreement" value={`${gates.min_direction_agreement} factors`} size="sm" />
                <MetricTile label="Max breakeven" value={`${num(gates.max_breakeven_ratio)} σ`} size="sm" />
                <MetricTile
                  label="Delta band"
                  value={`${num(gates.delta_band[0])}–${num(gates.delta_band[1])}`}
                  size="sm"
                />
                <MetricTile label="Max concurrent" value={String(gates.max_concurrent_positions)} size="sm" />
              </div>
            </Section>
          ) : null}
        </div>
      )}

      {/* ── FACTORS ────────────────────────────────────────────────────────── */}
      {activeTab === "factors" && (
        <div className="space-y-4">
          {(factors.data?.underlyings ?? []).length === 0 ? (
            <Empty
              title="No factor panel recorded"
              detail="The panel is written on every entry evaluation. Nothing here means no evaluation has run for this session."
            />
          ) : null}
          {(factors.data?.underlyings ?? []).map((u: any) => (
            <Section
              key={u.underlying}
              title={u.underlying}
              description={`Panel at ${formatIST(u.bar_ts)}. Shadow factors carry small weight and are journalled whether or not they act — that is how one earns a real weight later.`}
              rightSlot={
                <div className="flex gap-2 text-[11px]">
                  <span className="rounded-full bg-bg-secondary/50 px-2.5 py-1 font-mono">
                    direction {u.direction_score == null ? "—" : num(u.direction_score, 3)}
                  </span>
                  <span className="rounded-full bg-bg-secondary/50 px-2.5 py-1 font-mono">
                    size {num(u.size_score, 3)}
                  </span>
                </div>
              }
            >
              {(["direction", "size"] as const).map((role) => (
                <div key={role} className="mb-4 last:mb-0">
                  <div className="mb-2 text-[10.5px] uppercase tracking-[0.16em] text-text-muted">
                    {role}
                  </div>
                  <div className="overflow-x-auto">
                    <table className="w-full min-w-[680px] text-[12px]">
                      <thead className="text-[10.5px] uppercase tracking-wider text-text-muted">
                        <tr className="border-b border-bg-border">
                          <th className="py-1.5 text-left font-medium">Factor</th>
                          <th className="py-1.5 text-right font-medium">Raw</th>
                          <th className="py-1.5 text-right font-medium">Score</th>
                          <th className="py-1.5 text-right font-medium">Weight</th>
                          <th className="py-1.5 text-right font-medium">Contribution</th>
                          <th className="py-1.5 text-left font-medium">Confidence</th>
                          <th className="py-1.5 text-left font-medium">State</th>
                        </tr>
                      </thead>
                      <tbody>
                        {(u[role] ?? []).map((f: any) => (
                          <tr key={f.name} className="border-b border-bg-border/40 last:border-0">
                            <td className="py-1.5 font-mono text-text-primary">{f.name}</td>
                            <td className="py-1.5 text-right font-mono">{num(f.value, 4)}</td>
                            <td className="py-1.5 text-right font-mono">{num(f.score, 3)}</td>
                            <td className="py-1.5 text-right font-mono text-text-muted">
                              {num(f.weight, 2)}
                            </td>
                            <td
                              className={`py-1.5 text-right font-mono ${
                                f.contribution == null
                                  ? ""
                                  : f.contribution > 0
                                  ? "text-emerald-400"
                                  : f.contribution < 0
                                  ? "text-rose-400"
                                  : ""
                              }`}
                            >
                              {f.contribution == null ? absent : num(f.contribution, 3)}
                            </td>
                            <td className="py-1.5 text-text-muted">{f.confidence}</td>
                            <td className="py-1.5">
                              {f.status !== "ok" ? (
                                <span className="text-amber-400/90">{f.status}</span>
                              ) : f.acting ? (
                                <span className="text-emerald-400">acting</span>
                              ) : (
                                <span className="text-text-muted">shadow</span>
                              )}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              ))}
            </Section>
          ))}
        </div>
      )}

      {/* ── SURFACE ────────────────────────────────────────────────────────── */}
      {activeTab === "surface" && (
        <div className="space-y-4">
          {surface.data?.status === "no_data" ? (
            <Empty
              title="No fitted surface in this window"
              detail="The vol substrate has not produced a usable fit recently. This is a data state, not a modelling one — check that the substrate runner is enabled and the chain sweep is landing."
            />
          ) : surface.data?.status === "stale" ? (
            <div className="rounded-2xl border border-amber-500/30 bg-amber-500/5 px-4 py-3 text-[12px] text-amber-200/90">
              The newest fitted slice is over a day old. The lane will still mark positions off it,
              but any implied-vol reading below describes an older market.
            </div>
          ) : null}

          <Section
            title="Fitted slices"
            description="One row per index: the front expiry the substrate could actually fit, and how well."
          >
            <div className="overflow-x-auto">
              <table className="w-full min-w-[760px] text-[12px]">
                <thead className="text-[10.5px] uppercase tracking-wider text-text-muted">
                  <tr className="border-b border-bg-border">
                    <th className="py-1.5 text-left font-medium">Index</th>
                    <th className="py-1.5 text-left font-medium">Expiry</th>
                    <th className="py-1.5 text-right font-medium">DTE</th>
                    <th className="py-1.5 text-right font-medium">ATM IV</th>
                    <th className="py-1.5 text-right font-medium">Quotes</th>
                    <th className="py-1.5 text-right font-medium">RMSE (vol)</th>
                    <th className="py-1.5 text-right font-medium">Refused</th>
                    <th className="py-1.5 text-left font-medium">Butterfly</th>
                    <th className="py-1.5 text-right font-medium">Age</th>
                  </tr>
                </thead>
                <tbody>
                  {(surface.data?.slices ?? []).map((s: any) => (
                    <tr key={s.underlying} className="border-b border-bg-border/40 last:border-0">
                      <td className="py-1.5 font-medium text-text-primary">{s.underlying}</td>
                      <td className="py-1.5 font-mono">{s.expiry}</td>
                      <td className="py-1.5 text-right font-mono">{num(s.days_to_expiry, 1)}</td>
                      <td className="py-1.5 text-right font-mono">{formatPct(s.atm_iv)}</td>
                      <td className="py-1.5 text-right font-mono">{s.n_quotes}</td>
                      <td className="py-1.5 text-right font-mono">{num(s.rmse_vol, 4)}</td>
                      <td className="py-1.5 text-right font-mono text-amber-400/90">{s.n_rejects}</td>
                      <td className="py-1.5">
                        {s.butterfly_ok ? (
                          <span className="text-emerald-400">arb-free</span>
                        ) : (
                          <span className="text-rose-400">violated</span>
                        )}
                      </td>
                      <td className="py-1.5 text-right font-mono text-text-muted">
                        {s.age_minutes == null ? "—" : `${num(s.age_minutes, 0)}m`}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Section>

          <Section
            title="Skew and the variance risk premium"
            description="Front-tenor coordinates. VRP is implied against trailing realised — a long-premium lane pays this, so a wide positive number is a headwind, not an opportunity."
          >
            <div className="grid gap-3 md:grid-cols-3">
              {(surface.data?.tenor_metrics ?? []).map((m: any) => (
                <div key={m.underlying} className="rounded-2xl border border-bg-border bg-bg-secondary/24 p-4">
                  <div className="flex items-baseline justify-between">
                    <span className="text-sm font-semibold text-text-primary">{m.underlying}</span>
                    <span className="font-mono text-[11px] text-text-muted">{m.tenor_days}d</span>
                  </div>
                  <div className="mt-2 space-y-1 text-[12px]">
                    {[
                      ["ATM IV", formatPct(m.atm_iv)],
                      ["Realised", m.realized_vol == null ? "—" : formatPct(m.realized_vol)],
                      [
                        "VRP",
                        m.vrp_spread == null
                          ? "—"
                          : `${num(m.vrp_spread * 100, 2)} vol pts`,
                      ],
                      ["25Δ RR", m.rr_25 == null ? "—" : num(m.rr_25 * 100, 2)],
                      ["Skew slope", m.skew_slope == null ? "—" : num(m.skew_slope, 3)],
                    ].map(([k, v]) => (
                      <div key={k as string} className="flex justify-between">
                        <span className="text-text-muted">{k}</span>
                        <span className="font-mono text-text-primary">{v}</span>
                      </div>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          </Section>

          <Section
            title="Quarantine"
            description="Quotes the IV solver or the SVI fit refused. A day this jumps is a data incident, not a modelling nuance."
          >
            {(surface.data?.quarantine ?? []).length === 0 ? (
              <div className="text-[12px] text-text-muted">Nothing refused in this window.</div>
            ) : (
              <div className="grid gap-2 md:grid-cols-2">
                {surface.data.quarantine.map((q: any, i: number) => (
                  <div key={i} className="flex justify-between rounded-xl bg-bg-secondary/30 px-3 py-2 text-[12px]">
                    <span className="text-text-muted">
                      {q.underlying} · {q.stage} · {q.status}
                    </span>
                    <span className="font-mono text-text-primary">{q.n}</span>
                  </div>
                ))}
              </div>
            )}
          </Section>
        </div>
      )}

      {/* ── BOOK ───────────────────────────────────────────────────────────── */}
      {activeTab === "book" && (
        <Section title="Positions" description="Long premium only. Stop and target are anchored on the fill, not the signal price.">
          {(positions.data?.positions ?? []).length === 0 ? (
            <Empty title="No positions" detail="The lane has neither opened nor closed a position." />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[900px] text-[12px]">
                <thead className="text-[10.5px] uppercase tracking-wider text-text-muted">
                  <tr className="border-b border-bg-border">
                    <th className="py-1.5 text-left font-medium">Contract</th>
                    <th className="py-1.5 text-left font-medium">Status</th>
                    <th className="py-1.5 text-right font-medium">Lots</th>
                    <th className="py-1.5 text-right font-medium">Entry</th>
                    <th className="py-1.5 text-right font-medium">Mark</th>
                    <th className="py-1.5 text-right font-medium">Stop</th>
                    <th className="py-1.5 text-right font-medium">Target</th>
                    <th className="py-1.5 text-right font-medium">Δ / MVΔ</th>
                    <th className="py-1.5 text-right font-medium">P&amp;L</th>
                    <th className="py-1.5 text-left font-medium">Exit</th>
                  </tr>
                </thead>
                <tbody>
                  {positions.data.positions.map((p: any) => {
                    const pnl = p.status === "open" ? p.unrealized_pnl : p.realized_pnl;
                    return (
                      <tr key={p.position_id} className="border-b border-bg-border/40 last:border-0">
                        <td className="py-1.5 font-mono text-text-primary">
                          {p.underlying} {formatNumber(p.strike, 0)}
                          {p.option_type}
                          <span className="ml-2 text-text-muted">{p.expiry}</span>
                        </td>
                        <td className="py-1.5">
                          {p.status === "open" ? (
                            <span className="text-sky-400">open · {p.hold_sessions}s</span>
                          ) : (
                            <span className="text-text-muted">closed</span>
                          )}
                        </td>
                        <td className="py-1.5 text-right font-mono">{p.lots}</td>
                        <td className="py-1.5 text-right font-mono">{num(p.entry_premium)}</td>
                        <td className="py-1.5 text-right font-mono">{num(p.latest_premium)}</td>
                        <td className="py-1.5 text-right font-mono text-rose-400/80">{num(p.stop_premium)}</td>
                        <td className="py-1.5 text-right font-mono text-emerald-400/80">{num(p.target_premium)}</td>
                        <td className="py-1.5 text-right font-mono text-text-muted">
                          {num(p.entry_delta, 3)} / {num(p.entry_mv_delta, 3)}
                        </td>
                        <td
                          className={`py-1.5 text-right font-mono ${
                            pnl == null ? "" : pnl >= 0 ? "text-emerald-400" : "text-rose-400"
                          }`}
                        >
                          {pnl == null ? absent : formatSignedMoney(pnl)}
                        </td>
                        <td className="py-1.5 text-[11px] text-text-muted">{p.exit_reason ?? "—"}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </Section>
      )}

      {/* ── ATTRIBUTION ────────────────────────────────────────────────────── */}
      {activeTab === "attribution" && (
        <div className="space-y-4">
          {(attribution.data?.trades ?? []).length === 0 ? (
            <Empty
              title="No closed trades to attribute"
              detail="Attribution is computed at close, walking the mark trail rather than entry-to-exit, so it needs a completed position."
            />
          ) : (
            <>
              <Section
                title="Per-trade average"
                description="Which greek paid, averaged over closed trades. A large residual means entry greeks stopped describing the position — informative, not hidden."
              >
                <div className="grid grid-cols-2 gap-3 md:grid-cols-6">
                  {[
                    ["Delta", attribution.data.per_trade_average.delta_pnl],
                    ["Gamma", attribution.data.per_trade_average.gamma_pnl],
                    ["Vega", attribution.data.per_trade_average.vega_pnl],
                    ["Theta", attribution.data.per_trade_average.theta_pnl],
                    ["Residual", attribution.data.per_trade_average.residual_pnl],
                    ["Costs", attribution.data.per_trade_average.costs],
                  ].map(([k, v]: any) => (
                    <MetricTile
                      key={k}
                      label={k}
                      value={formatSignedMoney(v)}
                      size="sm"
                      color={v >= 0 ? "text-emerald-400" : "text-rose-400"}
                    />
                  ))}
                </div>
                <div className="mt-3 text-[11.5px] text-text-muted">
                  Explained fraction{" "}
                  <span className="font-mono text-text-primary">
                    {attribution.data.per_trade_average.explained_fraction == null
                      ? "—"
                      : formatPct(attribution.data.per_trade_average.explained_fraction)}
                  </span>{" "}
                  over {attribution.data.per_trade_average.n} closed trades.
                </div>
              </Section>

              <Section title="By trade">
                <div className="overflow-x-auto">
                  <table className="w-full min-w-[820px] text-[12px]">
                    <thead className="text-[10.5px] uppercase tracking-wider text-text-muted">
                      <tr className="border-b border-bg-border">
                        <th className="py-1.5 text-left font-medium">Closed</th>
                        <th className="py-1.5 text-left font-medium">Contract</th>
                        <th className="py-1.5 text-right font-medium">Δ</th>
                        <th className="py-1.5 text-right font-medium">Γ</th>
                        <th className="py-1.5 text-right font-medium">V</th>
                        <th className="py-1.5 text-right font-medium">Θ</th>
                        <th className="py-1.5 text-right font-medium">Resid</th>
                        <th className="py-1.5 text-right font-medium">Net</th>
                        <th className="py-1.5 text-left font-medium">Exit</th>
                      </tr>
                    </thead>
                    <tbody>
                      {attribution.data.trades.map((t: any) => (
                        <tr key={t.position_id} className="border-b border-bg-border/40 last:border-0">
                          <td className="py-1.5 font-mono text-text-muted">{formatIST(t.closed_at)}</td>
                          <td className="py-1.5 font-mono text-text-primary">
                            {t.underlying} {formatNumber(t.strike, 0)}
                            {t.option_type}
                          </td>
                          <td className="py-1.5 text-right font-mono">{formatSignedMoney(t.delta_pnl)}</td>
                          <td className="py-1.5 text-right font-mono">{formatSignedMoney(t.gamma_pnl)}</td>
                          <td className="py-1.5 text-right font-mono">{formatSignedMoney(t.vega_pnl)}</td>
                          <td className="py-1.5 text-right font-mono">{formatSignedMoney(t.theta_pnl)}</td>
                          <td className="py-1.5 text-right font-mono text-text-muted">
                            {formatSignedMoney(t.residual_pnl)}
                          </td>
                          <td
                            className={`py-1.5 text-right font-mono ${
                              t.net_pnl >= 0 ? "text-emerald-400" : "text-rose-400"
                            }`}
                          >
                            {formatSignedMoney(t.net_pnl)}
                          </td>
                          <td className="py-1.5 text-[11px] text-text-muted">{t.exit_reason ?? "—"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </Section>
            </>
          )}
        </div>
      )}
    </DeskShell>
  );
}

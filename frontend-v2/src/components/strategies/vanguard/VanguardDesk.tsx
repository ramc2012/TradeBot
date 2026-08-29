"use client";

/**
 * Vanguard desk (v2) — the M1–M10 trade-selection research lane.
 *
 * ─── The design problem this desk exists to solve ───────────────────────────
 *
 * Vanguard currently emits ZERO tickets. Every candidate dies at one filter or
 * another, so `tickets` holds only gated near-misses and the book is flat.
 *
 * A naive desk renders five empty panels, and an empty panel is
 * indistinguishable from a broken feed — exactly the failure nav-model.ts
 * calls out ("an em-dash indistinguishable from a flat book"). Worse, here the
 * two states have opposite meanings: doctrine #2 says the default answer is NO
 * TRADE, so a reasoned no-trade is the system WORKING, while a stale feed
 * producing no trade is the system BROKEN. They must never look the same.
 *
 * So this desk leads with WHY, not with WHAT — and, since 2026-08-27, with the
 * EVIDENCE underneath the why. The desk previously showed that the lane
 * decided nothing but not what it decided nothing ABOUT: not one symbol's
 * collected market information appeared anywhere in the UI. The Market tab is
 * that missing layer, and it is now the landing tab.
 *
 *   Market     every symbol the lane evaluated at this bar, with every input
 *              it collected and — crucially — the AGE of each one. Click a row
 *              for the full decision trace and every feed behind it. This is
 *              the tab that answers "why not this one?".
 *   Decision   the attrition ribbon: how a bar's universe narrows leg by leg,
 *              which leg is the biggest killer, and how far the whole
 *              cross-section sits from the conviction gate.
 *   Sentiment  market-wide only — FII/DII positioning, PCR, volatility and
 *              breadth. On its own tab rather than as a column because NSE's
 *              participant file is an aggregate with no per-symbol dimension,
 *              and rendering it beside a symbol would invent detail the
 *              exchange does not publish.
 *   Book       fills / outcomes / equity, with unrealized P&L declared ABSENT
 *              rather than shown as 0 (nothing marks open paper positions).
 *   Research   the cross-sectional IC study (the only measurement here that
 *              can currently falsify M2) and M7's risk limits, including
 *              whether they can actually bind.
 *   Attribution M10's cumulative record, every statistic suppressed to "not
 *              enough closed trades" until its own sample gate passes.
 *   Backtest   M8 replay, kept visually distinct from Attribution because a
 *              backtest is not a track record.
 *   Pipeline   per-feed coverage. The place a stale feed is supposed to be
 *              obvious, so the no-trade above it can be trusted.
 *
 * ─── Read-only, deliberately ────────────────────────────────────────────────
 *
 * There is no action button anywhere on this desk. Vanguard's doctrine #4
 * forbids an execution layer, and `/api/vanguard/*` exposes no POST at all —
 * `make daily-cycle` on the research host is the only thing that advances the
 * lane. A "run now" control would be that forbidden layer wearing a UI.
 */
import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  BookOpen,
  Filter,
  FlaskConical,
  GitBranch,
  Info,
  Gauge,
  Layers,
  Sigma,
} from "lucide-react";

import {
  DeskShell,
  MetricTile,
  REFRESH_MS,
  Section,
  StatusBadge,
  formatIST,
  formatMoney,
  formatNumber,
  formatSignedMoney,
  tone,
  useUrlTab,
} from "@/components/desk-ui";
import {
  getVanguardAttribution,
  getVanguardBacktests,
  getVanguardBook,
  getVanguardCrossSection,
  getVanguardFunnel,
  getVanguardMarket,
  getVanguardPipeline,
  getVanguardRisk,
  getVanguardSelection,
  getVanguardMp,
  getMpVerdicts,
  getVanguardSentiment,
  getVanguardSummary,
  getVanguardSymbol,
} from "@/lib/api";

import { DecisionFlowTab } from "./DecisionFlow";
import { MarketTab } from "./MarketTab";
import { ResearchTab } from "./ResearchTab";
import { MpTab } from "./MpTab";
import { SentimentTab } from "./SentimentTab";
import { SymbolDetail } from "./SymbolDetail";

type FunnelStage = { stage: string; surviving: number; gate: string };

/** A value the backend genuinely did not compute. Never rendered as 0. */
function Unmeasured({ why }: { why: string }) {
  return (
    <span className="text-text-muted" title={why}>
      not measured
    </span>
  );
}

function num(value: unknown): number | null {
  const n = typeof value === "string" ? Number(value) : value;
  return typeof n === "number" && Number.isFinite(n) ? n : null;
}

export default function VanguardDesk() {
  const [activeTab, setActiveTab] = useUrlTab("market");
  // Which symbol's detail is open. Kept here rather than in MarketTab so the
  // Decision tab's "these died here" chips can open the same panel — clicking a
  // casualty in the funnel and clicking it in the grid must land in one place.
  const [symbol, setSymbol] = useState<string | null>(null);

  const openSymbol = (next: string | null) => {
    setSymbol(next);
    if (next) setActiveTab("market");
  };

  const summary = useQuery({
    queryKey: ["vanguard", "summary"],
    queryFn: (): Promise<any> => getVanguardSummary().then((r) => r.data),
    refetchInterval: REFRESH_MS.summary,
  });
  const funnel = useQuery({
    queryKey: ["vanguard", "funnel"],
    queryFn: (): Promise<any> => getVanguardFunnel().then((r) => r.data),
    refetchInterval: REFRESH_MS.summary,
    enabled: activeTab === "decision",
  });
  const selection = useQuery({
    queryKey: ["vanguard", "selection"],
    queryFn: (): Promise<any> => getVanguardSelection().then((r) => r.data),
    refetchInterval: REFRESH_MS.summary,
    enabled: activeTab === "decision",
  });
  const book = useQuery({
    queryKey: ["vanguard", "book"],
    queryFn: (): Promise<any> => getVanguardBook().then((r) => r.data),
    refetchInterval: REFRESH_MS.summary,
    enabled: activeTab === "book",
  });
  const attribution = useQuery({
    queryKey: ["vanguard", "attribution"],
    queryFn: (): Promise<any> => getVanguardAttribution().then((r) => r.data),
    enabled: activeTab === "attribution",
  });
  const backtests = useQuery({
    queryKey: ["vanguard", "backtests"],
    queryFn: (): Promise<any> => getVanguardBacktests().then((r) => r.data),
    enabled: activeTab === "backtest",
  });
  const pipeline = useQuery({
    queryKey: ["vanguard", "pipeline"],
    queryFn: (): Promise<any> => getVanguardPipeline().then((r) => r.data),
    enabled: activeTab === "pipeline",
  });
  // The market grid is needed by BOTH the Market tab and the Decision tab (the
  // conviction histogram is over the whole evaluated universe, not just
  // survivors), so it is not gated on a single tab.
  const market = useQuery({
    queryKey: ["vanguard", "market"],
    queryFn: (): Promise<any> => getVanguardMarket().then((r) => r.data),
    refetchInterval: REFRESH_MS.summary,
    enabled: activeTab === "market" || activeTab === "decision",
  });
  const detail = useQuery({
    queryKey: ["vanguard", "symbol", symbol],
    queryFn: (): Promise<any> => getVanguardSymbol(symbol as string).then((r) => r.data),
    enabled: Boolean(symbol),
  });
  const crossSection = useQuery({
    queryKey: ["vanguard", "cross-section"],
    queryFn: (): Promise<any> => getVanguardCrossSection().then((r) => r.data),
    enabled: activeTab === "research",
  });
  const sentiment = useQuery({
    queryKey: ["vanguard", "sentiment"],
    queryFn: (): Promise<any> => getVanguardSentiment().then((r) => r.data),
    enabled: activeTab === "sentiment",
  });
  const mp = useQuery({
    queryKey: ["vanguard", "mp"],
    queryFn: (): Promise<any> => getVanguardMp().then((r) => r.data),
    enabled: activeTab === "mp",
    refetchInterval: REFRESH_MS.summary,
  });
  const mpVerdicts = useQuery({
    queryKey: ["mp", "verdicts"],
    queryFn: (): Promise<any> => getMpVerdicts().then((r) => r.data),
    enabled: activeTab === "mp",
    staleTime: Infinity,
  });
  const risk = useQuery({
    queryKey: ["vanguard", "risk"],
    queryFn: (): Promise<any> => getVanguardRisk().then((r) => r.data),
    enabled: activeTab === "research",
  });

  const s = summary.data;
  const thresholds = s?.thresholds;
  const capital = s?.capital;

  return (
    <DeskShell
      title="Vanguard"
      description="M1–M10 trade-selection research lane — options flow × GEX regime × sector RS × microstructure timing, journaled end to end. Paper only; no broker path exists."
      asOf={s?.latest_timing_bar ?? null}
      asOfLabel="latest timing bar"
      paperMode
      isFetching={summary.isFetching}
      tabs={[
        { key: "market", label: "Market", icon: Layers },
        { key: "decision", label: "Decision flow", icon: Filter },
        { key: "sentiment", label: "Sentiment", icon: Gauge },
        { key: "mp", label: "MP structure", icon: Layers },
        { key: "book", label: "Book", icon: BookOpen },
        { key: "research", label: "Research", icon: Sigma },
        { key: "attribution", label: "Attribution", icon: Activity },
        { key: "backtest", label: "Backtest", icon: FlaskConical },
        { key: "pipeline", label: "Pipeline", icon: GitBranch },
      ]}
      activeTab={activeTab}
      onTabChange={setActiveTab}
    >
      {activeTab === "market" &&
        (symbol ? (
          <SymbolDetail
            data={detail.data}
            loading={detail.isLoading}
            thresholds={market.data?.thresholds}
            onBack={() => setSymbol(null)}
          />
        ) : (
          <MarketTab
            market={market.data}
            selectedSymbol={symbol}
            onPickSymbol={openSymbol}
          />
        ))}
      {activeTab === "decision" && (
        <DecisionFlowTab
          funnel={funnel.data}
          selection={selection.data}
          market={market.data}
          onPickSymbol={openSymbol}
        />
      )}
      {activeTab === "sentiment" && <SentimentTab data={sentiment.data} />}
      {activeTab === "mp" && <MpTab data={mp.data} verdicts={mpVerdicts.data} />}
      {activeTab === "research" && (
        <ResearchTab crossSection={crossSection.data} risk={risk.data} />
      )}
      {activeTab === "book" && <BookTab book={book.data} capital={capital} summary={s} />}
      {activeTab === "attribution" && <AttributionTab data={attribution.data} />}
      {activeTab === "backtest" && <BacktestTab data={backtests.data} />}
      {activeTab === "pipeline" && <PipelineTab data={pipeline.data} />}
    </DeskShell>
  );
}

// ─── Selection ──────────────────────────────────────────────────────────────
//
// SelectionTab lived here and drew the funnel plus the ticket table. Both moved
// to ./DecisionFlow.tsx when the funnel stopped being a re-derivation of M6's
// filter and became a projection of the journal M6 writes (see migration 006).
// Deleted rather than left dormant: a second, older renderer of the same
// numbers is exactly how the funnel and the selector drifted apart in the first
// place.

// ─── Book ───────────────────────────────────────────────────────────────────

function BookTab({ book, capital, summary }: { book?: any; capital?: any; summary?: any }) {
  const closed = book?.closed ?? [];
  const open = book?.open_positions ?? [];
  const equity = book?.equity_curve ?? [];
  const hitRate = num(summary?.book?.hit_rate);

  return (
    <div className="space-y-4">
      <Section title="Paper book" icon={<BookOpen size={16} />}>
        <div className="grid grid-cols-2 gap-2 md:grid-cols-3 lg:grid-cols-5">
          <MetricTile label="Equity" value={formatMoney(num(capital?.ending_equity))} />
          <MetricTile
            label="Realized P&L"
            value={formatSignedMoney(num(capital?.realized_pnl))}
            color={tone(num(capital?.realized_pnl))}
          />
          <MetricTile label="Open" value={String(open.length)} />
          <MetricTile label="Closed" value={String(closed.length)} />
          <MetricTile
            label="Hit rate"
            value={hitRate == null ? "—" : `${(hitRate * 100).toFixed(1)}%`}
            detail={hitRate == null ? "no closed trades yet" : undefined}
          />
        </div>
        <p className="mt-3 text-xs text-text-muted">
          Unrealized P&amp;L is <Unmeasured why="Nothing marks open paper positions to market; a 0 here would read as a flat book." />{" "}
          — open positions are not marked to market, so it is genuinely uncomputed rather than zero.
        </p>
      </Section>

      <Section
        title="Closed trades"
        description="Every fill is simulated against real historical bars. `fill_method` is carried through verbatim so a simulated fill is never mistaken for a broker fill."
      >
        {closed.length === 0 ? (
          <p className="text-sm text-text-secondary">
            No position has ever been closed, because none has ever been opened — the selector has
            not emitted a ticket. See the Selection tab for which gate is binding.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[820px] text-sm">
              <thead>
                <tr className="border-b border-bg-border text-left text-[11px] uppercase tracking-wider text-text-muted">
                  <th className="py-2 pr-3">Symbol</th>
                  <th className="py-2 pr-3">Instrument</th>
                  <th className="py-2 pr-3 text-right">Entry</th>
                  <th className="py-2 pr-3 text-right">Exit</th>
                  <th className="py-2 pr-3">Reason</th>
                  <th className="py-2 pr-3 text-right">P&amp;L</th>
                  <th className="py-2 pr-3 text-right">R</th>
                </tr>
              </thead>
              <tbody>
                {closed.map((t: any) => (
                  <tr key={t.id} className="border-b border-bg-border/50">
                    <td className="py-2 pr-3 font-medium text-text-primary">{t.symbol}</td>
                    <td className="py-2 pr-3 font-mono text-xs text-text-secondary">{t.instrument}</td>
                    <td className="py-2 pr-3 text-right font-mono">{formatNumber(num(t.fill_price), 2)}</td>
                    <td className="py-2 pr-3 text-right font-mono">{formatNumber(num(t.exit_price), 2)}</td>
                    <td className="py-2 pr-3 text-xs text-text-secondary">{t.exit_reason}</td>
                    <td className={`py-2 pr-3 text-right font-mono ${tone(num(t.pnl_rupees))}`}>
                      {formatSignedMoney(num(t.pnl_rupees))}
                    </td>
                    <td className={`py-2 pr-3 text-right font-mono ${tone(num(t.r_multiple))}`}>
                      {formatNumber(num(t.r_multiple), 2)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>

      {equity.length > 0 && (
        <Section title="Equity curve" description="One row per session, written by M9's own capital rollup.">
          <div className="overflow-x-auto">
            <table className="w-full min-w-[520px] text-sm">
              <thead>
                <tr className="border-b border-bg-border text-left text-[11px] uppercase tracking-wider text-text-muted">
                  <th className="py-2 pr-3">Session</th>
                  <th className="py-2 pr-3 text-right">Start</th>
                  <th className="py-2 pr-3 text-right">Realized</th>
                  <th className="py-2 pr-3 text-right">End</th>
                </tr>
              </thead>
              <tbody>
                {equity.map((row: any) => (
                  <tr key={row.dt} className="border-b border-bg-border/50">
                    <td className="py-2 pr-3 font-mono text-xs">{row.dt}</td>
                    <td className="py-2 pr-3 text-right font-mono">{formatMoney(num(row.starting_equity))}</td>
                    <td className={`py-2 pr-3 text-right font-mono ${tone(num(row.realized_pnl))}`}>
                      {formatSignedMoney(num(row.realized_pnl))}
                    </td>
                    <td className="py-2 pr-3 text-right font-mono">{formatMoney(num(row.ending_equity))}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Section>
      )}
    </div>
  );
}

// ─── Attribution ────────────────────────────────────────────────────────────

function AttributionTab({ data }: { data?: any }) {
  const latest = data?.latest;
  const report = latest?.report ?? {};
  const ic = report.component_ic ?? {};
  const deciles = report.conviction_decile_report ?? [];
  const adequate = report.decile_sample_size_adequate;

  return (
    <div className="space-y-4">
      <Section
        title="Cumulative record"
        icon={<Activity size={16} />}
        description={
          latest
            ? `M10 rollup as of ${latest.as_of_date}, over every closed paper trade up to that date.`
            : "No attribution run has been recorded."
        }
      >
        <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
          <MetricTile label="Closed trades" value={String(latest?.n_tickets_closed ?? 0)} />
          <MetricTile
            label="Hit rate"
            value={latest?.hit_rate == null ? "—" : `${(latest.hit_rate * 100).toFixed(1)}%`}
            detail={latest?.hit_rate == null ? "no closed trades" : undefined}
          />
          <MetricTile
            label="Avg R"
            value={latest?.avg_r == null ? "—" : formatNumber(num(latest.avg_r), 3)}
            color={tone(num(latest?.avg_r))}
            detail={latest?.avg_r == null ? "no closed trades" : "R = P&L ÷ premium paid"}
          />
          <MetricTile
            label="Decile monotonic"
            value={
              latest?.conviction_decile_monotonic == null
                ? "—"
                : latest.conviction_decile_monotonic
                  ? "yes"
                  : "no"
            }
            detail={adequate === false ? "sample too small to be a verdict" : undefined}
          />
        </div>
        {adequate === false && (
          <p className="mt-3 text-xs text-accent-amber">
            Sample below the module&apos;s own threshold — these figures are reported, not
            concluded. The decile check is not a verdict yet.
          </p>
        )}
      </Section>

      <Section
        title="Per-component information coefficient"
        description="Correlation between each fused component score and the eventual R. Suppressed entirely until each component has enough paired observations — a coefficient from a handful of points is noise wearing a number."
      >
        <div className="grid grid-cols-2 gap-2 md:grid-cols-5">
          {["flow", "sector_rs", "timing", "regime", "leadlag"].map((k) => {
            const entry = ic[k] ?? {};
            return (
              <MetricTile
                key={k}
                size="sm"
                label={k.replace("_", " ")}
                value={entry.ic == null ? "—" : formatNumber(num(entry.ic), 3)}
                color={tone(num(entry.ic))}
                detail={entry.ic == null ? entry.reason ?? "not computed" : `n=${entry.n}`}
              />
            );
          })}
        </div>
      </Section>

      {deciles.length > 0 && (
        <Section title="Conviction deciles">
          <div className="overflow-x-auto">
            <table className="w-full min-w-[480px] text-sm">
              <thead>
                <tr className="border-b border-bg-border text-left text-[11px] uppercase tracking-wider text-text-muted">
                  <th className="py-2 pr-3">Bucket</th>
                  <th className="py-2 pr-3">Conviction range</th>
                  <th className="py-2 pr-3 text-right">n</th>
                  <th className="py-2 pr-3 text-right">Win rate</th>
                  <th className="py-2 pr-3 text-right">Avg R</th>
                </tr>
              </thead>
              <tbody>
                {deciles.map((d: any) => (
                  <tr key={d.bucket} className="border-b border-bg-border/50">
                    <td className="py-2 pr-3 font-mono">{d.bucket}</td>
                    <td className="py-2 pr-3 font-mono text-xs">
                      {(d.conviction_range ?? []).join(" – ")}
                    </td>
                    <td className="py-2 pr-3 text-right font-mono">{d.n}</td>
                    <td className="py-2 pr-3 text-right font-mono">
                      {(Number(d.win_rate) * 100).toFixed(0)}%
                    </td>
                    <td className={`py-2 pr-3 text-right font-mono ${tone(num(d.avg_r))}`}>
                      {formatNumber(num(d.avg_r), 3)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Section>
      )}
    </div>
  );
}

// ─── Backtest ───────────────────────────────────────────────────────────────

function BacktestTab({ data }: { data?: any }) {
  const latest = data?.latest;
  const report = latest?.report ?? {};
  const deciles = report.conviction_decile_report ?? [];

  return (
    <div className="space-y-4">
      <Section
        title="Historical replay"
        icon={<FlaskConical size={16} />}
        description={
          latest
            ? `M8 replay of ${formatIST(latest.start_ts)} → ${formatIST(latest.end_ts)}, run ${formatIST(latest.run_at)}.`
            : "No backtest has been run."
        }
        rightSlot={<StatusBadge label="not a track record" variant="warn" />}
      >
        {!latest ? (
          <p className="text-sm text-text-secondary">No replay recorded yet.</p>
        ) : (
          <>
            <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
              <MetricTile label="Candidates" value={String(report.candidates_evaluated ?? 0)} />
              <MetricTile
                label="Emitted & closed"
                value={String(report.emitted_trades_closed ?? 0)}
                detail={
                  (report.emitted_trades_unresolved ?? 0) + (report.emitted_trades_without_sizing ?? 0) > 0
                    ? `${report.emitted_trades_unresolved ?? 0} unresolved, ${report.emitted_trades_without_sizing ?? 0} unsized`
                    : undefined
                }
              />
              <MetricTile
                label="Expectancy (net)"
                value={
                  report.expectancy_net_rupees == null
                    ? "—"
                    : formatSignedMoney(num(report.expectancy_net_rupees))
                }
                color={tone(num(report.expectancy_net_rupees))}
                detail="after modelled costs"
              />
              <MetricTile
                label="Max drawdown"
                value={
                  report.max_drawdown_rupees == null
                    ? "—"
                    : formatSignedMoney(num(report.max_drawdown_rupees))
                }
                color={tone(num(report.max_drawdown_rupees))}
              />
            </div>
            {report.decile_check_sample_size_adequate === false && (
              <p className="mt-3 text-xs text-accent-amber">
                Sample below the harness&apos;s own adequacy threshold — reported, not concluded.
              </p>
            )}
          </>
        )}
      </Section>

      {deciles.length > 0 && (
        <Section
          title="Conviction deciles (replay)"
          description="Computed over EVERY filter-passing candidate, not just emitted tickets — an emitted-only check would be tautological while conviction never reaches the emission threshold."
        >
          <div className="overflow-x-auto">
            <table className="w-full min-w-[480px] text-sm">
              <thead>
                <tr className="border-b border-bg-border text-left text-[11px] uppercase tracking-wider text-text-muted">
                  <th className="py-2 pr-3">Bucket</th>
                  <th className="py-2 pr-3">Conviction range</th>
                  <th className="py-2 pr-3 text-right">n</th>
                  <th className="py-2 pr-3 text-right">Win rate</th>
                  <th className="py-2 pr-3 text-right">Avg R</th>
                </tr>
              </thead>
              <tbody>
                {deciles.map((d: any) => (
                  <tr key={d.bucket} className="border-b border-bg-border/50">
                    <td className="py-2 pr-3 font-mono">{d.bucket}</td>
                    <td className="py-2 pr-3 font-mono text-xs">
                      {(d.conviction_range ?? []).join(" – ")}
                    </td>
                    <td className="py-2 pr-3 text-right font-mono">{d.n}</td>
                    <td className="py-2 pr-3 text-right font-mono">
                      {(Number(d.win_rate) * 100).toFixed(0)}%
                    </td>
                    <td className={`py-2 pr-3 text-right font-mono ${tone(num(d.avg_r))}`}>
                      {formatNumber(num(d.avg_r), 3)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Section>
      )}
    </div>
  );
}

// ─── Pipeline ───────────────────────────────────────────────────────────────

function PipelineTab({ data }: { data?: any }) {
  const feeds = data?.feeds ?? [];
  const sessions = data?.recent_sessions ?? [];
  const withAll = data?.sessions_with_all_inputs ?? 0;
  const examined = data?.sessions_examined ?? 0;
  const ingest = data?.ingest_log ?? [];

  const coverageWarning = useMemo(
    () => examined > 0 && withAll < examined,
    [examined, withAll],
  );

  return (
    <div className="space-y-4">
      <Section
        title="Feed coverage"
        icon={<GitBranch size={16} />}
        description="M6 needs flow + sector RS (prior session) AND regime + timing (same bar) simultaneously. Where those coverages fail to intersect, no threshold tuning can produce a trade."
      >
        <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
          {feeds.map((f: any) => (
            <div key={f.table} className="rounded-xl border border-bg-border bg-bg-secondary/25 px-3 py-2">
              <div className="flex items-baseline justify-between gap-2">
                <span className="text-sm font-medium text-text-primary">{f.label}</span>
                <StatusBadge label={f.cadence === "bar" ? "per bar" : "per session"} variant="info" />
              </div>
              <div className="mt-1 font-mono text-xs text-text-secondary">
                {formatNumber(num(f.rows), 0)} rows · {f.entities} entities
              </div>
              <div className="mt-0.5 font-mono text-[11px] text-text-muted">
                {f.first_ts ? formatIST(f.first_ts) : "—"} → {f.last_ts ? formatIST(f.last_ts) : "—"}
              </div>
            </div>
          ))}
        </div>
        {coverageWarning && (
          <p className="mt-3 text-xs text-accent-amber">
            Only {withAll} of the last {examined} sessions carry all three per-symbol inputs. On the
            others the selector cannot form a candidate at all — a DATA gap, not a strategy decision.
          </p>
        )}
      </Section>

      {sessions.length > 0 && (
        <Section title="Recent sessions" description="Row counts per input, newest first.">
          <div className="overflow-x-auto">
            <table className="w-full min-w-[480px] text-sm">
              <thead>
                <tr className="border-b border-bg-border text-left text-[11px] uppercase tracking-wider text-text-muted">
                  <th className="py-2 pr-3">Session</th>
                  <th className="py-2 pr-3 text-right">Flow</th>
                  <th className="py-2 pr-3 text-right">Timing</th>
                  <th className="py-2 pr-3 text-right">Regime</th>
                </tr>
              </thead>
              <tbody>
                {sessions.map((row: any) => {
                  const complete =
                    (row.flow_rows ?? 0) > 0 && (row.timing_rows ?? 0) > 0 && (row.regime_rows ?? 0) > 0;
                  return (
                    <tr key={String(row.session)} className="border-b border-bg-border/50">
                      <td className="py-2 pr-3 font-mono text-xs">
                        {String(row.session)}
                        {!complete && <span className="ml-2 text-accent-amber">incomplete</span>}
                      </td>
                      <td className={`py-2 pr-3 text-right font-mono ${row.flow_rows ? "" : "text-accent-red"}`}>
                        {row.flow_rows ?? 0}
                      </td>
                      <td className={`py-2 pr-3 text-right font-mono ${row.timing_rows ? "" : "text-accent-red"}`}>
                        {row.timing_rows ?? 0}
                      </td>
                      <td className={`py-2 pr-3 text-right font-mono ${row.regime_rows ? "" : "text-accent-red"}`}>
                        {row.regime_rows ?? 0}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </Section>
      )}

      {ingest.length > 0 && (
        <Section title="Collector log" description="Every M1 ingest run, successful or not (doctrine #5).">
          <div className="max-h-72 overflow-y-auto">
            <table className="w-full min-w-[560px] text-sm">
              <thead>
                <tr className="border-b border-bg-border text-left text-[11px] uppercase tracking-wider text-text-muted">
                  <th className="py-2 pr-3">Collector</th>
                  <th className="py-2 pr-3">Target</th>
                  <th className="py-2 pr-3">Status</th>
                  <th className="py-2 pr-3 text-right">Rows</th>
                </tr>
              </thead>
              <tbody>
                {ingest.map((row: any, i: number) => (
                  <tr key={i} className="border-b border-bg-border/50">
                    <td className="py-2 pr-3 font-mono text-xs">{row.collector}</td>
                    <td className="py-2 pr-3 font-mono text-xs text-text-secondary">
                      {row.target_date ?? "—"}
                    </td>
                    <td className="py-2 pr-3">
                      <StatusBadge
                        label={row.status}
                        variant={row.status === "ok" ? "success" : row.status === "error" ? "error" : "neutral"}
                      />
                    </td>
                    <td className="py-2 pr-3 text-right font-mono">{row.rows_written ?? 0}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Section>
      )}
    </div>
  );
}

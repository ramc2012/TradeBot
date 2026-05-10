"use client";

import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { clsx } from "clsx";
import {
  Activity,
  BarChart3,
  Brain,
  CheckCircle2,
  Database,
  RefreshCw,
  Shield,
  WifiOff,
} from "lucide-react";

import { getBrokerStatus, getStrategyAgentStatus } from "@/lib/api";
import { isBrokerReady, type BrokerStatusEntry } from "@/lib/broker-status";
import { useStore } from "@/store";

type StrategySummary = {
  available_capital?: number | null;
  total_equity?: number | null;
  unrealized_pnl?: number | null;
  realized_pnl?: number | null;
  day_pnl?: number | null;
  total_trades?: number | null;
  win_rate?: number | null;
  open_positions?: number | null;
  entries?: number | null;
  exits?: number | null;
};

type StrategyPosition = {
  symbol?: string | null;
  underlying?: string | null;
  expiry?: string | null;
  strike?: number | null;
  option_type?: string | null;
  qty?: number | null;
  entry_price?: number | null;
  current_price?: number | null;
  entered_at?: string | null;
  price_updated_at?: string | null;
  phase?: string | null;
  signal_reason?: string | null;
  latest_rsi?: number | null;
  entry_iv_pct?: number | null;
  regime?: string | null;
  spot_setup?: string | null;
  unrealized_pnl?: number | null;
  return_pct?: number | null;
};

type StrategySignal = {
  underlying?: string | null;
  direction?: string | null;
  status?: string | null;
  freshness?: string | null;
  reason?: string | null;
  instruction?: string | null;
  source?: string | null;
  as_of?: string | null;
  signal_date?: string | null;
  trade_date?: string | null;
  spot_price?: number | null;
  atm_strike?: number | null;
  ltp?: number | null;
  iv_pct?: number | null;
  priority_score?: number | null;
  mp_day_type?: string | null;
  poc?: number | null;
  vah?: number | null;
  val?: number | null;
  option_last_bar_time?: string | null;
  spot_last_time?: string | null;
};

type StrategyTrade = {
  symbol?: string | null;
  action?: string | null;
  qty?: number | null;
  entry_price?: number | null;
  exit_price?: number | null;
  pnl?: number | null;
  entry_time?: string | null;
  exit_time?: string | null;
  option_type?: string | null;
};

type StrategyPayload = {
  key?: string | null;
  label?: string | null;
  last_scan_at?: string | null;
  last_message?: string | null;
  summary?: StrategySummary;
  positions?: StrategyPosition[];
  trade_history?: StrategyTrade[];
  signals?: StrategySignal[];
  meta?: Record<string, any>;
};

type StrategyAgentStatus = {
  running?: boolean;
  loop_active?: boolean;
  auto_run_enabled?: boolean;
  kill_switch_active?: boolean;
  last_run_at?: string | null;
  next_scan_at?: string | null;
  last_message?: string | null;
  data_health?: Record<string, any>;
  target_expiry?: string | null;
  strategies?: StrategyPayload[];
};

const TRADING_BROKERS = ["fyers", "upstox"];
const BROKER_LABEL: Record<string, string> = {
  fyers: "Fyers",
  upstox: "Upstox",
};

function formatNumber(value?: number | null, digits = 2) {
  if (value == null || Number.isNaN(value)) return "--";
  return value.toFixed(digits);
}

function formatSigned(value?: number | null, digits = 2, suffix = "") {
  if (value == null || Number.isNaN(value)) return "--";
  const prefix = value > 0 ? "+" : "";
  return `${prefix}${value.toFixed(digits)}${suffix}`;
}

function formatMoney(value?: number | null) {
  if (value == null || Number.isNaN(value)) return "--";
  const sign = value > 0 ? "+" : value < 0 ? "-" : "";
  const abs = Math.abs(value);
  if (abs >= 10_00_000) return `${sign}Rs ${(abs / 10_00_000).toFixed(2)}L`;
  if (abs >= 1_000) return `${sign}Rs ${(abs / 1_000).toFixed(1)}K`;
  return `${sign}Rs ${abs.toFixed(0)}`;
}

function formatTimestamp(value?: string | null) {
  if (!value) return "--";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString("en-IN", {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

function prettify(value?: string | null) {
  if (!value) return "--";
  return value
    .replaceAll("_", " ")
    .replaceAll("-", " ")
    .split(" ")
    .filter(Boolean)
    .map((part) => part.slice(0, 1).toUpperCase() + part.slice(1))
    .join(" ");
}

function pnlTone(value?: number | null) {
  if (value == null || Number.isNaN(value)) return "text-text-muted";
  if (value > 0) return "text-accent-green";
  if (value < 0) return "text-accent-red";
  return "text-text-secondary";
}

function badgeTone(value?: string | null) {
  const text = String(value || "").toLowerCase();
  if (["ready", "prepared", "ok", "active", "connected", "ce", "trend-aligned", "entry-ready"].some((key) => text.includes(key))) {
    return "border-accent-green/30 bg-accent-green/10 text-accent-green";
  }
  if (["pe", "stale", "waiting", "monitoring", "market closed", "session-close"].some((key) => text.includes(key))) {
    return "border-accent-amber/30 bg-accent-amber/10 text-accent-amber";
  }
  if (["blocked", "missing", "error", "expired", "offline", "kill"].some((key) => text.includes(key))) {
    return "border-accent-red/30 bg-accent-red/10 text-accent-red";
  }
  return "border-bg-border bg-bg-secondary/40 text-text-secondary";
}

function StatusBadge({ label, tone }: { label: string; tone?: string | null }) {
  return (
    <span
      className={clsx(
        "inline-flex items-center rounded-md border px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-[0.08em]",
        badgeTone(tone || label),
      )}
    >
      {label}
    </span>
  );
}

function MetricTile({
  label,
  value,
  detail,
  tone,
}: {
  label: string;
  value: string;
  detail?: string;
  tone?: string;
}) {
  return (
    <div className="rounded-lg border border-bg-border bg-bg-secondary/35 px-3 py-2" title={detail || label}>
      <div className="text-[10px] uppercase tracking-[0.1em] text-text-muted">{label}</div>
      <div className={clsx("mt-1 font-mono text-base font-semibold text-text-primary", tone)}>{value}</div>
      {detail ? <div className="truncate text-[10px] text-text-muted">{detail}</div> : null}
    </div>
  );
}

function PanelHeader({
  icon,
  title,
  detail,
  meta,
}: {
  icon: React.ReactNode;
  title: string;
  detail: string;
  meta?: string;
}) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-2">
      <div className="flex items-center gap-2 text-sm font-semibold text-text-primary" title={detail}>
        {icon}
        {title}
      </div>
      {meta ? <div className="text-[11px] text-text-muted">{meta}</div> : null}
    </div>
  );
}

function BucketBar({ rows }: { rows: StrategySignal[] }) {
  const counts = rows.reduce(
    (acc, row) => {
      const key = String(row.direction || row.status || row.freshness || "other").toUpperCase();
      if (key.includes("CE")) acc.ce += 1;
      else if (key.includes("PE")) acc.pe += 1;
      else if (key.includes("MISSING")) acc.missing += 1;
      else acc.watch += 1;
      return acc;
    },
    { ce: 0, pe: 0, watch: 0, missing: 0 },
  );
  const total = Math.max(1, counts.ce + counts.pe + counts.watch + counts.missing);
  const segments = [
    { label: "CE", value: counts.ce, className: "bg-accent-green" },
    { label: "PE", value: counts.pe, className: "bg-accent-red" },
    { label: "Watch", value: counts.watch, className: "bg-accent-amber" },
    { label: "Missing", value: counts.missing, className: "bg-text-muted/50" },
  ];

  return (
    <div className="rounded-lg border border-bg-border bg-bg-secondary/25 px-3 py-2" title="Saved signal split by CE, PE, watch and missing lanes">
      <div className="mb-1 flex items-center justify-between text-[10px] uppercase tracking-[0.08em] text-text-muted">
        <span>Signal Split</span>
        <span className="font-mono">{counts.ce}/{counts.pe}/{counts.watch}/{counts.missing}</span>
      </div>
      <div className="flex h-2 overflow-hidden rounded-full bg-bg-primary">
        {segments.map((segment) => (
          <div
            key={segment.label}
            className={segment.className}
            style={{ width: `${(segment.value / total) * 100}%` }}
            title={`${segment.label}: ${segment.value}`}
          />
        ))}
      </div>
    </div>
  );
}

function MarketBrokerState() {
  const layoutBrokerStatuses = useStore((state) => state.brokerStatuses);
  const statusQuery = useQuery<BrokerStatusEntry[]>({
    queryKey: ["brokerHealthBanner"],
    queryFn: () => getBrokerStatus().then((r) => r.data),
    refetchInterval: 30_000,
    staleTime: 20_000,
  });

  const entries = statusQuery.data?.length ? statusQuery.data : layoutBrokerStatuses;
  const trading = entries.filter((entry) => TRADING_BROKERS.includes(entry.broker));
  const disconnected = trading.filter((entry) => !isBrokerReady(entry));
  const connected = trading.filter((entry) => isBrokerReady(entry));

  if (disconnected.length === 0 && connected.length > 0) {
    return (
      <div className="inline-flex items-center gap-2 rounded-full border border-accent-green/20 bg-accent-green/8 px-3 py-2 text-xs text-accent-green">
        <CheckCircle2 size={13} className="shrink-0" />
        <span className="font-medium">Brokers</span>
        <span>{connected.map((entry) => BROKER_LABEL[entry.broker] ?? entry.broker).join(" · ")}</span>
      </div>
    );
  }

  const names = disconnected.map((entry) => BROKER_LABEL[entry.broker] ?? entry.broker).join(", ") || "Fyers, Upstox";
  return (
    <a
      href="/settings"
      className="inline-flex items-center gap-2 rounded-full border border-accent-red/25 bg-accent-red/8 px-3 py-2 text-xs text-accent-red transition-colors hover:opacity-90"
      title="Broker sessions can stay offline on holidays; this page reads saved NSE strategy state."
    >
      <WifiOff size={13} className="shrink-0" />
      <span className="font-semibold">{names} offline</span>
    </a>
  );
}

function findStrategy(status: StrategyAgentStatus | undefined, key: string) {
  return (status?.strategies || []).find((strategy) => strategy.key === key) || null;
}

export default function MarketPage() {
  const statusQuery = useQuery<StrategyAgentStatus>({
    queryKey: ["nseStrategyMarketIntelligence"],
    queryFn: () => getStrategyAgentStatus().then((response) => response.data),
    refetchInterval: 60_000,
    staleTime: 15_000,
    refetchOnWindowFocus: false,
  });

  const status = statusQuery.data;
  const strategy1 = findStrategy(status, "macd_strategy");
  const strategy2 = findStrategy(status, "index_mp_strategy");
  const strategy1Prepared = useMemo<StrategySignal[]>(
    () => (strategy1?.meta?.prepared_watchlist || strategy1?.signals || []) as StrategySignal[],
    [strategy1?.meta?.prepared_watchlist, strategy1?.signals],
  );
  const strategy2Signals = useMemo<StrategySignal[]>(
    () => (strategy2?.signals || []) as StrategySignal[],
    [strategy2?.signals],
  );
  const allPositions = [...(strategy1?.positions || []), ...(strategy2?.positions || [])];
  const marketHealth = status?.data_health?.market_intelligence || strategy1?.meta?.market_intelligence || strategy2?.meta?.market_intelligence || {};
  const brokerSnapshot = status?.data_health?.broker_snapshot || strategy1?.meta?.broker_snapshot || strategy2?.meta?.broker_snapshot || {};
  const strategy2Pipeline = (strategy2?.meta?.pipeline || []) as Array<Record<string, any>>;
  const okPipeline = strategy2Pipeline.filter((row) => row.status === "ok").length;
  const totalUnrealized = (strategy1?.summary?.unrealized_pnl || 0) + (strategy2?.summary?.unrealized_pnl || 0);
  const totalRealized = (strategy1?.summary?.realized_pnl || 0) + (strategy2?.summary?.realized_pnl || 0);

  return (
    <div className="mx-auto max-w-[1680px] space-y-3 pb-6">
      <section className="rounded-xl border border-bg-active/60 bg-bg-secondary/30 px-3 py-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <div className="flex items-center gap-2 font-mono text-xl font-semibold text-text-primary" title="NSE Strategy 1 and Strategy 2 saved intelligence only">
              <Brain size={18} className="text-accent-blue" />
              Market Intelligence
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <button
              onClick={() => void statusQuery.refetch()}
              className="inline-flex items-center gap-2 rounded-lg border border-bg-border bg-bg-secondary/35 px-3 py-2 text-xs text-text-secondary transition-colors hover:text-text-primary"
              title="Refresh saved NSE strategy state"
            >
              <RefreshCw size={14} className={statusQuery.isFetching ? "animate-spin" : ""} />
              Refresh
            </button>
            <MarketBrokerState />
          </div>
        </div>

        <div className="mt-3 grid gap-2 md:grid-cols-3 xl:grid-cols-9">
          <MetricTile label="Last Saved" value={formatTimestamp(status?.last_run_at)} detail={status?.last_message || undefined} />
          <MetricTile label="Runtime" value={status?.running ? "Running" : status?.loop_active ? "Loop" : "Idle"} detail={status?.last_message || undefined} />
          <MetricTile label="Broker Gate" value={brokerSnapshot?.broker_ready ? "Ready" : "Offline"} detail={`Fyers ${brokerSnapshot?.fyers_ready ? "ready" : "off"} / Upstox ${brokerSnapshot?.upstox_ready ? "ready" : "off"}`} tone={brokerSnapshot?.broker_ready ? "text-accent-green" : "text-accent-red"} />
          <MetricTile label="S1 Saved" value={String(strategy1Prepared.length)} detail={`${strategy1?.positions?.length || 0} open positions`} />
          <MetricTile label="S2 Lanes" value={String(strategy2Signals.length)} detail={`${okPipeline}/${strategy2Pipeline.length || 0} replay feeds OK`} />
          <MetricTile label="Latest Rows" value={String(marketHealth?.watchlist_rows_latest || strategy1?.meta?.watchlist_rows || 0)} detail={formatTimestamp(marketHealth?.latest_watchlist_time || strategy1?.meta?.latest_watchlist_time)} />
          <MetricTile label="Open Positions" value={String(allPositions.length)} />
          <MetricTile label="Open P&L" value={formatMoney(totalUnrealized)} tone={pnlTone(totalUnrealized)} />
          <MetricTile label="Realized" value={formatMoney(totalRealized)} tone={pnlTone(totalRealized)} />
        </div>
      </section>

      <section className="grid gap-3 xl:grid-cols-2">
        <div className="rounded-xl border border-bg-border bg-bg-secondary/20 p-3">
          <PanelHeader
            icon={<Activity size={16} className="text-accent-green" />}
            title="Strategy 1 · 30m ATM MACD"
            detail="Saved Strategy 1 positions and prepared CE/PE candidates from the last usable NSE data."
            meta={prettify(strategy1?.meta?.mode || "waiting")}
          />
          <div className="mt-3 grid gap-2 md:grid-cols-4">
            <MetricTile label="Equity" value={formatMoney(strategy1?.summary?.total_equity)} />
            <MetricTile label="Available" value={formatMoney(strategy1?.summary?.available_capital)} />
            <MetricTile label="Open P&L" value={formatMoney(strategy1?.summary?.unrealized_pnl)} tone={pnlTone(strategy1?.summary?.unrealized_pnl)} />
            <BucketBar rows={strategy1Prepared} />
          </div>
          <div className="mt-3 overflow-hidden rounded-lg border border-bg-border">
            <table className="w-full min-w-[980px] text-xs">
              <thead className="bg-bg-primary/60 text-text-muted">
                <tr>
                  <th className="px-3 py-2 text-left">Open Position</th>
                  <th className="px-3 py-2 text-right">Qty</th>
                  <th className="px-3 py-2 text-right">Entry</th>
                  <th className="px-3 py-2 text-right">Last</th>
                  <th className="px-3 py-2 text-right">P&L</th>
                  <th className="px-3 py-2 text-right">Return</th>
                  <th className="px-3 py-2 text-left">State</th>
                  <th className="px-3 py-2 text-left">Updated</th>
                </tr>
              </thead>
              <tbody>
                {(strategy1?.positions || []).map((position, index) => (
                  <tr key={position.symbol || `${position.underlying || "position"}-${index}`} className="border-t border-bg-border/60">
                    <td className="px-3 py-2">
                      <div className="font-semibold text-text-primary">{position.underlying || position.symbol}</div>
                      <div className="text-[11px] text-text-muted">{position.expiry} {formatNumber(position.strike, 0)} {position.option_type}</div>
                    </td>
                    <td className="px-3 py-2 text-right font-mono">{position.qty || "--"}</td>
                    <td className="px-3 py-2 text-right font-mono">{formatNumber(position.entry_price)}</td>
                    <td className="px-3 py-2 text-right font-mono">{formatNumber(position.current_price)}</td>
                    <td className={clsx("px-3 py-2 text-right font-mono font-semibold", pnlTone(position.unrealized_pnl))}>{formatMoney(position.unrealized_pnl)}</td>
                    <td className={clsx("px-3 py-2 text-right font-mono", pnlTone(position.return_pct))}>{formatSigned(position.return_pct, 2, "%")}</td>
                    <td className="px-3 py-2"><StatusBadge label={prettify(position.phase)} tone={position.phase} /></td>
                    <td className="px-3 py-2 text-text-muted">{formatTimestamp(position.price_updated_at || position.entered_at)}</td>
                  </tr>
                ))}
                {!strategy1?.positions?.length ? (
                  <tr><td colSpan={8} className="px-3 py-6 text-center text-text-muted">No saved open Strategy 1 positions.</td></tr>
                ) : null}
              </tbody>
            </table>
          </div>
        </div>

        <div className="rounded-xl border border-bg-border bg-bg-secondary/20 p-3">
          <PanelHeader
            icon={<BarChart3 size={16} className="text-accent-blue" />}
            title="Strategy 2 · 5m Index MACD + MP"
            detail="Saved Strategy 2 index lanes, Market Profile replay state and recent ledger."
            meta={prettify(strategy2?.meta?.mode || "waiting")}
          />
          <div className="mt-3 grid gap-2 md:grid-cols-4">
            <MetricTile label="Equity" value={formatMoney(strategy2?.summary?.total_equity)} />
            <MetricTile label="Trades" value={String(strategy2?.summary?.total_trades || 0)} />
            <MetricTile label="Realized" value={formatMoney(strategy2?.summary?.realized_pnl)} tone={pnlTone(strategy2?.summary?.realized_pnl)} />
            <BucketBar rows={strategy2Signals} />
          </div>
          <div className="mt-3 overflow-hidden rounded-lg border border-bg-border">
            <table className="w-full min-w-[920px] text-xs">
              <thead className="bg-bg-primary/60 text-text-muted">
                <tr>
                  <th className="px-3 py-2 text-left">Lane</th>
                  <th className="px-3 py-2 text-left">Signal</th>
                  <th className="px-3 py-2 text-right">Spot</th>
                  <th className="px-3 py-2 text-right">POC</th>
                  <th className="px-3 py-2 text-right">VAH / VAL</th>
                  <th className="px-3 py-2 text-left">Freshness</th>
                  <th className="px-3 py-2 text-left">Last Bar</th>
                </tr>
              </thead>
              <tbody>
                {strategy2Signals.map((row) => (
                  <tr key={`${row.underlying}-${row.status}-${row.option_last_bar_time}`} className="border-t border-bg-border/60">
                    <td className="px-3 py-2 font-semibold text-text-primary">{row.underlying || "--"}</td>
                    <td className="px-3 py-2">
                      <div className="flex flex-wrap gap-1">
                        {row.direction ? <StatusBadge label={row.direction} tone={row.direction} /> : null}
                        <StatusBadge label={prettify(row.status)} tone={row.status} />
                      </div>
                      <div className="mt-1 truncate text-[11px] text-text-muted" title={row.reason || row.instruction || undefined}>{row.reason || row.instruction || "--"}</div>
                    </td>
                    <td className="px-3 py-2 text-right font-mono">{formatNumber(row.spot_price)}</td>
                    <td className="px-3 py-2 text-right font-mono">{formatNumber(row.poc)}</td>
                    <td className="px-3 py-2 text-right font-mono">{formatNumber(row.vah)} / {formatNumber(row.val)}</td>
                    <td className="px-3 py-2"><StatusBadge label={prettify(row.freshness)} tone={row.freshness} /></td>
                    <td className="px-3 py-2 text-text-muted">{formatTimestamp(row.option_last_bar_time || row.spot_last_time || row.as_of)}</td>
                  </tr>
                ))}
                {!strategy2Signals.length ? (
                  <tr><td colSpan={7} className="px-3 py-6 text-center text-text-muted">No saved Strategy 2 lanes available.</td></tr>
                ) : null}
              </tbody>
            </table>
          </div>
        </div>
      </section>

      <section className="grid gap-3 xl:grid-cols-[1.4fr_0.8fr]">
        <div className="rounded-xl border border-bg-border bg-bg-secondary/20 p-3">
          <PanelHeader
            icon={<Database size={16} className="text-accent-amber" />}
            title="Strategy 1 Prepared CE/PE List"
            detail="Saved candidate list. It is not regenerated from broker feeds while brokers are disconnected."
            meta={`${strategy1Prepared.length} rows`}
          />
          <div className="mt-3 max-h-[52vh] overflow-auto rounded-lg border border-bg-border">
            <table className="w-full min-w-[1180px] text-xs">
              <thead className="sticky top-0 bg-bg-primary/95 text-text-muted">
                <tr>
                  <th className="px-3 py-2 text-left">Underlying</th>
                  <th className="px-3 py-2 text-left">Side</th>
                  <th className="px-3 py-2 text-right">Score</th>
                  <th className="px-3 py-2 text-right">Spot</th>
                  <th className="px-3 py-2 text-right">ATM</th>
                  <th className="px-3 py-2 text-right">LTP</th>
                  <th className="px-3 py-2 text-right">IV</th>
                  <th className="px-3 py-2 text-left">Reason</th>
                  <th className="px-3 py-2 text-left">Saved</th>
                </tr>
              </thead>
              <tbody>
                {strategy1Prepared.map((row) => (
                  <tr key={`${row.underlying}-${row.direction}-${row.priority_score}-${row.as_of}`} className="border-t border-bg-border/60">
                    <td className="px-3 py-2 font-semibold text-text-primary">{row.underlying || "--"}</td>
                    <td className="px-3 py-2"><StatusBadge label={row.direction || "--"} tone={row.direction} /></td>
                    <td className="px-3 py-2 text-right font-mono">{formatNumber(row.priority_score, 2)}</td>
                    <td className="px-3 py-2 text-right font-mono">{formatNumber(row.spot_price)}</td>
                    <td className="px-3 py-2 text-right font-mono">{formatNumber(row.atm_strike, 0)}</td>
                    <td className="px-3 py-2 text-right font-mono">{formatNumber(row.ltp)}</td>
                    <td className="px-3 py-2 text-right font-mono">{formatNumber(row.iv_pct, 1)}%</td>
                    <td className="px-3 py-2 text-text-secondary" title={row.instruction || undefined}>{row.reason || "--"}</td>
                    <td className="px-3 py-2 text-text-muted">{formatTimestamp(row.option_last_bar_time || row.as_of)}</td>
                  </tr>
                ))}
                {!strategy1Prepared.length ? (
                  <tr><td colSpan={9} className="px-3 py-8 text-center text-text-muted">No saved Strategy 1 prepared list returned by the API.</td></tr>
                ) : null}
              </tbody>
            </table>
          </div>
        </div>

        <div className="space-y-3">
          <div className="rounded-xl border border-bg-border bg-bg-secondary/20 p-3">
            <PanelHeader
              icon={<Shield size={16} className="text-accent-green" />}
              title="Persistence Check"
              detail="Confirms the page is reading saved state and not depending on holiday broker sessions."
            />
            <div className="mt-3 grid gap-2">
              <MetricTile label="Market Mode" value={prettify(strategy1?.meta?.market_state || strategy2?.meta?.market_state || "unknown")} />
              <MetricTile label="Readiness" value={marketHealth?.ready ? "Saved Ready" : "Not Ready"} detail={prettify(marketHealth?.readiness_mode || marketHealth?.execution_mode)} tone={marketHealth?.ready ? "text-accent-green" : "text-accent-red"} />
              <MetricTile label="Latest Session" value={String(marketHealth?.watchlist_rows_latest || 0)} detail={formatTimestamp(marketHealth?.latest_watchlist_time)} />
              <MetricTile label="Today Rows" value={String(marketHealth?.watchlist_rows_today || 0)} detail="Holiday/offline rows can be zero" />
            </div>
          </div>

          <div className="rounded-xl border border-bg-border bg-bg-secondary/20 p-3">
            <PanelHeader
              icon={<Database size={16} className="text-accent-blue" />}
              title="Recent Strategy 2 Ledger"
              detail="Recent saved Strategy 2 closed trades."
              meta={`${strategy2?.trade_history?.length || 0} trades`}
            />
            <div className="mt-3 space-y-2">
              {(strategy2?.trade_history || []).slice(0, 5).map((trade) => (
                <div key={`${trade.symbol}-${trade.exit_time}`} className="rounded-lg border border-bg-border bg-bg-primary/25 px-3 py-2 text-xs">
                  <div className="flex items-center justify-between gap-3">
                    <div className="min-w-0">
                      <div className="truncate font-semibold text-text-primary">{trade.symbol || "--"}</div>
                      <div className="mt-1 text-[11px] text-text-muted">{formatTimestamp(trade.exit_time || trade.entry_time)} · {trade.qty || "--"} qty</div>
                    </div>
                    <div className={clsx("font-mono font-semibold", pnlTone(trade.pnl))}>{formatMoney(trade.pnl)}</div>
                  </div>
                </div>
              ))}
              {!strategy2?.trade_history?.length ? <div className="text-xs text-text-muted">No saved Strategy 2 closed trades.</div> : null}
            </div>
          </div>
        </div>
      </section>
    </div>
  );
}

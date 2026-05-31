"use client";

import { useMemo, useState } from "react";
import { clsx } from "clsx";

type StrategyAuditBucket = "conditions_met_traded" | "favourable_tracking" | "drifting_away";

type StrategyAuditRow = {
  kind?: string | null;
  source?: string | null;
  underlying?: string | null;
  symbol?: string | null;
  direction?: string | null;
  status?: string | null;
  reason?: string | null;
  freshness?: string | null;
  instruction?: string | null;
  as_of?: string | null;
  expiry?: string | null;
  strike?: number | null;
  ltp?: number | null;
  entry_price?: number | null;
  exit_price?: number | null;
  qty?: number | null;
  pnl?: number | null;
  return_pct?: number | null;
  iv_pct?: number | null;
  macd?: number | null;
  previous_macd?: number | null;
  macd_histogram?: number | null;
  rsi?: number | null;
  priority_score?: number | null;
};

export type StrategyAgentStatus = {
  running: boolean;
  enabled?: boolean;
  auto_run_enabled?: boolean;
  kill_switch_active?: boolean;
  scan_interval_seconds?: number | null;
  last_run_at?: string | null;
  last_message?: string | null;
  last_error?: string | null;
  target_expiry?: string | null;
  candidate_expiries?: string[];
  next_scan_at?: string | null;
  telegram?: {
    enabled: boolean;
    configured: boolean;
    report_interval?: string | null;
    last_sent_at?: string | null;
  };
  commentary?: Array<{
    time: string;
    scope: string;
    tone: string;
    message: string;
  }>;
  data_health?: {
    broker_snapshot?: {
      connected_brokers?: string[];
      broker_ready?: boolean;
      upstox_ready?: boolean;
      fyers_ready?: boolean;
      upstox_token_health?: BrokerTokenHealth;
      fyers_token_health?: BrokerTokenHealth;
    };
    fyers_token_health?: BrokerTokenHealth;
    option_history?: OptionHistoryHealth;
  };
  strategy_agents?: Array<{
    key: string;
    label: string;
    timeframe?: string | null;
    instrument_scope?: string | null;
    execution_mode?: string | null;
    position_cap?: number | null;
    last_scan_at?: string | null;
    last_message?: string | null;
    open_positions?: number | null;
    signals?: number | null;
    mode?: string | null;
  }>;
  strategies: Array<{
    key: string;
    label: string;
    agent?: {
      key?: string;
      label?: string;
      timeframe?: string | null;
      scope?: string | null;
      scan_interval_seconds?: number | null;
      position_cap?: number | null;
      execution_mode?: string | null;
    } | null;
    last_scan_at?: string | null;
    last_message?: string | null;
    signals?: Array<{
      underlying: string;
      direction?: string | null;
      status?: string | null;
      reason?: string | null;
      freshness?: string | null;
      instruction?: string | null;
      mp_day_type?: string | null;
      source?: string | null;
      as_of?: string | null;
      expiry?: string | null;
      strike?: number | null;
      atm_strike?: number | null;
      ltp?: number | null;
      iv_pct?: number | null;
      macd?: number | null;
      previous_macd?: number | null;
      macd_histogram?: number | null;
      rsi?: number | null;
      priority_score?: number | null;
    }>;
    audit_lanes?: Partial<Record<StrategyAuditBucket, StrategyAuditRow[]>>;
    meta?: {
      mode?: string | null;
      scan_interval?: string | null;
      watchlist_rows?: number | null;
      updated_at?: string | null;
      pipeline?: Array<{
        name: string;
        status: string;
        rows: number;
        last_date: string;
        detail: string;
        freshness?: string | null;
      }>;
    };
    summary: {
      total_equity?: number | null;
      realized_pnl?: number | null;
      unrealized_pnl?: number | null;
      total_trades?: number | null;
      win_rate?: number | null;
      open_positions?: number | null;
      entries?: number | null;
      exits?: number | null;
    };
    positions: Array<{
      symbol: string;
      underlying: string;
      expiry?: string | null;
      option_type: string;
      strike: number;
      qty: number;
      entry_price: number;
      current_price: number;
      phase?: string | null;
      trailing_stop?: number | null;
      peak_price?: number | null;
      entry_iv_pct?: number | null;
      spot_setup?: string | null;
      regime?: string | null;
      latest_rsi?: number | null;
      signal_reason: string;
      entered_at?: string | null;
      price_updated_at?: string | null;
      unrealized_pnl?: number | null;
      return_pct?: number | null;
    }>;
    recent_events: Array<{
      time: string;
      event: string;
      underlying: string;
      option_type: string;
      strike: number;
      qty: number;
      price: number;
      reason: string;
      pnl?: number | null;
    }>;
    trade_history?: Array<{
      symbol: string;
      action: string;
      qty: number;
      entry_price: number;
      exit_price: number;
      pnl: number;
      entry_time: string;
      exit_time: string;
      expiry?: string | null;
      strike?: number | null;
      option_type?: string | null;
    }>;
  }>;
};

type BrokerTokenHealth = {
  connected?: boolean;
  valid?: boolean;
  status?: string | null;
  checked_at?: string | null;
  has_saved_token?: boolean;
  source?: string | null;
  needs_reconnect?: boolean;
  message?: string | null;
  expires_at_ist?: string | null;
};

type OptionHistoryHealth = {
  failure_count?: number;
  success_count?: number;
  brokers?: Record<
    string,
    {
      success?: number;
      failure?: number;
      last_status?: string | null;
      last_detail?: string | null;
    }
  >;
};

export function formatSigned(value?: number | null, digits = 2, suffix = "") {
  if (value == null || Number.isNaN(value)) return "--";
  const prefix = value > 0 ? "+" : "";
  return `${prefix}${value.toFixed(digits)}${suffix}`;
}

function formatTimestamp(value?: string | null) {
  if (!value) return "--";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString();
}

function prettifyToken(value?: string | null) {
  if (!value) return "--";
  return value.replaceAll("_", " ");
}

function prettifyAuditLabel(value?: string | null) {
  if (!value) return "--";
  return value.replaceAll("_", " ").replaceAll("-", " ");
}

function signalBucket(signal: StrategyAuditRow): StrategyAuditBucket {
  const status = String(signal.status || "").toLowerCase().replaceAll("_", "-");
  const freshness = String(signal.freshness || "").toLowerCase();
  if (["active", "open", "entry-ready", "candidate", "filled"].includes(status)) {
    return "conditions_met_traded";
  }
  if (["trend-aligned", "watching", "monitoring", "research-only"].includes(status) && !["missing", "stale"].includes(freshness)) {
    return "favourable_tracking";
  }
  return "drifting_away";
}

function buildFallbackAuditLanes(strategy: StrategyAgentStatus["strategies"][number]): Record<StrategyAuditBucket, StrategyAuditRow[]> {
  const lanes: Record<StrategyAuditBucket, StrategyAuditRow[]> = {
    conditions_met_traded: [],
    favourable_tracking: [],
    drifting_away: [],
  };

  for (const position of strategy.positions || []) {
    lanes.conditions_met_traded.push({
      kind: "position",
      source: "open_position",
      underlying: position.underlying,
      symbol: position.symbol,
      direction: position.option_type,
      status: `open:${position.phase || "phase1"}`,
      reason: position.signal_reason,
      freshness: "live",
      instruction: `${position.underlying} ${position.option_type} ${position.strike} entry ${position.entry_price.toFixed(2)}, last ${position.current_price.toFixed(2)}, return ${formatSigned(position.return_pct, 2, "%")}`,
      as_of: position.price_updated_at || position.entered_at,
      expiry: position.expiry,
      strike: position.strike,
      ltp: position.current_price,
      entry_price: position.entry_price,
      qty: position.qty,
      pnl: position.unrealized_pnl,
      return_pct: position.return_pct,
      iv_pct: position.entry_iv_pct,
      rsi: position.latest_rsi,
    });
  }

  for (const trade of strategy.trade_history || []) {
    lanes.conditions_met_traded.push({
      kind: "trade",
      source: "closed_trade",
      underlying: trade.symbol?.split(":")[1] || trade.symbol,
      symbol: trade.symbol,
      direction: trade.option_type,
      status: "closed",
      reason: trade.action,
      freshness: "ledger",
      instruction: `Closed ${trade.qty} @ ${trade.exit_price?.toFixed(2)}; P&L ${formatSigned(trade.pnl, 2)}`,
      as_of: trade.exit_time,
      expiry: trade.expiry,
      strike: trade.strike,
      entry_price: trade.entry_price,
      exit_price: trade.exit_price,
      qty: trade.qty,
      pnl: trade.pnl,
    });
  }

  for (const signal of strategy.signals || []) {
    const row: StrategyAuditRow = {
      kind: "signal",
      source: signal.source || "signal_lane",
      underlying: signal.underlying,
      symbol: signal.underlying,
      direction: signal.direction,
      status: signal.status,
      reason: signal.reason,
      freshness: signal.freshness,
      instruction: signal.instruction,
      as_of: signal.as_of,
      expiry: signal.expiry,
      strike: signal.strike ?? signal.atm_strike,
      ltp: signal.ltp,
      iv_pct: signal.iv_pct,
      macd: signal.macd,
      previous_macd: signal.previous_macd,
      macd_histogram: signal.macd_histogram,
      rsi: signal.rsi,
      priority_score: signal.priority_score,
    };
    lanes[signalBucket(row)].push(row);
  }

  return lanes;
}

function auditLanesForStrategy(strategy: StrategyAgentStatus["strategies"][number]): Record<StrategyAuditBucket, StrategyAuditRow[]> {
  const fallback = buildFallbackAuditLanes(strategy);
  const apiLanes = strategy.audit_lanes || {};
  return {
    conditions_met_traded: apiLanes.conditions_met_traded?.length ? apiLanes.conditions_met_traded : fallback.conditions_met_traded,
    favourable_tracking: apiLanes.favourable_tracking?.length ? apiLanes.favourable_tracking : fallback.favourable_tracking,
    drifting_away: apiLanes.drifting_away?.length ? apiLanes.drifting_away : fallback.drifting_away,
  };
}

function tokenTone(value?: string | null, valid?: boolean) {
  if (valid) return "bg-accent-green/15 text-accent-green";
  if (value === "missing" || value === "expired_reconnect_required") {
    return "bg-accent-red/15 text-accent-red";
  }
  if (value === "valid_no_refresh" || value === "valid_session") {
    return "bg-accent-green/15 text-accent-green";
  }
  return "bg-accent-amber/15 text-accent-amber";
}

function historyHealthSummary(health?: OptionHistoryHealth) {
  if (!health) {
    return {
      summary: "No option-history health reported yet.",
      detail: undefined as string | undefined,
      tone: "text-text-muted",
    };
  }
  const failures = Number(health.failure_count || 0);
  const successes = Number(health.success_count || 0);
  const brokerStates = Object.entries(health.brokers || {});
  const latestFailure = brokerStates
    .map(([broker, state]) => {
      const failureCount = Number(state.failure || 0);
      if (!failureCount) return null;
      return `${broker.toUpperCase()}: ${state.last_detail || "fetch failed"}`;
    })
    .filter(Boolean)
    .join(" | ");

  if (failures > 0) {
    return {
      summary: `History fetch warnings detected: ${failures} failure${failures === 1 ? "" : "s"}, ${successes} success${successes === 1 ? "" : "es"}.`,
      detail: latestFailure || undefined,
      tone: "text-accent-amber",
    };
  }
  if (successes > 0) {
    return {
      summary: `History fetches healthy: ${successes} successful broker request${successes === 1 ? "" : "s"}.`,
      detail: undefined,
      tone: "text-accent-green",
    };
  }
  return {
    summary: "No option-history calls have completed in this runtime yet.",
    detail: undefined,
    tone: "text-text-muted",
  };
}

function BrokerHealthStrip({ agentStatus }: { agentStatus?: StrategyAgentStatus }) {
  const brokerSnapshot = agentStatus?.data_health?.broker_snapshot;
  const fyersHealth = brokerSnapshot?.fyers_token_health || agentStatus?.data_health?.fyers_token_health;
  const upstoxHealth = brokerSnapshot?.upstox_token_health;
  const historyHealth = historyHealthSummary(agentStatus?.data_health?.option_history);

  return (
    <div className="mt-4 rounded border border-bg-border bg-bg-secondary/25 p-3">
      <div className="flex flex-wrap items-center gap-2 text-[11px] uppercase tracking-[0.14em] text-text-muted">
        <span>Data Health</span>
        <span
          className={clsx(
            "rounded px-2 py-1 font-semibold",
            brokerSnapshot?.broker_ready ? "bg-accent-green/15 text-accent-green" : "bg-accent-red/15 text-accent-red",
          )}
        >
          {brokerSnapshot?.broker_ready ? "Broker Ready" : "Broker Blocked"}
        </span>
      </div>

      <div className="mt-3 flex flex-wrap gap-2 text-xs">
        {upstoxHealth ? (
          <span className={clsx("rounded px-2 py-1 font-semibold", tokenTone(upstoxHealth.status, upstoxHealth.valid))}>
            UPSTOX {prettifyToken(upstoxHealth.status)}
          </span>
        ) : null}
        {fyersHealth ? (
          <span className={clsx("rounded px-2 py-1 font-semibold", tokenTone(fyersHealth.status, fyersHealth.valid))}>
            FYERS {prettifyToken(fyersHealth.status)}
          </span>
        ) : null}
        {brokerSnapshot?.connected_brokers?.length ? (
          <span className="rounded bg-bg-primary/60 px-2 py-1 text-text-muted">
            Connected {brokerSnapshot.connected_brokers.map((broker) => broker.toUpperCase()).join(" + ")}
          </span>
        ) : null}
      </div>

      <div className={clsx("mt-3 text-sm", historyHealth.tone)}>{historyHealth.summary}</div>
      {historyHealth.detail ? (
        <div className="mt-1 text-xs text-text-muted">{historyHealth.detail}</div>
      ) : null}

      {(upstoxHealth?.message || fyersHealth?.message) ? (
        <div className="mt-3 grid gap-2 md:grid-cols-2 text-xs text-text-muted">
          {upstoxHealth?.message ? (
            <div className="rounded border border-bg-border bg-bg-primary/40 px-3 py-2">
              <div className="font-semibold text-text-primary">Upstox</div>
              <div className="mt-1">{upstoxHealth.message}</div>
            </div>
          ) : null}
          {fyersHealth?.message ? (
            <div className="rounded border border-bg-border bg-bg-primary/40 px-3 py-2">
              <div className="font-semibold text-text-primary">Fyers</div>
              <div className="mt-1">{fyersHealth.message}</div>
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

export function StrategyStatusPanel({ agentStatus }: { agentStatus?: StrategyAgentStatus }) {
  const autoRunEnabled = agentStatus?.auto_run_enabled ?? agentStatus?.enabled ?? false;

  return (
    <div className="card p-4">
      <div className="flex flex-wrap items-center gap-3 text-xs">
        <span
          className={clsx(
            "rounded px-2 py-1 font-semibold",
            agentStatus?.running ? "bg-accent-blue/15 text-accent-blue" : "bg-bg-secondary text-text-muted"
          )}
        >
          {agentStatus?.running ? "Running" : "Idle"}
        </span>
        <span className="text-text-muted">
          {autoRunEnabled ? "Automatic scan loop enabled" : "Manual scan only"}
        </span>
        <span className={clsx("rounded px-2 py-1 font-semibold", agentStatus?.kill_switch_active ? "bg-accent-red/15 text-accent-red" : "bg-accent-green/15 text-accent-green")}>
          {agentStatus?.kill_switch_active ? "Kill Switch Active" : "Kill Switch Released"}
        </span>
        <span className="text-text-muted">Scan every {agentStatus?.scan_interval_seconds || 60}s</span>
        <span className="text-text-muted">Expiry {agentStatus?.target_expiry || "--"}</span>
        <span className="text-text-muted">
          Candidates {(agentStatus?.candidate_expiries?.length ? agentStatus.candidate_expiries.join(", ") : agentStatus?.target_expiry || "--")}
        </span>
        <span className="text-text-muted">Last run {formatTimestamp(agentStatus?.last_run_at)}</span>
        <span className="text-text-muted">Next scan {formatTimestamp(agentStatus?.next_scan_at)}</span>
        <span className="text-text-muted">
          Telegram {agentStatus?.telegram?.enabled ? `${agentStatus.telegram.report_interval || "1h"} enabled` : "disabled"}
        </span>
      </div>
      <div className={clsx("mt-3 text-sm", agentStatus?.last_error ? "text-accent-red" : "text-text-secondary")}>
        {agentStatus?.last_error || agentStatus?.last_message || "Waiting for strategy state…"}
      </div>
      <BrokerHealthStrip agentStatus={agentStatus} />
    </div>
  );
}

export function AgentCommentaryFeed({ agentStatus }: { agentStatus?: StrategyAgentStatus }) {
  const items = agentStatus?.commentary || [];

  return (
    <div className="card p-4 flex flex-col">
      <div className="flex items-center justify-between gap-3">
        <div>
          <h2 className="text-sm font-semibold text-text-primary">Agent Commentary</h2>
          <div className="text-xs text-text-muted">Live English notes on what the agent is observing and why it is trading, exiting, or standing aside.</div>
        </div>
        <div className="text-xs text-text-muted">{items.length} recent notes</div>
      </div>

      <div className="mt-4 max-h-[360px] overflow-y-auto space-y-2 pr-1">
        {items.length ? (
          items.map((item, index) => (
            <div key={`${item.time}-${index}`} className="rounded border border-bg-border bg-bg-secondary/20 px-3 py-3 text-xs">
              <div className="flex items-center justify-between gap-3">
                <div className="flex items-center gap-2">
                  <span
                    className={clsx(
                      "rounded px-2 py-0.5 font-semibold",
                      item.tone === "trade" && "bg-accent-blue/15 text-accent-blue",
                      item.tone === "success" && "bg-accent-green/15 text-accent-green",
                      item.tone === "warning" && "bg-accent-amber/15 text-accent-amber",
                      item.tone === "error" && "bg-accent-red/15 text-accent-red",
                      item.tone === "idle" && "bg-bg-secondary text-text-muted",
                      !["trade", "success", "warning", "error", "idle"].includes(item.tone) && "bg-bg-secondary text-text-muted"
                    )}
                  >
                    {item.scope}
                  </span>
                </div>
                <span className="text-text-muted">{formatTimestamp(item.time)}</span>
              </div>
              <div className="mt-2 text-sm text-text-secondary">{item.message}</div>
            </div>
          ))
        ) : (
          <div className="rounded border border-dashed border-bg-border px-3 py-6 text-center text-xs text-text-muted">
            No agent commentary yet.
          </div>
        )}
      </div>
    </div>
  );
}

const AUDIT_TABS: Array<{ key: StrategyAuditBucket; label: string }> = [
  { key: "conditions_met_traded", label: "Met / Traded" },
  { key: "favourable_tracking", label: "Close Watch" },
  { key: "drifting_away", label: "Drifting" },
];

function fmtNumber(value?: number | null, digits = 2) {
  if (value == null || Number.isNaN(Number(value))) return "--";
  return Number(value).toFixed(digits);
}

function StrategyAuditTabs({ strategy }: { strategy: StrategyAgentStatus["strategies"][number] }) {
  const [activeTab, setActiveTab] = useState<StrategyAuditBucket>("conditions_met_traded");
  const lanes = useMemo(() => auditLanesForStrategy(strategy), [strategy]);
  const rows = lanes[activeTab] || [];

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-2">
        {AUDIT_TABS.map((tab) => (
          <button
            key={tab.key}
            type="button"
            onClick={() => setActiveTab(tab.key)}
            className={clsx(
              "rounded border px-3 py-2 text-left text-xs font-semibold transition",
              activeTab === tab.key
                ? "border-accent-blue bg-accent-blue/15 text-text-primary"
                : "border-bg-border bg-bg-secondary/20 text-text-muted hover:text-text-primary",
            )}
          >
            <span>{tab.label}</span>
            <span className="ml-2 font-mono text-[11px] text-text-muted">{lanes[tab.key]?.length || 0}</span>
          </button>
        ))}
      </div>

      {rows.length ? (
        <div className="overflow-x-auto rounded border border-bg-border">
          <table className="w-full min-w-[920px] text-xs">
            <thead className="bg-bg-secondary/40 text-text-muted">
              <tr>
                <th className="px-3 py-2 text-left">Instrument</th>
                <th className="px-3 py-2 text-left">State</th>
                <th className="px-3 py-2 text-right">LTP</th>
                <th className="px-3 py-2 text-right">MACD</th>
                <th className="px-3 py-2 text-right">RSI</th>
                <th className="px-3 py-2 text-right">IV</th>
                <th className="px-3 py-2 text-right">P&L</th>
                <th className="px-3 py-2 text-left">Trace</th>
              </tr>
            </thead>
            <tbody>
              {rows.slice(0, 12).map((row, index) => (
                <tr key={`${row.kind}-${row.symbol}-${row.as_of}-${index}`} className="border-t border-bg-border/60 align-top">
                  <td className="px-3 py-2">
                    <div className="font-semibold text-text-primary">
                      {row.underlying || row.symbol || "--"} {row.direction || ""}
                    </div>
                    <div className="mt-1 font-mono text-[11px] text-text-muted">
                      {row.expiry || "--"} {row.strike != null ? ` | ${fmtNumber(row.strike, 0)}` : ""}
                    </div>
                  </td>
                  <td className="px-3 py-2">
                    <div className="text-text-primary">{prettifyAuditLabel(row.status)}</div>
                    <div className="mt-1 text-text-muted">{prettifyAuditLabel(row.reason)}</div>
                  </td>
                  <td className="px-3 py-2 text-right font-mono text-text-primary">{fmtNumber(row.ltp ?? row.exit_price ?? row.entry_price)}</td>
                  <td className="px-3 py-2 text-right font-mono text-text-primary">
                    {fmtNumber(row.macd, 4)}
                    {row.previous_macd != null ? <div className="text-[11px] text-text-muted">prev {fmtNumber(row.previous_macd, 4)}</div> : null}
                  </td>
                  <td className="px-3 py-2 text-right font-mono text-text-primary">{fmtNumber(row.rsi, 1)}</td>
                  <td className="px-3 py-2 text-right font-mono text-text-primary">{fmtNumber(row.iv_pct, 1)}</td>
                  <td className={clsx("px-3 py-2 text-right font-mono", (row.pnl || 0) >= 0 ? "text-accent-green" : "text-accent-red")}>
                    {row.pnl != null ? formatSigned(row.pnl, 2) : row.return_pct != null ? formatSigned(row.return_pct, 2, "%") : "--"}
                  </td>
                  <td className="px-3 py-2">
                    <div className="max-w-[340px] text-text-muted">{row.instruction || prettifyAuditLabel(row.source)}</div>
                    <div className="mt-1 font-mono text-[11px] text-text-muted">{formatTimestamp(row.as_of)}</div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="rounded border border-dashed border-bg-border px-3 py-5 text-xs text-text-muted text-center">
          No rows in this lane.
        </div>
      )}
    </div>
  );
}

export function StrategyCard({ strategy }: { strategy: StrategyAgentStatus["strategies"][number] }) {
  const tradeHistory = strategy.trade_history || [];
  const signals = strategy.signals || [];

  return (
    <div className="card p-4 space-y-3">
      <div className="flex items-center justify-between gap-3">
        <div>
          <h2 className="text-sm font-semibold text-text-primary">{strategy.label}</h2>
          <div className="text-xs text-text-muted">
            {strategy.summary.total_trades || 0} trades | {strategy.summary.open_positions || 0} open
          </div>
        </div>
        <div
          className={clsx(
            "rounded px-2 py-1 text-xs font-semibold",
            (strategy.summary.realized_pnl || 0) >= 0 ? "bg-accent-green/15 text-accent-green" : "bg-accent-red/15 text-accent-red"
          )}
        >
          {formatSigned(strategy.summary.realized_pnl, 2)}
        </div>
      </div>

      <div className="grid grid-cols-2 gap-2 text-xs">
        <div className="rounded border border-bg-border bg-bg-secondary/40 p-2">
          <div className="text-text-muted">Equity</div>
          <div className="mt-1 font-mono text-sm text-text-primary">{strategy.summary.total_equity?.toFixed(2) || "--"}</div>
        </div>
        <div className="rounded border border-bg-border bg-bg-secondary/40 p-2">
          <div className="text-text-muted">Unrealized</div>
          <div className={clsx("mt-1 font-mono text-sm", (strategy.summary.unrealized_pnl || 0) >= 0 ? "text-accent-green" : "text-accent-red")}>
            {formatSigned(strategy.summary.unrealized_pnl, 2)}
          </div>
        </div>
        <div className="rounded border border-bg-border bg-bg-secondary/40 p-2">
          <div className="text-text-muted">Win Rate</div>
          <div className="mt-1 font-mono text-sm text-text-primary">
            {strategy.summary.win_rate != null ? `${(strategy.summary.win_rate * 100).toFixed(1)}%` : "--"}
          </div>
        </div>
        <div className="rounded border border-bg-border bg-bg-secondary/40 p-2">
          <div className="text-text-muted">Entries / Exits</div>
          <div className="mt-1 font-mono text-sm text-text-primary">
            {strategy.summary.entries || 0} / {strategy.summary.exits || 0}
          </div>
        </div>
      </div>

      <div className="rounded border border-bg-border bg-bg-secondary/20 px-3 py-2 text-xs text-text-muted">
        <span>Last scan {formatTimestamp(strategy.last_scan_at)}</span>
        <span className="mx-2">|</span>
        <span>{strategy.last_message || "Waiting for first scan."}</span>
      </div>

      <div className="space-y-2">
        <div className="text-xs font-semibold text-text-secondary uppercase tracking-wide">Actionability Audit</div>
        <StrategyAuditTabs strategy={strategy} />
      </div>

      <div className="space-y-2">
        <div className="text-xs font-semibold text-text-secondary uppercase tracking-wide">Open Positions</div>
        {strategy.positions.length ? (
          strategy.positions.map((position) => (
            <div key={position.symbol} className="rounded border border-bg-border bg-bg-secondary/30 p-3 text-xs">
              <div className="flex items-center justify-between gap-3">
                <div className="font-semibold text-text-primary">
                  {position.underlying} {position.option_type} {position.strike}
                </div>
                <div className={clsx("font-mono", (position.unrealized_pnl || 0) >= 0 ? "text-accent-green" : "text-accent-red")}>
                  {formatSigned(position.unrealized_pnl, 2)}
                </div>
              </div>
              <div className="mt-2 grid grid-cols-2 gap-2 text-text-muted">
                <div>Entry {position.entry_price.toFixed(2)} | Last {position.current_price.toFixed(2)}</div>
                <div>Qty {position.qty} | RSI {position.latest_rsi?.toFixed(1) || "--"}</div>
                <div>Signal {position.signal_reason}</div>
                <div>Trail {position.trailing_stop?.toFixed(2) || "--"} | Return {formatSigned(position.return_pct, 2, "%")}</div>
              </div>
            </div>
          ))
        ) : (
          <div className="rounded border border-dashed border-bg-border px-3 py-5 text-xs text-text-muted text-center">
            No open positions.
          </div>
        )}
      </div>

      {signals.length ? (
        <div className="space-y-2">
          <div className="text-xs font-semibold text-text-secondary uppercase tracking-wide">Signal Lane</div>
          {signals.slice(0, 4).map((signal, index) => (
            <div key={`${signal.underlying}-${index}`} className="rounded border border-bg-border bg-bg-secondary/20 px-3 py-2 text-xs">
              <div className="flex items-center gap-2">
                <span className="font-semibold text-text-primary">{signal.underlying}</span>
                {signal.direction ? (
                  <span
                    className={clsx(
                      "rounded px-2 py-0.5 font-semibold",
                      signal.direction === "CE" ? "bg-accent-green/15 text-accent-green" : "bg-accent-red/15 text-accent-red",
                    )}
                  >
                    {signal.direction}
                  </span>
                ) : null}
                <span className="rounded bg-bg-secondary px-2 py-0.5 text-text-muted">{signal.status || "waiting"}</span>
                <span className="ml-auto text-text-muted">{signal.freshness || "--"}</span>
              </div>
              <div className="mt-2 text-text-muted">{signal.instruction || signal.reason || "--"}</div>
            </div>
          ))}
        </div>
      ) : null}

      <div className="space-y-2">
        <div className="text-xs font-semibold text-text-secondary uppercase tracking-wide">Trade History</div>
        {tradeHistory.length ? (
          <div className="overflow-x-auto rounded border border-bg-border">
            <table className="w-full text-xs font-mono">
              <thead className="bg-bg-secondary/40 text-text-muted">
                <tr>
                  <th className="px-3 py-2 text-left">Contract</th>
                  <th className="px-3 py-2 text-right">Qty</th>
                  <th className="px-3 py-2 text-right">Entry</th>
                  <th className="px-3 py-2 text-right">Exit</th>
                  <th className="px-3 py-2 text-right">P&L</th>
                  <th className="px-3 py-2 text-right">Exit Time</th>
                </tr>
              </thead>
              <tbody>
                {tradeHistory.slice(0, 6).map((trade, index) => (
                  <tr key={`${trade.symbol}-${trade.exit_time}-${index}`} className="border-t border-bg-border/60">
                    <td className="px-3 py-2 text-text-primary">
                      {trade.symbol?.split(":").slice(1).join(" ") || trade.symbol}
                    </td>
                    <td className="px-3 py-2 text-right text-text-primary">{trade.qty}</td>
                    <td className="px-3 py-2 text-right text-text-primary">{trade.entry_price?.toFixed(2)}</td>
                    <td className="px-3 py-2 text-right text-text-primary">{trade.exit_price?.toFixed(2)}</td>
                    <td className={clsx("px-3 py-2 text-right font-semibold", (trade.pnl || 0) >= 0 ? "text-accent-green" : "text-accent-red")}>
                      {formatSigned(trade.pnl, 2)}
                    </td>
                    <td className="px-3 py-2 text-right text-text-muted">{formatTimestamp(trade.exit_time)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="rounded border border-dashed border-bg-border px-3 py-5 text-xs text-text-muted text-center">
            No closed trades yet.
          </div>
        )}
      </div>

      <div className="space-y-2">
        <div className="text-xs font-semibold text-text-secondary uppercase tracking-wide">Recent Activity</div>
        {strategy.recent_events.length ? (
          strategy.recent_events.slice(0, 5).map((event, index) => (
            <div key={`${event.time}-${index}`} className="flex items-center justify-between gap-3 rounded border border-bg-border bg-bg-secondary/20 px-3 py-2 text-xs">
              <div>
                <div className="text-text-primary">
                  {event.event.toUpperCase()} {event.underlying} {event.option_type} {event.strike}
                </div>
                <div className="text-text-muted">{event.reason}</div>
              </div>
              <div className="text-right">
                <div className="font-mono text-text-primary">{event.price.toFixed(2)}</div>
                <div className={clsx("font-mono", (event.pnl || 0) >= 0 ? "text-accent-green" : "text-accent-red")}>
                  {event.pnl != null ? formatSigned(event.pnl, 2) : "--"}
                </div>
              </div>
            </div>
          ))
        ) : (
          <div className="rounded border border-dashed border-bg-border px-3 py-5 text-xs text-text-muted text-center">
            No activity yet.
          </div>
        )}
      </div>
    </div>
  );
}

export function StrategyMonitorSection({ agentStatus }: { agentStatus?: StrategyAgentStatus }) {
  const strategies = agentStatus?.strategies || [];

  return (
    <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
      {strategies.length ? (
        strategies.map((strategy) => <StrategyCard key={strategy.key} strategy={strategy} />)
      ) : (
        <div className="card p-6 text-sm text-text-muted">Strategy monitor is not available yet.</div>
      )}
    </div>
  );
}

"use client";

import { clsx } from "clsx";

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
  strategies: Array<{
    key: string;
    label: string;
    last_scan_at?: string | null;
    last_message?: string | null;
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
      option_type: string;
      strike: number;
      qty: number;
      entry_price: number;
      current_price: number;
      trailing_stop?: number | null;
      latest_rsi?: number | null;
      signal_reason: string;
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
    </div>
  );
}

export function AgentCommentaryFeed({ agentStatus }: { agentStatus?: StrategyAgentStatus }) {
  const items = agentStatus?.commentary || [];

  return (
    <div className="card p-4">
      <div className="flex items-center justify-between gap-3">
        <div>
          <h2 className="text-sm font-semibold text-text-primary">Agent Commentary</h2>
          <div className="text-xs text-text-muted">Live English notes on what the agent is observing and why it is trading, exiting, or standing aside.</div>
        </div>
        <div className="text-xs text-text-muted">{items.length} recent notes</div>
      </div>

      <div className="mt-4 space-y-2">
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

export function StrategyCard({ strategy }: { strategy: StrategyAgentStatus["strategies"][number] }) {
  const tradeHistory = strategy.trade_history || [];

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

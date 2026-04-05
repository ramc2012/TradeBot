"use client";

import { startTransition, useDeferredValue, useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { clsx } from "clsx";
import {
  Boxes,
  Play,
  RefreshCw,
  Save,
  ShieldAlert,
  ShieldCheck,
} from "lucide-react";

import {
  getCommodityATMWatchlist,
  getCommodityKillSwitchStatus,
  getCommodityOrders,
  getCommodityPositions,
  getCommodityReports,
  startCommodityStrategyAgent,
  getCommodityStrategyContracts,
  getCommodityStrategyStatus,
  updateCommodityKillSwitch,
  updateCommodityStrategyContracts,
  updateCommodityStrategyConfig,
} from "@/lib/api";

type KillSwitchState = {
  market: string;
  auto_run_enabled: boolean;
  kill_switch_active: boolean;
  loop_active?: boolean;
  start_required?: boolean;
  cancelled_orders?: number;
};

type CommodityPosition = {
  symbol: string;
  action: "BUY" | "SELL";
  qty: number;
  entry_price: number;
  current_price: number;
  stop_price: number;
  target_price: number;
  regime: string;
  signal_reason: string;
  atr?: number | null;
  entered_at: string;
  unrealized_pnl?: number | null;
  return_pct?: number | null;
};

type CommodityOrder = {
  time: string;
  order_id: string;
  symbol: string;
  action: "BUY" | "SELL";
  qty: number;
  order_type: string;
  status: string;
  fill_price?: number | null;
  reason: string;
};

type CommodityReport = {
  time: string;
  total_equity: number;
  realized_pnl: number;
  unrealized_pnl: number;
  open_positions: number;
  tracked_symbols: number;
  last_message: string;
};

type CommodityWatchRow = {
  symbol: string;
  price?: number | null;
  previous_close?: number | null;
  change_pct?: number | null;
  signal?: string | null;
  reason: string;
  regime: string;
  ema_fast?: number | null;
  ema_slow?: number | null;
  atr?: number | null;
  breakout_high?: number | null;
  breakout_low?: number | null;
  bar_time?: string | null;
};

type CommodityStatus = {
  enabled: boolean;
  auto_run_enabled?: boolean;
  kill_switch_active?: boolean;
  loop_active?: boolean;
  start_required?: boolean;
  running: boolean;
  scan_interval_seconds: number;
  last_run_at?: string | null;
  last_error?: string | null;
  last_message?: string | null;
  config: {
    symbols: string[];
    selected_option_expiries?: Record<string, string>;
    timeframe: string;
    fast_ema: number;
    slow_ema: number;
    atr_period: number;
    breakout_lookback: number;
    position_qty: number;
  };
  summary: {
    total_equity?: number | null;
    realized_pnl?: number | null;
    unrealized_pnl?: number | null;
    total_trades?: number | null;
    open_positions?: number | null;
    tracked_symbols?: number | null;
    open_orders?: number | null;
  };
  watchlist: CommodityWatchRow[];
  positions: CommodityPosition[];
  orders: CommodityOrder[];
  reports: CommodityReport[];
  commentary: Array<{
    time: string;
    tone: string;
    message: string;
  }>;
};

type ATMWatchlistOptionSide = {
  strike: number;
  option_type: "CE" | "PE";
  instrument_key?: string | null;
  trading_symbol?: string | null;
  ltp: number;
  prev_close?: number | null;
  change?: number | null;
  change_pct?: number | null;
  oi: number;
  prev_oi?: number | null;
  oi_change?: number | null;
  oi_change_pct?: number | null;
  volume: number;
  iv?: number | null;
  delta?: number | null;
  gamma?: number | null;
  theta?: number | null;
  vega?: number | null;
  macd?: number | null;
  macd_signal?: number | null;
  macd_histogram?: number | null;
  rsi?: number | null;
};

type ATMWatchlistPayload = {
  expiry: string | null;
  rows: Array<{
    underlying: string;
    symbol: string;
    lookup_symbol?: string | null;
    kind: string;
    spot_price: number;
    expiry: string;
    selected_expiry?: string | null;
    suggested_expiry?: string | null;
    active_expiry?: string | null;
    available_expiries?: string[];
    atm_strike: number;
    live_source: string;
    fyers_symbol?: string | null;
    ce?: ATMWatchlistOptionSide | null;
    pe?: ATMWatchlistOptionSide | null;
  }>;
  summary: {
    total_rows: number;
    ce_ready: number;
    pe_ready: number;
    tracked_symbols?: number;
    configured_contracts?: number;
  };
  source?: string;
  detail?: string | null;
  timestamp?: string;
};

type CommodityContractCatalogPayload = {
  contracts: Array<{
    symbol: string;
    underlying: string;
    lookup_symbol?: string | null;
    expiries: string[];
    selected_expiry?: string | null;
    suggested_expiry?: string | null;
    active_expiry?: string | null;
    has_options: boolean;
    detail?: string | null;
  }>;
  summary: {
    total_symbols: number;
    contracts_ready: number;
    active_selections: number;
  };
  source?: string;
  detail?: string | null;
  timestamp?: string;
};

type CommodityWorkspace = "setup" | "signals" | "atm" | "activity";

const EXAMPLE_SYMBOLS = [
  "MCX:GOLD26JUNFUT",
  "MCX:SILVERM26JUNFUT",
  "MCX:CRUDEOIL26MAYFUT",
  "MCX:NATURALGAS26MAYFUT",
];

function formatSigned(value?: number | null, digits = 2, suffix = "") {
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

function formatCompact(value?: number | null) {
  if (value == null || Number.isNaN(value)) return "--";
  if (Math.abs(value) >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (Math.abs(value) >= 1_000) return `${(value / 1_000).toFixed(1)}k`;
  return `${Math.round(value)}`;
}

function formatIv(value?: number | null) {
  if (value == null || Number.isNaN(value)) return "--";
  const normalized = value > 5 ? value : value * 100;
  return `${normalized.toFixed(1)}%`;
}

function formatIndicator(value?: number | null, digits = 2) {
  if (value == null || Number.isNaN(value)) return "--";
  const prefix = value > 0 ? "+" : "";
  return `${prefix}${value.toFixed(digits)}`;
}

function valueTone(value?: number | null) {
  if (value == null || Number.isNaN(value)) return "text-text-muted";
  if (value > 0) return "text-accent-green";
  if (value < 0) return "text-accent-red";
  return "text-text-secondary";
}

function macdTone(value?: number | null) {
  if (value == null || Number.isNaN(value)) return "text-text-muted";
  return value >= 0 ? "text-accent-green" : "text-accent-red";
}

function rsiTone(value?: number | null) {
  if (value == null || Number.isNaN(value)) return "text-text-muted";
  if (value >= 70) return "text-accent-red";
  if (value <= 30) return "text-accent-green";
  return "text-text-secondary";
}

function WorkspaceButton({
  active,
  label,
  description,
  onClick,
}: {
  active: boolean;
  label: string;
  description: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={clsx(
        "rounded-xl border px-3 py-2 text-left transition-colors",
        active
          ? "border-accent-blue bg-accent-blue/10 text-text-primary"
          : "border-bg-border bg-bg-secondary/45 text-text-muted hover:border-bg-active hover:text-text-primary",
      )}
    >
      <div className="text-xs font-semibold uppercase tracking-[0.12em]">{label}</div>
      <div className="mt-1 text-[11px]">{description}</div>
    </button>
  );
}

function ATMOptionCell({
  option,
  accent,
}: {
  option?: ATMWatchlistOptionSide | null;
  accent: "ce" | "pe";
}) {
  const ltpTone = accent === "ce" ? "text-accent-green" : "text-accent-red";

  if (!option) {
    return (
      <div className="grid grid-cols-5 gap-2 text-[11px] text-text-muted">
        <span className="col-span-5">No ATM contract</span>
      </div>
    );
  }

  return (
    <div className="grid grid-cols-5 gap-x-3 gap-y-1 text-[11px]">
      <div>
        <div className="text-text-muted">LTP</div>
        <div className={clsx("font-mono font-semibold", ltpTone)}>{option.ltp.toFixed(2)}</div>
      </div>
      <div>
        <div className="text-text-muted">Chg</div>
        <div className={valueTone(option.change_pct)}>{formatSigned(option.change_pct, 2, "%")}</div>
      </div>
      <div>
        <div className="text-text-muted">Vol</div>
        <div className="text-text-secondary">{formatCompact(option.volume)}</div>
      </div>
      <div>
        <div className="text-text-muted">OI</div>
        <div className="text-text-primary">{formatCompact(option.oi)}</div>
      </div>
      <div>
        <div className="text-text-muted">dOI</div>
        <div className={valueTone(option.oi_change)}>{formatCompact(option.oi_change)}</div>
      </div>
      <div>
        <div className="text-text-muted">IV</div>
        <div className="text-text-secondary">{formatIv(option.iv)}</div>
      </div>
      <div>
        <div className="text-text-muted">MACD</div>
        <div className={macdTone(option.macd_histogram)}>{formatIndicator(option.macd_histogram, 3)}</div>
      </div>
      <div>
        <div className="text-text-muted">RSI</div>
        <div className={rsiTone(option.rsi)}>{formatIndicator(option.rsi, 1)}</div>
      </div>
      <div>
        <div className="text-text-muted">Delta</div>
        <div className="text-text-secondary">{formatSigned(option.delta, 3)}</div>
      </div>
      <div>
        <div className="text-text-muted">Symbol</div>
        <div className="truncate text-text-muted" title={option.trading_symbol || option.instrument_key || undefined}>
          {option.trading_symbol || option.instrument_key || "--"}
        </div>
      </div>
    </div>
  );
}

export default function CommodityPage() {
  const qc = useQueryClient();
  const [workspace, setWorkspace] = useState<CommodityWorkspace>("signals");
  const [draftSymbols, setDraftSymbols] = useState("");
  const [hasEditedSymbols, setHasEditedSymbols] = useState(false);
  const [contractExpiryDrafts, setContractExpiryDrafts] = useState<Record<string, string>>({});
  const [hasEditedContractExpiries, setHasEditedContractExpiries] = useState(false);
  const deferredDraftSymbols = useDeferredValue(draftSymbols);

  const { data: status } = useQuery({
    queryKey: ["commodityStrategyStatus"],
    queryFn: () => getCommodityStrategyStatus().then((response) => response.data as CommodityStatus),
    refetchInterval: 5_000,
    staleTime: 10_000,
  });

  const { data: killSwitchState } = useQuery({
    queryKey: ["commodityKillSwitch"],
    queryFn: () => getCommodityKillSwitchStatus().then((response) => response.data as KillSwitchState),
    refetchInterval: 5_000,
    staleTime: 10_000,
  });

  const { data: orders } = useQuery({
    queryKey: ["commodityOrders"],
    queryFn: () => getCommodityOrders(25).then((response) => response.data as CommodityOrder[]),
    refetchInterval: 5_000,
  });

  const { data: positions } = useQuery({
    queryKey: ["commodityPositions"],
    queryFn: () => getCommodityPositions().then((response) => response.data as CommodityPosition[]),
    refetchInterval: 5_000,
  });

  const { data: reports } = useQuery({
    queryKey: ["commodityReports"],
    queryFn: () => getCommodityReports(20).then((response) => response.data as CommodityReport[]),
    refetchInterval: 15_000,
  });

  const contractCatalogQuery = useQuery<CommodityContractCatalogPayload>({
    queryKey: ["commodityStrategyContracts"],
    queryFn: () => getCommodityStrategyContracts().then((response) => response.data),
    enabled: workspace === "setup" || workspace === "atm",
    refetchInterval: 300_000,
    staleTime: 60_000,
  });

  const watchlistQuery = useQuery<ATMWatchlistPayload>({
    queryKey: ["commodityAtmWatchlist"],
    queryFn: () => getCommodityATMWatchlist().then((response) => response.data),
    enabled: workspace === "atm" && contractCatalogQuery.isSuccess,
    refetchInterval: workspace === "atm" ? 5_000 : false,
    staleTime: 5_000,
  });

  useEffect(() => {
    if (hasEditedSymbols) {
      return;
    }
    const nextSymbols = status?.config.symbols?.length
      ? status.config.symbols.join("\n")
      : EXAMPLE_SYMBOLS.join("\n");
    startTransition(() => {
      setDraftSymbols(nextSymbols);
    });
  }, [hasEditedSymbols, status?.config.symbols]);

  useEffect(() => {
    if (hasEditedContractExpiries) {
      return;
    }
    const nextDrafts: Record<string, string> = {};
    for (const contract of contractCatalogQuery.data?.contracts ?? []) {
      if (contract.active_expiry) {
        nextDrafts[contract.symbol] = contract.active_expiry;
      }
    }
    setContractExpiryDrafts(nextDrafts);
  }, [contractCatalogQuery.data, hasEditedContractExpiries]);

  const parsedSymbols = deferredDraftSymbols
    .split("\n")
    .map((value) => value.trim().toUpperCase())
    .filter(Boolean);

  const saveConfigMutation = useMutation({
    mutationFn: (symbols: string[]) => updateCommodityStrategyConfig(symbols),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["commodityStrategyStatus"] });
      qc.invalidateQueries({ queryKey: ["commodityReports"] });
      qc.invalidateQueries({ queryKey: ["commodityStrategyContracts"] });
      qc.invalidateQueries({ queryKey: ["commodityAtmWatchlist"] });
      setHasEditedContractExpiries(false);
      setHasEditedSymbols(false);
    },
  });

  const startAgentMutation = useMutation({
    mutationFn: () => startCommodityStrategyAgent(),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["commodityStrategyStatus"] });
      qc.invalidateQueries({ queryKey: ["commodityKillSwitch"] });
      qc.invalidateQueries({ queryKey: ["commodityOrders"] });
      qc.invalidateQueries({ queryKey: ["commodityPositions"] });
      qc.invalidateQueries({ queryKey: ["commodityReports"] });
      qc.invalidateQueries({ queryKey: ["commodityAtmWatchlist"] });
    },
  });

  const killSwitchMutation = useMutation({
    mutationFn: (active: boolean) => updateCommodityKillSwitch(active),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["commodityKillSwitch"] });
      qc.invalidateQueries({ queryKey: ["commodityStrategyStatus"] });
      qc.invalidateQueries({ queryKey: ["commodityOrders"] });
      qc.invalidateQueries({ queryKey: ["commodityPositions"] });
      qc.invalidateQueries({ queryKey: ["commodityReports"] });
      qc.invalidateQueries({ queryKey: ["commodityAtmWatchlist"] });
    },
  });

  const saveContractSelectionsMutation = useMutation({
    mutationFn: (selectedOptionExpiries: Record<string, string>) =>
      updateCommodityStrategyContracts(selectedOptionExpiries),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["commodityStrategyStatus"] });
      qc.invalidateQueries({ queryKey: ["commodityStrategyContracts"] });
      qc.invalidateQueries({ queryKey: ["commodityAtmWatchlist"] });
      setHasEditedContractExpiries(false);
    },
  });

  const watchlist = status?.watchlist || [];
  const commentary = status?.commentary || [];
  const positionRows = positions || status?.positions || [];
  const orderRows = orders || status?.orders || [];
  const reportRows = reports || status?.reports || [];
  const contractCatalog = contractCatalogQuery.data?.contracts || [];
  const killSwitchActive = killSwitchState?.kill_switch_active ?? status?.kill_switch_active ?? false;
  const loopActive = status?.loop_active ?? killSwitchState?.loop_active ?? false;
  const startRequired = status?.start_required ?? killSwitchState?.start_required ?? false;
  const selectedExpiryCount = Object.keys(status?.config.selected_option_expiries || {}).length;
  const totalTrades = status?.summary.total_trades ?? 0;
  const saveError = saveConfigMutation.error as { response?: { data?: { detail?: string } } } | null;

  return (
    <div className="max-w-[1800px] space-y-4 pb-8">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="flex items-center gap-2 text-lg font-bold font-mono text-text-primary">
            <Boxes size={18} className="text-accent-amber" />
            Commodity Desk
          </h1>
          <p className="mt-1 max-w-3xl text-xs text-text-muted">
            MCX paper workspace with a continuous background agent. Broker connection status already lives in the global top bar, so this page stays focused on setup, signals, the ATM board, and activity.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {!killSwitchActive && (!loopActive || startRequired) && (
            <button
              type="button"
              onClick={() => startAgentMutation.mutate()}
              disabled={startAgentMutation.isPending}
              className={clsx(
                "inline-flex items-center gap-2 rounded border px-3 py-2 text-xs font-semibold transition-colors",
                "border-accent-blue/40 bg-accent-blue/10 text-accent-blue hover:bg-accent-blue/20",
                startAgentMutation.isPending && "cursor-not-allowed opacity-60",
              )}
            >
              {startAgentMutation.isPending ? <RefreshCw size={14} className="animate-spin" /> : <Play size={14} />}
              Start Commodity Agent
            </button>
          )}
          <button
            type="button"
            onClick={() => killSwitchMutation.mutate(!killSwitchActive)}
            disabled={killSwitchMutation.isPending}
            className={clsx(
              "inline-flex items-center gap-2 rounded border px-3 py-2 text-xs font-semibold transition-colors",
              killSwitchActive
                ? "border-accent-green/40 bg-accent-green/10 text-accent-green hover:bg-accent-green/20"
                : "border-accent-red/40 bg-accent-red/10 text-accent-red hover:bg-accent-red/20",
              killSwitchMutation.isPending && "cursor-not-allowed opacity-60",
            )}
          >
            {killSwitchActive ? <ShieldCheck size={14} /> : <ShieldAlert size={14} />}
            {killSwitchActive ? "Release Commodity Kill Switch" : "Activate Commodity Kill Switch"}
          </button>
        </div>
      </div>

      <div className="card p-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex flex-wrap items-center gap-2">
            <span
              className={clsx(
                "rounded px-2 py-1 text-xs font-semibold",
                loopActive ? "bg-accent-blue/15 text-accent-blue" : "bg-bg-secondary text-text-muted",
              )}
            >
              {loopActive ? "Continuous Loop Active" : startRequired ? "Start Required" : "Loop Paused"}
            </span>
            <span
              className={clsx(
                "rounded px-2 py-1 text-xs font-semibold",
                killSwitchActive ? "bg-accent-red/15 text-accent-red" : "bg-accent-green/15 text-accent-green",
              )}
            >
              {killSwitchActive ? "Kill Switch Active" : "Kill Switch Released"}
            </span>
            <span className="rounded px-2 py-1 text-xs font-semibold bg-bg-secondary text-text-muted">
              State Persisted
            </span>
          </div>
          <div className="text-xs text-text-muted">
            Last scan {formatTimestamp(status?.last_run_at)} | Scan every {status?.scan_interval_seconds || 120}s
          </div>
        </div>

        <div className="mt-4 grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
          <div className="rounded border border-bg-border bg-bg-secondary/30 p-3">
            <div className="text-[11px] uppercase tracking-wide text-text-muted">Tracked</div>
            <div className="mt-1 font-mono text-sm text-text-primary">{status?.summary.tracked_symbols ?? 0}</div>
          </div>
          <div className="rounded border border-bg-border bg-bg-secondary/30 p-3">
            <div className="text-[11px] uppercase tracking-wide text-text-muted">Saved Expiries</div>
            <div className="mt-1 font-mono text-sm text-text-primary">{selectedExpiryCount}</div>
          </div>
          <div className="rounded border border-bg-border bg-bg-secondary/30 p-3">
            <div className="text-[11px] uppercase tracking-wide text-text-muted">Open Positions</div>
            <div className="mt-1 font-mono text-sm text-text-primary">{status?.summary.open_positions ?? 0}</div>
          </div>
          <div className="rounded border border-bg-border bg-bg-secondary/30 p-3">
            <div className="text-[11px] uppercase tracking-wide text-text-muted">Trades</div>
            <div className="mt-1 font-mono text-sm text-text-primary">{totalTrades}</div>
          </div>
          <div className="rounded border border-bg-border bg-bg-secondary/30 p-3">
            <div className="text-[11px] uppercase tracking-wide text-text-muted">Equity</div>
            <div className="mt-1 font-mono text-sm text-text-primary">
              {status?.summary.total_equity != null ? status.summary.total_equity.toFixed(2) : "--"}
            </div>
          </div>
          <div className="rounded border border-bg-border bg-bg-secondary/30 p-3">
            <div className="text-[11px] uppercase tracking-wide text-text-muted">Realized P&amp;L</div>
            <div
              className={clsx(
                "mt-1 font-mono text-sm",
                (status?.summary.realized_pnl || 0) >= 0 ? "text-accent-green" : "text-accent-red",
              )}
            >
              {formatSigned(status?.summary.realized_pnl, 2)}
            </div>
          </div>
        </div>

        <div className="mt-4 rounded border border-bg-border bg-bg-secondary/20 px-3 py-3 text-sm text-text-secondary">
          {status?.last_error || status?.last_message || "Waiting for commodity strategy state…"}
        </div>
      </div>

      <div className="card p-4">
        <div className="flex flex-wrap items-center gap-2">
          <WorkspaceButton
            active={workspace === "setup"}
            label="Setup"
            description="Symbols and expiry map."
            onClick={() => setWorkspace("setup")}
          />
          <WorkspaceButton
            active={workspace === "signals"}
            label="Signals"
            description="Commodity breakout watchlist."
            onClick={() => setWorkspace("signals")}
          />
          <WorkspaceButton
            active={workspace === "atm"}
            label="ATM Board"
            description="MCX CE/PE board by saved expiry."
            onClick={() => setWorkspace("atm")}
          />
          <WorkspaceButton
            active={workspace === "activity"}
            label="Activity"
            description="Positions, orders, reports, notes."
            onClick={() => setWorkspace("activity")}
          />
        </div>
      </div>

      {workspace === "setup" && (
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-[1fr_1.5fr]">
          <div className="card p-4">
            <div className="flex items-center justify-between gap-3">
              <div>
                <h2 className="text-sm font-semibold text-text-primary">Tracked MCX Symbols</h2>
                <div className="text-xs text-text-muted">One Fyers commodity future per line. Older silver symbols are normalized to `SILVERM`.</div>
              </div>
              <span className="text-xs text-text-muted">{parsedSymbols.length} symbols</span>
            </div>

            <textarea
              value={draftSymbols}
              onChange={(event) => {
                setHasEditedSymbols(true);
                setDraftSymbols(event.target.value);
              }}
              className="terminal-input mt-4 min-h-[260px] w-full resize-y text-sm"
              spellCheck={false}
            />

            <div className="mt-3 text-xs text-text-muted">
              Example Fyers codes: {EXAMPLE_SYMBOLS.join(", ")}
            </div>

            <div className="mt-4 flex flex-wrap gap-2">
              <button
                type="button"
                onClick={() => saveConfigMutation.mutate(parsedSymbols)}
                disabled={saveConfigMutation.isPending}
                className={clsx(
                  "inline-flex items-center gap-2 rounded border px-3 py-2 text-xs font-semibold transition-colors",
                  "border-accent-green/40 bg-accent-green/10 text-accent-green hover:bg-accent-green/20",
                  saveConfigMutation.isPending && "cursor-not-allowed opacity-60",
                )}
              >
                <Save size={14} />
                Save Symbols
              </button>
              <button
                type="button"
                onClick={() => {
                  setHasEditedSymbols(false);
                  setDraftSymbols((status?.config.symbols?.length ? status.config.symbols : EXAMPLE_SYMBOLS).join("\n"));
                }}
                className="inline-flex items-center gap-2 rounded border border-bg-border px-3 py-2 text-xs font-semibold text-text-secondary transition-colors hover:border-accent-blue hover:text-accent-blue"
              >
                <RefreshCw size={14} />
                Reset Draft
              </button>
            </div>

            {(saveConfigMutation.isError || saveConfigMutation.isSuccess) && (
              <div
                className={clsx(
                  "mt-3 text-xs",
                  saveConfigMutation.isError ? "text-accent-red" : "text-accent-green",
                )}
              >
                {saveConfigMutation.isError
                  ? (saveError?.response?.data?.detail || "Unable to save symbol list.")
                  : "Commodity symbol list saved."}
              </div>
            )}
          </div>

          <div className="card p-4">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <h2 className="text-sm font-semibold text-text-primary">Expiry Selection Map</h2>
                <div className="text-xs text-text-muted">
                  Each MCX future keeps its own option expiry ladder. Pick the expiry this desk should use for that instrument.
                </div>
              </div>
              <div className="flex flex-wrap gap-2">
                <button
                  type="button"
                  onClick={() => {
                    const payload = Object.fromEntries(
                      Object.entries(contractExpiryDrafts).filter(([, value]) => value),
                    );
                    saveContractSelectionsMutation.mutate(payload);
                  }}
                  disabled={saveContractSelectionsMutation.isPending}
                  className={clsx(
                    "inline-flex items-center gap-2 rounded border px-3 py-2 text-xs font-semibold transition-colors",
                    "border-accent-green/40 bg-accent-green/10 text-accent-green hover:bg-accent-green/20",
                    saveContractSelectionsMutation.isPending && "cursor-not-allowed opacity-60",
                  )}
                >
                  <Save size={14} />
                  Save Expiry Map
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setHasEditedContractExpiries(false);
                    const nextDrafts: Record<string, string> = {};
                    for (const contract of contractCatalog) {
                      if (contract.active_expiry) {
                        nextDrafts[contract.symbol] = contract.active_expiry;
                      }
                    }
                    setContractExpiryDrafts(nextDrafts);
                  }}
                  className="inline-flex items-center gap-2 rounded border border-bg-border px-3 py-2 text-xs font-semibold text-text-secondary transition-colors hover:border-accent-blue hover:text-accent-blue"
                >
                  <RefreshCw size={14} />
                  Reset Expiries
                </button>
              </div>
            </div>

            {contractCatalogQuery.data?.detail && (
              <div className="mt-3 text-xs text-accent-amber">{contractCatalogQuery.data.detail}</div>
            )}

            <div className="mt-4 grid gap-3 md:grid-cols-3">
              <div className="rounded-xl border border-bg-border bg-bg-secondary/50 p-3">
                <div className="text-[11px] uppercase tracking-[0.14em] text-text-muted">Saved Symbols</div>
                <div className="mt-1 font-mono text-lg font-semibold text-text-primary">
                  {contractCatalogQuery.data?.summary?.total_symbols ?? 0}
                </div>
              </div>
              <div className="rounded-xl border border-bg-border bg-bg-secondary/50 p-3">
                <div className="text-[11px] uppercase tracking-[0.14em] text-text-muted">Contracts Ready</div>
                <div className="mt-1 font-mono text-lg font-semibold text-accent-blue">
                  {contractCatalogQuery.data?.summary?.contracts_ready ?? 0}
                </div>
              </div>
              <div className="rounded-xl border border-bg-border bg-bg-secondary/50 p-3">
                <div className="text-[11px] uppercase tracking-[0.14em] text-text-muted">Active Expiries</div>
                <div className="mt-1 font-mono text-lg font-semibold text-accent-green">
                  {contractCatalogQuery.data?.summary?.active_selections ?? 0}
                </div>
              </div>
            </div>

            <div className="mt-4 overflow-x-auto">
              <table className="w-full min-w-[980px] text-left text-xs">
                <thead>
                  <tr className="border-b border-bg-border text-text-muted">
                    <th className="pb-2 pr-3">Underlying</th>
                    <th className="pb-2 pr-3">Saved Future</th>
                    <th className="pb-2 pr-3">Option Root</th>
                    <th className="pb-2 pr-3">Available Expiries</th>
                    <th className="pb-2 pr-3">Expiry To Trade</th>
                    <th className="pb-2">Status</th>
                  </tr>
                </thead>
                <tbody>
                  {contractCatalog.length ? (
                    contractCatalog.map((contract) => (
                      <tr key={contract.symbol} className="border-b border-bg-border/40 align-top">
                        <td className="py-3 pr-3 font-medium text-text-primary">{contract.underlying}</td>
                        <td className="py-3 pr-3 font-mono text-text-muted">{contract.symbol}</td>
                        <td className="py-3 pr-3 font-mono text-text-muted">{contract.lookup_symbol || contract.symbol}</td>
                        <td className="py-3 pr-3 text-text-secondary">
                          {contract.expiries.length ? contract.expiries.join(", ") : "--"}
                        </td>
                        <td className="py-3 pr-3">
                          <select
                            value={contractExpiryDrafts[contract.symbol] || ""}
                            onChange={(event) => {
                              setHasEditedContractExpiries(true);
                              setContractExpiryDrafts((current) => ({
                                ...current,
                                [contract.symbol]: event.target.value,
                              }));
                            }}
                            disabled={!contract.expiries.length}
                            className="terminal-input min-w-[176px] py-1.5 text-xs"
                          >
                            <option value="">No selection</option>
                            {contract.expiries.map((item) => (
                              <option key={`${contract.symbol}:${item}`} value={item}>
                                {item}
                              </option>
                            ))}
                          </select>
                        </td>
                        <td className="py-3">
                          {!contract.has_options ? (
                            <span className="text-accent-amber">{contract.detail || "No option contracts"}</span>
                          ) : contract.selected_expiry ? (
                            <div className="space-y-1">
                              <span className="block text-accent-green">Saved {contract.selected_expiry}</span>
                              {contract.detail && <span className="block text-text-muted">{contract.detail}</span>}
                            </div>
                          ) : contract.suggested_expiry ? (
                            <div className="space-y-1">
                              <span className="block text-text-secondary">Suggested {contract.suggested_expiry}</span>
                              {contract.detail && <span className="block text-text-muted">{contract.detail}</span>}
                            </div>
                          ) : (
                            <span className="text-text-muted">Waiting for selection</span>
                          )}
                        </td>
                      </tr>
                    ))
                  ) : (
                    <tr>
                      <td colSpan={6} className="py-8 text-center text-sm text-text-muted">
                        Save MCX symbols to list available commodity option contracts.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>

            {(saveContractSelectionsMutation.isError || saveContractSelectionsMutation.isSuccess) && (
              <div
                className={clsx(
                  "mt-3 text-xs",
                  saveContractSelectionsMutation.isError ? "text-accent-red" : "text-accent-green",
                )}
              >
                {saveContractSelectionsMutation.isError
                  ? "Unable to save commodity expiry selections."
                  : "Commodity expiry selections saved."}
              </div>
            )}
          </div>
        </div>
      )}

      {workspace === "signals" && (
        <div className="card p-4">
          <div className="flex items-center justify-between gap-3">
            <div>
              <h2 className="text-sm font-semibold text-text-primary">Signal Watchlist</h2>
              <div className="text-xs text-text-muted">
                Latest 30-minute breakout state for the saved MCX futures list.
              </div>
            </div>
            <div className="text-xs text-text-muted">{watchlist.length} rows</div>
          </div>

          <div className="mt-4 overflow-x-auto">
            <table className="w-full min-w-[1100px] text-left text-xs">
              <thead>
                <tr className="border-b border-bg-border text-text-muted">
                  <th className="pb-2 pr-3">Symbol</th>
                  <th className="pb-2 pr-3">Price</th>
                  <th className="pb-2 pr-3">Change</th>
                  <th className="pb-2 pr-3">Signal</th>
                  <th className="pb-2 pr-3">Regime</th>
                  <th className="pb-2 pr-3">EMA Fast / Slow</th>
                  <th className="pb-2 pr-3">ATR</th>
                  <th className="pb-2 pr-3">Breakout</th>
                  <th className="pb-2">Last Bar</th>
                </tr>
              </thead>
              <tbody>
                {watchlist.length ? (
                  watchlist.map((row) => (
                    <tr key={row.symbol} className="border-b border-bg-border/40">
                      <td className="py-3 pr-3 font-medium text-text-primary">{row.symbol}</td>
                      <td className="py-3 pr-3 font-mono text-text-primary">{row.price?.toFixed(2) || "--"}</td>
                      <td className={clsx("py-3 pr-3 font-mono", (row.change_pct || 0) >= 0 ? "text-accent-green" : "text-accent-red")}>
                        {formatSigned(row.change_pct, 2, "%")}
                      </td>
                      <td className="py-3 pr-3">
                        <span
                          className={clsx(
                            "rounded px-2 py-1 text-[11px] font-semibold",
                            row.signal === "BUY" && "bg-accent-green/15 text-accent-green",
                            row.signal === "SELL" && "bg-accent-red/15 text-accent-red",
                            !row.signal && "bg-bg-secondary text-text-muted",
                          )}
                        >
                          {row.signal || "WAIT"}
                        </span>
                      </td>
                      <td className="py-3 pr-3 text-text-secondary">{row.regime}</td>
                      <td className="py-3 pr-3 font-mono text-text-primary">
                        {row.ema_fast?.toFixed(2) || "--"} / {row.ema_slow?.toFixed(2) || "--"}
                      </td>
                      <td className="py-3 pr-3 font-mono text-text-primary">{row.atr?.toFixed(4) || "--"}</td>
                      <td className="py-3 pr-3 text-text-muted">
                        {row.reason.replaceAll("_", " ")} | H {row.breakout_high?.toFixed(2) || "--"} | L {row.breakout_low?.toFixed(2) || "--"}
                      </td>
                      <td className="py-3 text-text-muted">{formatTimestamp(row.bar_time)}</td>
                    </tr>
                  ))
                ) : (
                  <tr>
                    <td colSpan={9} className="py-8 text-center text-sm text-text-muted">
                      No commodity signal rows yet. Save symbols in Setup and let the background agent populate this watchlist.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {workspace === "atm" && (
        <div className="space-y-4">
          <div className="card p-4">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <h2 className="text-sm font-semibold text-text-primary">ATM Board</h2>
                <div className="text-xs text-text-muted">
                  MCX CE / PE board for the saved expiry selection of each instrument. Prices refresh automatically every 5 seconds while this tab is open.
                </div>
              </div>
              <div className="text-xs text-text-muted">
                {watchlistQuery.isFetching ? "Refreshing prices..." : "Live auto-refresh"}
              </div>
            </div>

            {watchlistQuery.data?.detail && (
              <div className="mt-3 text-xs text-accent-amber">{watchlistQuery.data.detail}</div>
            )}

            <div className="mt-4 grid gap-3 md:grid-cols-4">
              <div className="rounded-xl border border-bg-border bg-bg-secondary/50 p-3">
                <div className="text-[11px] uppercase tracking-[0.14em] text-text-muted">Rows</div>
                <div className="mt-1 font-mono text-lg font-semibold text-text-primary">
                  {watchlistQuery.data?.summary?.total_rows ?? 0}
                </div>
              </div>
              <div className="rounded-xl border border-bg-border bg-bg-secondary/50 p-3">
                <div className="text-[11px] uppercase tracking-[0.14em] text-text-muted">CE Ready</div>
                <div className="mt-1 font-mono text-lg font-semibold text-accent-green">
                  {watchlistQuery.data?.summary?.ce_ready ?? 0}
                </div>
              </div>
              <div className="rounded-xl border border-bg-border bg-bg-secondary/50 p-3">
                <div className="text-[11px] uppercase tracking-[0.14em] text-text-muted">PE Ready</div>
                <div className="mt-1 font-mono text-lg font-semibold text-accent-red">
                  {watchlistQuery.data?.summary?.pe_ready ?? 0}
                </div>
              </div>
              <div className="rounded-xl border border-bg-border bg-bg-secondary/50 p-3">
                <div className="text-[11px] uppercase tracking-[0.14em] text-text-muted">Configured</div>
                <div className="mt-1 font-mono text-lg font-semibold text-accent-blue">
                  {watchlistQuery.data?.summary?.configured_contracts ?? 0}
                </div>
              </div>
            </div>

            <div className="mt-4 overflow-x-auto">
              <table className="w-full min-w-[1800px] text-left text-xs">
                <thead>
                  <tr className="border-b border-bg-border text-text-muted">
                    <th className="pb-2 pr-3">Underlying</th>
                    <th className="pb-2 pr-3">Option Root</th>
                    <th className="pb-2 pr-3">Spot</th>
                    <th className="pb-2 pr-3">Option Expiry</th>
                    <th className="pb-2 pr-3">ATM</th>
                    <th className="pb-2 pr-3">CE Snapshot</th>
                    <th className="pb-2">PE Snapshot</th>
                  </tr>
                </thead>
                <tbody>
                  {(watchlistQuery.data?.rows ?? []).map((row) => (
                    <tr key={`${row.symbol}:${row.active_expiry || row.expiry}`} className="border-b border-bg-border/40 align-top">
                      <td className="py-3 pr-3">
                        <div className="font-medium text-text-primary">{row.underlying}</div>
                        <div className="mt-1 text-[11px] uppercase tracking-[0.12em] text-text-muted">
                          saved {row.symbol}
                        </div>
                      </td>
                      <td className="py-3 pr-3 font-mono text-text-muted">{row.fyers_symbol || row.lookup_symbol || "--"}</td>
                      <td className="py-3 pr-3 font-mono text-text-primary">{row.spot_price.toFixed(2)}</td>
                      <td className="py-3 pr-3 font-mono text-text-secondary">{row.expiry}</td>
                      <td className="py-3 pr-3 font-mono text-accent-amber">{row.atm_strike}</td>
                      <td className="py-3 pr-3"><ATMOptionCell option={row.ce} accent="ce" /></td>
                      <td className="py-3"><ATMOptionCell option={row.pe} accent="pe" /></td>
                    </tr>
                  ))}
                  {!watchlistQuery.isLoading && !(watchlistQuery.data?.rows?.length) && (
                    <tr>
                      <td colSpan={7} className="py-8 text-center text-sm text-text-muted">
                        {watchlistQuery.data?.detail || "No ATM watchlist rows are available for the saved expiries."}
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}

      {workspace === "activity" && (
        <div className="space-y-4">
          <div className="grid grid-cols-1 gap-4 xl:grid-cols-[1.15fr_0.85fr]">
            <div className="card p-4">
              <div className="flex items-center justify-between gap-3">
                <h2 className="text-sm font-semibold text-text-primary">Open Positions</h2>
                <div className="text-xs text-text-muted">{positionRows.length} positions</div>
              </div>
              <div className="mt-3 overflow-x-auto">
                <table className="w-full text-xs font-mono">
                  <thead>
                    <tr className="border-b border-bg-border text-text-muted">
                      <th className="pb-2 text-left">Symbol</th>
                      <th className="pb-2 text-left">Side</th>
                      <th className="pb-2 text-right">Qty</th>
                      <th className="pb-2 text-right">Entry</th>
                      <th className="pb-2 text-right">Last</th>
                      <th className="pb-2 text-right">Stop</th>
                      <th className="pb-2 text-right">Target</th>
                      <th className="pb-2 text-right">P&amp;L</th>
                    </tr>
                  </thead>
                  <tbody>
                    {positionRows.length ? (
                      positionRows.map((position) => (
                        <tr key={position.symbol} className="border-b border-bg-border/40">
                          <td className="py-2 text-text-primary">{position.symbol}</td>
                          <td className={clsx("py-2", position.action === "BUY" ? "text-accent-green" : "text-accent-red")}>
                            {position.action}
                          </td>
                          <td className="py-2 text-right text-text-primary">{position.qty}</td>
                          <td className="py-2 text-right text-text-primary">{position.entry_price.toFixed(2)}</td>
                          <td className="py-2 text-right text-text-primary">{position.current_price.toFixed(2)}</td>
                          <td className="py-2 text-right text-text-primary">{position.stop_price.toFixed(2)}</td>
                          <td className="py-2 text-right text-text-primary">{position.target_price.toFixed(2)}</td>
                          <td className={clsx("py-2 text-right font-semibold", (position.unrealized_pnl || 0) >= 0 ? "text-accent-green" : "text-accent-red")}>
                            {formatSigned(position.unrealized_pnl, 2)}
                          </td>
                        </tr>
                      ))
                    ) : (
                      <tr>
                        <td colSpan={8} className="py-8 text-center text-text-muted">
                          No open commodity positions.
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            </div>

            <div className="card p-4">
              <div className="flex items-center justify-between gap-3">
                <h2 className="text-sm font-semibold text-text-primary">Agent Commentary</h2>
                <div className="text-xs text-text-muted">{commentary.length} notes</div>
              </div>
              <div className="mt-3 space-y-2">
                {commentary.length ? (
                  commentary.slice(0, 8).map((entry, index) => (
                    <div key={`${entry.time}-${index}`} className="rounded border border-bg-border bg-bg-secondary/20 px-3 py-3 text-xs">
                      <div className="flex items-center justify-between gap-3">
                        <span
                          className={clsx(
                            "rounded px-2 py-0.5 font-semibold",
                            entry.tone === "trade" && "bg-accent-blue/15 text-accent-blue",
                            entry.tone === "success" && "bg-accent-green/15 text-accent-green",
                            entry.tone === "warning" && "bg-accent-amber/15 text-accent-amber",
                            entry.tone === "error" && "bg-accent-red/15 text-accent-red",
                            entry.tone === "idle" && "bg-bg-secondary text-text-muted",
                          )}
                        >
                          {entry.tone}
                        </span>
                        <span className="text-text-muted">{formatTimestamp(entry.time)}</span>
                      </div>
                      <div className="mt-2 text-sm text-text-secondary">{entry.message}</div>
                    </div>
                  ))
                ) : (
                  <div className="rounded border border-dashed border-bg-border px-3 py-10 text-center text-xs text-text-muted">
                    No commentary yet.
                  </div>
                )}
              </div>
            </div>
          </div>

          <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
            <div className="card p-4">
              <div className="flex items-center justify-between gap-3">
                <h2 className="text-sm font-semibold text-text-primary">Recent Orders</h2>
                <div className="text-xs text-text-muted">{orderRows.length} entries</div>
              </div>
              <div className="mt-3 overflow-x-auto">
                <table className="w-full text-xs font-mono">
                  <thead>
                    <tr className="border-b border-bg-border text-text-muted">
                      <th className="pb-2 text-left">Time</th>
                      <th className="pb-2 text-left">Symbol</th>
                      <th className="pb-2 text-left">Side</th>
                      <th className="pb-2 text-right">Qty</th>
                      <th className="pb-2 text-right">Fill</th>
                      <th className="pb-2 text-left">Reason</th>
                    </tr>
                  </thead>
                  <tbody>
                    {orderRows.length ? (
                      orderRows.map((order) => (
                        <tr key={order.order_id} className="border-b border-bg-border/40">
                          <td className="py-2 text-text-muted">{formatTimestamp(order.time)}</td>
                          <td className="py-2 text-text-primary">{order.symbol}</td>
                          <td className={clsx("py-2", order.action === "BUY" ? "text-accent-green" : "text-accent-red")}>
                            {order.action}
                          </td>
                          <td className="py-2 text-right text-text-primary">{order.qty}</td>
                          <td className="py-2 text-right text-text-primary">{order.fill_price?.toFixed(2) || "--"}</td>
                          <td className="py-2 text-text-muted">{order.reason.replaceAll("_", " ")}</td>
                        </tr>
                      ))
                    ) : (
                      <tr>
                        <td colSpan={6} className="py-8 text-center text-text-muted">
                          No commodity orders yet.
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            </div>

            <div className="card p-4">
              <div className="flex items-center justify-between gap-3">
                <h2 className="text-sm font-semibold text-text-primary">Report Snapshots</h2>
                <div className="text-xs text-text-muted">{reportRows.length} snapshots</div>
              </div>
              <div className="mt-3 overflow-x-auto">
                <table className="w-full text-xs font-mono">
                  <thead>
                    <tr className="border-b border-bg-border text-text-muted">
                      <th className="pb-2 text-left">Time</th>
                      <th className="pb-2 text-right">Equity</th>
                      <th className="pb-2 text-right">Realized</th>
                      <th className="pb-2 text-right">Unrealized</th>
                      <th className="pb-2 text-right">Open</th>
                    </tr>
                  </thead>
                  <tbody>
                    {reportRows.length ? (
                      reportRows.map((report, index) => (
                        <tr key={`${report.time}-${index}`} className="border-b border-bg-border/40">
                          <td className="py-2 text-text-muted">{formatTimestamp(report.time)}</td>
                          <td className="py-2 text-right text-text-primary">{report.total_equity.toFixed(2)}</td>
                          <td className={clsx("py-2 text-right", report.realized_pnl >= 0 ? "text-accent-green" : "text-accent-red")}>
                            {formatSigned(report.realized_pnl, 2)}
                          </td>
                          <td className={clsx("py-2 text-right", report.unrealized_pnl >= 0 ? "text-accent-green" : "text-accent-red")}>
                            {formatSigned(report.unrealized_pnl, 2)}
                          </td>
                          <td className="py-2 text-right text-text-primary">{report.open_positions}</td>
                        </tr>
                      ))
                    ) : (
                      <tr>
                        <td colSpan={5} className="py-8 text-center text-text-muted">
                          No report snapshots yet.
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

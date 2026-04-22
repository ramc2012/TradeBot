"use client";
import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  runBacktestBreeze, runBacktestJson, runWalkForward,
  getBacktesterDefaultConfig, getBrokerStatus,
} from "@/lib/api";
import PageTabs from "@/components/layout/PageTabs";
import { clsx } from "clsx";
import {
  FlaskConical, Play, TrendingUp, TrendingDown, BarChart3,
  AlertCircle, CheckCircle2, Loader2, Upload,
} from "lucide-react";

// ── Types ─────────────────────────────────────────────────────────────────────

interface BacktestReport {
  summary: {
    underlying: string;
    market: string;
    instruments: number;
    config: Record<string, number>;
  };
  aggregate: {
    total_trades: number;
    avg_win_rate: number;
    avg_profit_factor: number;
    avg_sharpe: number;
    avg_max_drawdown_pct: number;
    total_pnl_rupees: number;
  };
  by_instrument: Array<{
    option_type: string;
    total_signals: number;
    total_trades: number;
    win_rate: number;
    profit_factor: number;
    sharpe_ratio: number;
    max_drawdown_pct: number;
    avg_holding_bars: number;
    reward_risk_ratio: number;
    exit_breakdown: Record<string, number>;
  }>;
}

const RESEARCH_TABS = [
  { href: "/analysis", label: "Research Monitor" },
  { href: "/backtester", label: "Backtester" },
  { href: "/data", label: "F&O Data" },
];

// ── Metric Card ───────────────────────────────────────────────────────────────

function MetricCard({
  label, value, sub, color = "text-text-primary", bg,
}: {
  label: string; value: string | number; sub?: string;
  color?: string; bg?: string;
}) {
  return (
    <div className={clsx("rounded-lg p-3 flex flex-col gap-0.5", bg || "bg-bg-secondary")}>
      <span className="text-text-muted text-xs uppercase tracking-wide">{label}</span>
      <span className={clsx("font-mono text-lg font-bold", color)}>{value}</span>
      {sub && <span className="text-text-muted text-xs">{sub}</span>}
    </div>
  );
}

// ── Results Panel ─────────────────────────────────────────────────────────────

function ResultsPanel({ report }: { report: BacktestReport }) {
  const agg = report.aggregate;
  const pnlPositive = agg.total_pnl_rupees >= 0;

  // Success criteria check
  const criteria = [
    { label: "Win rate > 52%", met: agg.avg_win_rate > 0.52 },
    { label: "Profit factor > 1.3", met: agg.avg_profit_factor > 1.3 },
    { label: "Sharpe > 1.0", met: agg.avg_sharpe > 1.0 },
    { label: "Max drawdown < 30%", met: agg.avg_max_drawdown_pct < 0.30 },
    { label: "Has trades", met: agg.total_trades > 0 },
  ];
  const passed = criteria.filter((c) => c.met).length;
  const overall = passed >= 4;

  return (
    <div className="space-y-4">
      {/* Overall verdict */}
      <div className={clsx(
        "rounded-lg p-4 border flex items-center gap-3",
        overall
          ? "border-accent-green/40 bg-accent-green/5"
          : "border-accent-amber/40 bg-accent-amber/5"
      )}>
        {overall
          ? <CheckCircle2 size={20} className="text-accent-green shrink-0" />
          : <AlertCircle size={20} className="text-accent-amber shrink-0" />
        }
        <div>
          <div className={clsx("font-bold text-sm", overall ? "text-accent-green" : "text-accent-amber")}>
            {overall ? "Strategy looks viable" : "Strategy needs improvement"}
          </div>
          <div className="text-xs text-text-muted">
            {passed}/{criteria.length} success criteria met
          </div>
        </div>
        <div className="ml-auto flex flex-wrap gap-2">
          {criteria.map((c) => (
            <span key={c.label} className={clsx(
              "text-xs px-2 py-0.5 rounded",
              c.met ? "bg-accent-green/15 text-accent-green" : "bg-accent-red/15 text-accent-red"
            )}>
              {c.met ? "✓" : "✗"} {c.label}
            </span>
          ))}
        </div>
      </div>

      {/* Aggregate metrics */}
      <div className="grid grid-cols-3 md:grid-cols-6 gap-3">
        <MetricCard label="Trades" value={agg.total_trades} />
        <MetricCard
          label="Win Rate"
          value={`${(agg.avg_win_rate * 100).toFixed(1)}%`}
          color={agg.avg_win_rate > 0.52 ? "text-accent-green" : "text-accent-red"}
        />
        <MetricCard
          label="Profit Factor"
          value={agg.avg_profit_factor.toFixed(2)}
          color={agg.avg_profit_factor > 1.3 ? "text-accent-green" : "text-accent-red"}
        />
        <MetricCard
          label="Sharpe"
          value={agg.avg_sharpe.toFixed(2)}
          color={agg.avg_sharpe > 1.0 ? "text-accent-green" : "text-accent-amber"}
        />
        <MetricCard
          label="Max DD"
          value={`${(agg.avg_max_drawdown_pct * 100).toFixed(1)}%`}
          color={agg.avg_max_drawdown_pct < 0.20 ? "text-accent-green" : "text-accent-red"}
        />
        <MetricCard
          label="Total P&L"
          value={`₹${Math.abs(agg.total_pnl_rupees).toLocaleString("en-IN", { maximumFractionDigits: 0 })}`}
          color={pnlPositive ? "text-accent-green" : "text-accent-red"}
          sub={pnlPositive ? "profit" : "loss"}
        />
      </div>

      {/* Per-instrument breakdown */}
      {report.by_instrument.map((inst, i) => (
        <div key={i} className="card p-4 space-y-3">
          <div className="flex items-center gap-3">
            <span className={clsx(
              "px-2 py-0.5 rounded text-xs font-bold",
              inst.option_type === "CE" ? "bg-accent-green/20 text-accent-green" : "bg-accent-red/20 text-accent-red"
            )}>
              {inst.option_type}
            </span>
            <span className="text-sm font-semibold">{report.summary.underlying}</span>
            <span className="text-xs text-text-muted">{inst.total_signals} signals → {inst.total_trades} trades</span>
          </div>

          <div className="grid grid-cols-4 gap-3 text-xs font-mono">
            <div>
              <div className="text-text-muted">Win Rate</div>
              <div className={clsx("font-bold", inst.win_rate > 0.52 ? "text-accent-green" : "text-accent-red")}>
                {(inst.win_rate * 100).toFixed(1)}%
              </div>
            </div>
            <div>
              <div className="text-text-muted">Profit Factor</div>
              <div className={clsx("font-bold", inst.profit_factor > 1.3 ? "text-accent-green" : "text-accent-red")}>
                {inst.profit_factor.toFixed(2)}x
              </div>
            </div>
            <div>
              <div className="text-text-muted">Sharpe</div>
              <div className={clsx("font-bold", inst.sharpe_ratio > 1.0 ? "text-accent-green" : "text-accent-amber")}>
                {inst.sharpe_ratio.toFixed(2)}
              </div>
            </div>
            <div>
              <div className="text-text-muted">Avg Hold</div>
              <div className="font-bold">{inst.avg_holding_bars.toFixed(0)} bars</div>
            </div>
          </div>

          {/* Exit breakdown */}
          <div className="text-xs">
            <div className="text-text-muted mb-1">Exit reasons:</div>
            <div className="flex flex-wrap gap-2">
              {Object.entries(inst.exit_breakdown).map(([k, v]) => v > 0 && (
                <span key={k} className="bg-bg-secondary px-2 py-0.5 rounded">
                  <span className="text-text-muted">{k.replace("_", " ")}: </span>
                  <span className="text-text-primary font-bold">{v}</span>
                </span>
              ))}
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

// ── Main Page ─────────────────────────────────────────────────────────────────

export default function BacktesterPage() {
  const [tab, setTab] = useState<"breeze" | "csv" | "walkforward">("breeze");
  const [report, setReport] = useState<BacktestReport | null>(null);
  const [wfResult, setWfResult] = useState<any>(null);

  // Check if ICICI Breeze is connected
  const { data: brokerStatuses } = useQuery({
    queryKey: ["brokerStatus"],
    queryFn: () => getBrokerStatus().then((r) => r.data),
    staleTime: 30000,
  });
  const breezeConnected = (brokerStatuses || []).find((b: any) => b.broker === "icici_breeze")?.connected;

  // MACD Config
  const [macdFast, setMacdFast] = useState("12");
  const [macdSlow, setMacdSlow] = useState("26");
  const [macdSignal, setMacdSignal] = useState("9");
  const [slPct, setSlPct] = useState("35");
  const [target1Pct, setTarget1Pct] = useState("50");
  const [target3Pct, setTarget3Pct] = useState("180");

  // Breeze form
  const [stockCode, setStockCode] = useState("NIFTY");
  const [expiry, setExpiry] = useState("2024-12-26T07:00:00.000Z");
  const [right, setRight] = useState("call");
  const [strikePrice, setStrikePrice] = useState("24000");
  const [fromDate, setFromDate] = useState("2024-11-01T07:00:00.000Z");
  const [toDate, setToDate] = useState("2024-12-26T07:00:00.000Z");

  // CSV upload
  const [csvFile, setCsvFile] = useState<File | null>(null);
  const [csvUnderlying, setCsvUnderlying] = useState("SPY");
  const [csvMarket, setCsvMarket] = useState("US");

  const getConfig = () => ({
    macd_fast: parseInt(macdFast),
    macd_slow: parseInt(macdSlow),
    macd_signal: parseInt(macdSignal),
    sl_pct: parseInt(slPct) / 100,
    target_1_pct: parseInt(target1Pct) / 100,
    target_2_pct: 1.00,
    target_3_pct: parseInt(target3Pct) / 100,
    option_types: ["CE", "PE"],
  });

  // Breeze backtest mutation
  const breezeMut = useMutation({
    mutationFn: () => runBacktestBreeze({
      stock_code: stockCode,
      expiry_date: expiry,
      right,
      strike_price: strikePrice,
      from_date: fromDate,
      to_date: toDate,
      config: getConfig(),
    }),
    onSuccess: (data) => setReport(data.data.report),
  });

  // CSV backtest
  const csvMut = useMutation({
    mutationFn: async () => {
      if (!csvFile) throw new Error("No file selected");
      const fd = new FormData();
      fd.append("file", csvFile);
      fd.append("underlying", csvUnderlying);
      fd.append("market", csvMarket);
      fd.append("config_json", JSON.stringify(getConfig()));
      const { uploadBacktestCsv } = await import("@/lib/api");
      return uploadBacktestCsv(fd);
    },
    onSuccess: (data) => setReport(data.data.report),
  });

  // Walk-forward (uses same breeze data)
  const wfMut = useMutation({
    mutationFn: () => runWalkForward({
      config: getConfig(),
      train_pct: 0.70,
      n_windows: 5,
      data: null,  // would need data loaded; placeholder
      underlying: stockCode,
      market: "NSE",
    }),
    onSuccess: (data) => setWfResult(data.data),
  });

  const tabs = [
    { id: "breeze", label: "ICICI Breeze (NSE)" },
    { id: "csv", label: "CSV Upload (US/NSE)" },
    { id: "walkforward", label: "Walk-Forward" },
  ] as const;

  return (
    <div className="max-w-5xl space-y-4">
      <div className="space-y-3">
        <div className="flex flex-wrap items-center gap-3">
          <FlaskConical size={18} className="text-accent-blue" />
          <h1 className="text-lg font-bold font-mono text-text-primary">Options MACD Backtester</h1>
          <span className="rounded bg-bg-secondary px-2 py-0.5 text-xs text-text-muted">
            MACD on ATM premium · zero-line cross
          </span>
        </div>
        <PageTabs tabs={RESEARCH_TABS} />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        {/* Config Panel */}
        <div className="space-y-4">
          <div className="card p-4 space-y-3">
            <h2 className="text-sm font-semibold text-text-secondary">MACD Parameters</h2>
            <div className="grid grid-cols-3 gap-2">
              <div>
                <label className="text-xs text-text-muted block mb-1">Fast</label>
                <input value={macdFast} onChange={(e) => setMacdFast(e.target.value)}
                  type="number" className="terminal-input w-full text-sm" />
              </div>
              <div>
                <label className="text-xs text-text-muted block mb-1">Slow</label>
                <input value={macdSlow} onChange={(e) => setMacdSlow(e.target.value)}
                  type="number" className="terminal-input w-full text-sm" />
              </div>
              <div>
                <label className="text-xs text-text-muted block mb-1">Signal</label>
                <input value={macdSignal} onChange={(e) => setMacdSignal(e.target.value)}
                  type="number" className="terminal-input w-full text-sm" />
              </div>
            </div>
            <div className="grid grid-cols-3 gap-2">
              <div>
                <label className="text-xs text-text-muted block mb-1">SL %</label>
                <input value={slPct} onChange={(e) => setSlPct(e.target.value)}
                  type="number" className="terminal-input w-full text-sm" />
              </div>
              <div>
                <label className="text-xs text-text-muted block mb-1">T1 %</label>
                <input value={target1Pct} onChange={(e) => setTarget1Pct(e.target.value)}
                  type="number" className="terminal-input w-full text-sm" />
              </div>
              <div>
                <label className="text-xs text-text-muted block mb-1">T3 %</label>
                <input value={target3Pct} onChange={(e) => setTarget3Pct(e.target.value)}
                  type="number" className="terminal-input w-full text-sm" />
              </div>
            </div>
          </div>

          {/* Strategy Info */}
          <div className="card p-4 space-y-2 text-xs text-text-muted">
            <div className="text-text-secondary font-semibold text-sm">Strategy Logic</div>
            <ul className="space-y-1 list-disc list-inside">
              <li>MACD computed on <strong className="text-text-primary">option premium</strong> (not underlying)</li>
              <li>Zero-line cross signals delta + vega alignment</li>
              <li>Buy CE on ZERO_CROSS_UP, buy PE on ZERO_CROSS_DOWN</li>
              <li>SL: {slPct}% below entry premium</li>
              <li>Targets: {target1Pct}% / 100% / {target3Pct}%</li>
              <li>Time exit: ~2 sessions (78 bars × 5min)</li>
            </ul>
          </div>
        </div>

        {/* Data Source Tabs */}
        <div className="lg:col-span-2 space-y-4">
          {/* Tabs */}
          <div className="flex gap-1 border-b border-bg-border">
            {tabs.map((t) => (
              <button
                key={t.id}
                onClick={() => setTab(t.id)}
                className={clsx(
                  "px-3 py-2 text-xs font-medium -mb-px border-b-2 transition-colors",
                  tab === t.id
                    ? "border-accent-blue text-accent-blue"
                    : "border-transparent text-text-muted hover:text-text-primary"
                )}
              >
                {t.label}
              </button>
            ))}
          </div>

          {/* Breeze Tab */}
          {tab === "breeze" && (
            <div className="card p-4 space-y-3">
              {!breezeConnected && (
                <div className="bg-accent-amber/5 border border-accent-amber/30 rounded p-3 text-xs text-accent-amber flex items-center gap-2">
                  <AlertCircle size={14} />
                  ICICI Breeze not connected. Go to{" "}
                  <a href="/settings" className="underline">Settings</a>{" "}
                  to connect for NSE historical F&O data (3 years).
                </div>
              )}
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="text-xs text-text-muted block mb-1">Underlying (stock_code)</label>
                  <input value={stockCode} onChange={(e) => setStockCode(e.target.value)}
                    placeholder="NIFTY" className="terminal-input w-full text-sm" />
                </div>
                <div>
                  <label className="text-xs text-text-muted block mb-1">Strike Price</label>
                  <input value={strikePrice} onChange={(e) => setStrikePrice(e.target.value)}
                    placeholder="24000" className="terminal-input w-full text-sm" />
                </div>
                <div>
                  <label className="text-xs text-text-muted block mb-1">Option Type</label>
                  <select value={right} onChange={(e) => setRight(e.target.value)}
                    className="terminal-input w-full text-sm bg-bg-secondary">
                    <option value="call">Call (CE)</option>
                    <option value="put">Put (PE)</option>
                  </select>
                </div>
                <div>
                  <label className="text-xs text-text-muted block mb-1">Expiry Date</label>
                  <input value={expiry} onChange={(e) => setExpiry(e.target.value)}
                    placeholder="2024-12-26T07:00:00.000Z" className="terminal-input w-full text-sm" />
                </div>
                <div>
                  <label className="text-xs text-text-muted block mb-1">From Date</label>
                  <input value={fromDate} onChange={(e) => setFromDate(e.target.value)}
                    placeholder="2024-11-01T07:00:00.000Z" className="terminal-input w-full text-sm" />
                </div>
                <div>
                  <label className="text-xs text-text-muted block mb-1">To Date</label>
                  <input value={toDate} onChange={(e) => setToDate(e.target.value)}
                    placeholder="2024-12-26T07:00:00.000Z" className="terminal-input w-full text-sm" />
                </div>
              </div>
              <button
                onClick={() => breezeMut.mutate()}
                disabled={breezeMut.isPending || !breezeConnected}
                className="w-full py-2.5 rounded text-sm bg-accent-blue/20 border border-accent-blue/30 text-accent-blue hover:bg-accent-blue/30 disabled:opacity-50 flex items-center justify-center gap-2 font-bold"
              >
                {breezeMut.isPending
                  ? <><Loader2 size={14} className="animate-spin" /> Fetching & Running...</>
                  : <><Play size={14} /> Run Backtest (Breeze NSE)</>
                }
              </button>
              {breezeMut.isError && (
                <p className="text-accent-red text-xs">
                  {(breezeMut.error as any)?.response?.data?.detail || "Backtest failed"}
                </p>
              )}
            </div>
          )}

          {/* CSV Tab */}
          {tab === "csv" && (
            <div className="card p-4 space-y-3">
              <div className="text-xs text-text-muted">
                Upload CSV from{" "}
                <a href="https://www.optionsdx.com" target="_blank" rel="noopener noreferrer"
                  className="text-accent-blue hover:underline">OptionsDX</a>{" "}
                (SPY, QQQ, etc.) or custom format with columns:
                <code className="ml-1 text-accent-green bg-bg-secondary px-1 rounded">
                  timestamp, expiry, strike, option_type, open, high, low, close, volume
                </code>
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="text-xs text-text-muted block mb-1">Underlying Name</label>
                  <input value={csvUnderlying} onChange={(e) => setCsvUnderlying(e.target.value)}
                    placeholder="SPY" className="terminal-input w-full text-sm" />
                </div>
                <div>
                  <label className="text-xs text-text-muted block mb-1">Market</label>
                  <select value={csvMarket} onChange={(e) => setCsvMarket(e.target.value)}
                    className="terminal-input w-full text-sm bg-bg-secondary">
                    <option value="US">US</option>
                    <option value="NSE">NSE</option>
                  </select>
                </div>
              </div>
              <label className={clsx(
                "border-2 border-dashed rounded p-6 flex flex-col items-center gap-2 cursor-pointer transition-colors",
                csvFile ? "border-accent-green/40 bg-accent-green/5" : "border-bg-border hover:border-accent-blue/30"
              )}>
                <Upload size={20} className={csvFile ? "text-accent-green" : "text-text-muted"} />
                <span className="text-sm text-text-secondary">
                  {csvFile ? csvFile.name : "Drop CSV here or click to upload"}
                </span>
                <input type="file" accept=".csv" className="hidden"
                  onChange={(e) => setCsvFile(e.target.files?.[0] || null)} />
              </label>
              <button
                onClick={() => csvMut.mutate()}
                disabled={csvMut.isPending || !csvFile}
                className="w-full py-2.5 rounded text-sm bg-accent-green/20 border border-accent-green/30 text-accent-green hover:bg-accent-green/30 disabled:opacity-50 flex items-center justify-center gap-2 font-bold"
              >
                {csvMut.isPending
                  ? <><Loader2 size={14} className="animate-spin" /> Processing CSV...</>
                  : <><Play size={14} /> Run Backtest (CSV)</>
                }
              </button>
              {csvMut.isError && (
                <p className="text-accent-red text-xs">
                  {(csvMut.error as any)?.response?.data?.detail || "Backtest failed"}
                </p>
              )}
            </div>
          )}

          {/* Walk-Forward Tab */}
          {tab === "walkforward" && (
            <div className="card p-4 space-y-3">
              <div className="text-xs text-text-muted">
                Walk-forward optimization: 70/30 train/test split across 5 rolling windows.
                Prevents overfitting. First load data via Breeze or CSV, then run validation.
              </div>
              <div className="bg-bg-secondary rounded p-3 space-y-2 text-xs font-mono">
                <div className="text-text-secondary font-bold">Acceptance Criteria</div>
                <div>• OOS Win rate &gt; 50%</div>
                <div>• OOS Profit factor &gt; 1.3</div>
                <div>• OOS Sharpe &gt; 1.0</div>
                <div>• Pass in 4 of 5 windows</div>
              </div>

              {wfResult && (
                <div className={clsx(
                  "rounded-lg p-4 border",
                  wfResult.accepted ? "border-accent-green/40 bg-accent-green/5" : "border-accent-red/40 bg-accent-red/5"
                )}>
                  <div className={clsx("font-bold text-sm mb-2",
                    wfResult.accepted ? "text-accent-green" : "text-accent-red")}>
                    {wfResult.accepted ? "✓ Walk-Forward PASSED" : "✗ Walk-Forward FAILED"}
                  </div>
                  <div className="text-xs text-text-muted mb-3">
                    {wfResult.accepted_windows}/{wfResult.total_windows} windows passed.
                    Best params: Fast={wfResult.best_params?.macd_fast},
                    Slow={wfResult.best_params?.macd_slow},
                    Signal={wfResult.best_params?.macd_signal}
                  </div>
                  <div className="space-y-1">
                    {(wfResult.window_results || []).map((w: any) => (
                      <div key={w.window} className={clsx(
                        "text-xs flex items-center gap-3 p-1.5 rounded",
                        w.accepted ? "bg-accent-green/10" : "bg-accent-red/10"
                      )}>
                        <span className="text-text-muted w-16">Window {w.window}</span>
                        <span className={w.accepted ? "text-accent-green" : "text-accent-red"}>
                          {w.accepted ? "PASS" : "FAIL"}
                        </span>
                        <span className="text-text-muted">
                          WR: {(w.oos_win_rate * 100).toFixed(0)}%
                          · PF: {w.oos_profit_factor.toFixed(2)}
                          · Sharpe: {w.oos_sharpe.toFixed(2)}
                        </span>
                        <span className="text-text-muted ml-auto text-xs">
                          ({w.best_params.macd_fast}/{w.best_params.macd_slow}/{w.best_params.macd_signal})
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              <p className="text-xs text-accent-amber">
                Note: Walk-forward requires data to be loaded first via Breeze or CSV. API integration in progress.
              </p>
            </div>
          )}
        </div>
      </div>

      {/* Results */}
      {(report) && (
        <div className="space-y-3">
          <div className="flex items-center gap-2">
            <BarChart3 size={16} className="text-accent-blue" />
            <h2 className="text-sm font-semibold text-text-secondary uppercase tracking-wide">
              Backtest Results — {report.summary.underlying} ({report.summary.market})
            </h2>
          </div>
          <ResultsPanel report={report} />
        </div>
      )}
    </div>
  );
}

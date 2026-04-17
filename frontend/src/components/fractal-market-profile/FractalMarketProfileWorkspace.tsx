"use client";

import {
  memo,
  type ReactNode,
  useDeferredValue,
  useMemo,
  useState,
  useTransition,
} from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { clsx } from "clsx";
import {
  Activity,
  AlertTriangle,
  ArrowUpRight,
  CandlestickChart,
  Fingerprint,
  Layers3,
  Loader2,
  Radar,
  RefreshCw,
  ShieldCheck,
  WalletCards,
} from "lucide-react";
import {
  Area,
  Bar,
  CartesianGrid,
  ComposedChart,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { useLiveSnapshotQuery } from "@/hooks/useLiveSnapshotQuery";
import {
  getFractalMarketProfileLiveSnapshot,
  getFractalMarketProfilePaperJournal,
  getFractalMarketProfilePaperPositions,
  getFractalMarketProfileSummary,
  runFractalMarketProfilePaperProposal,
} from "@/lib/api";
import { createFractalMarketProfileSocket } from "@/lib/websocket";

type FMPProfile = {
  scope: string;
  hour_number?: number | null;
  completed: boolean;
  session_date: string;
  open_price: number;
  high_price: number;
  low_price: number;
  close_price: number;
  poc: number;
  vah: number;
  val: number;
  initial_balance_high: number;
  initial_balance_low: number;
  initial_balance_range: number;
  day_range: number;
  range_extension_up: number;
  range_extension_down: number;
  tick_size: number;
  single_prints: number[];
  poor_high: boolean;
  poor_low: boolean;
  shape: string;
  direction_bias: string;
  tpo_rows: { price: number; count: number; letters: string }[];
  sample_count: number;
  period_count: number;
  value_area_overlap?: number | null;
  value_migration?: number | null;
  poc_shift?: number | null;
  prior_poc_untouched?: boolean | null;
  bracket_state?: string | null;
  value_migration_step?: number | null;
  value_migration_score?: number | null;
  window_start?: string;
  window_end?: string;
};

type FMPBar = {
  time: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
};

type FMPOptionSelection = {
  option_type?: string | null;
  strike?: number | null;
  expiry?: string | null;
  premium?: number | null;
  previous_premium?: number | null;
  trading_symbol?: string | null;
  instrument_key?: string | null;
  lot_size?: number | null;
  oi?: number | null;
  oi_change?: number | null;
  volume?: number | null;
  pcr_oi?: number | null;
  iv_rank?: number | null;
  selection_reason?: string | null;
  moneyness?: string | null;
  horizon?: string | null;
  days_to_expiry?: number | null;
};

type FMPSignal = {
  signal_time?: string;
  setup_name: string;
  action: string;
  confidence: number;
  horizon: string;
  actionable: boolean;
  latest_close: number;
  entry_trigger: number;
  stop_level: number;
  target_level: number;
  hourly_shape: string;
  daily_shape: string;
  hourly_number: number;
  value_migration_score: number;
  daily_context: string;
  rationale: string[];
  filters: string[];
  options?: FMPOptionSelection | null;
  metadata?: {
    daily_direction?: string;
    order_flow_direction?: string;
    order_flow_alignment?: number;
  };
};

type FMPOrderFlowPoint = {
  timestamp: string;
  bid: number;
  ask: number;
  bid_size: number;
  ask_size: number;
  last_price: number;
};

type FMPTradePoint = {
  timestamp: string;
  price: number;
  quantity: number;
  aggressor_side: string;
};

type FMPDepthSnapshot = {
  timestamp: string;
  bids: Array<{ price: number; quantity: number }>;
  asks: Array<{ price: number; quantity: number }>;
};

type FMPOrderFlow = {
  spread: number;
  mid_price: number;
  micro_price: number;
  top_imbalance: number;
  depth_imbalance: number;
  aggressive_buy_volume: number;
  aggressive_sell_volume: number;
  delta: number;
  cumulative_delta: number;
  vwap: number;
  vwap_drift: number;
  queue_pressure: number;
  volatility_burst: number;
  passive_fill_probability: number;
  aggressive_fill_probability: number;
  adverse_selection_risk: number;
  timing_confidence: number;
  execution_aggression: string;
  micro_stop_distance: number;
  trade_imbalance?: number | null;
  order_flow_imbalance?: number | null;
  book_pressure?: number | null;
  micro_price_offset_bps?: number | null;
  trade_intensity_per_minute?: number | null;
  quote_repricing_rate?: number | null;
  toxicity_score?: number | null;
  quote_history?: FMPOrderFlowPoint[];
  trade_prints?: FMPTradePoint[];
  depth_snapshot?: FMPDepthSnapshot | null;
  source?: string | null;
};

type FMPPaperPosition = {
  position_id: string;
  status: string;
  opened_at: string;
  updated_at: string;
  closed_at?: string | null;
  underlying: string;
  setup_name: string;
  action: string;
  horizon: string;
  trading_symbol?: string | null;
  instrument_key?: string | null;
  option_type?: string | null;
  strike?: number | null;
  expiry?: string | null;
  quantity: number;
  lot_size: number;
  entry_premium: number;
  latest_premium: number;
  exit_premium?: number | null;
  realized_pnl: number;
  unrealized_pnl: number;
  stop_level: number;
  target_level: number;
  confidence: number;
  daily_shape: string;
  hourly_shape: string;
  close_reason?: string | null;
};

type FMPJournalRecord = {
  recorded_at: string;
  underlying: string;
  session_date?: string | null;
  hourly_number?: number | null;
  setup_name?: string | null;
  action?: string | null;
  confidence?: number | null;
  horizon?: string | null;
  daily_shape?: string | null;
  hourly_shape?: string | null;
  entry_trigger?: number | null;
  stop_level?: number | null;
  target_level?: number | null;
  filters?: string[];
  rationale?: string[];
  options?: FMPOptionSelection | null;
  actionable?: boolean;
};

type PaperPositionsResponse = {
  symbol_filter?: string | null;
  status: string;
  summary: {
    open_positions: number;
    closed_positions: number;
    realized_pnl: number;
    unrealized_pnl: number;
    total_pnl: number;
  };
  open_positions: FMPPaperPosition[];
  closed_positions: FMPPaperPosition[];
};

type PaperJournalResponse = {
  symbol_filter?: string | null;
  count: number;
  records: FMPJournalRecord[];
};

type ReplayTrade = {
  trade_id: string;
  underlying: string;
  setup_name: string;
  action: string;
  horizon: string;
  entry_time: string;
  exit_time: string;
  entry_underlying: number;
  exit_underlying: number;
  entry_premium: number;
  exit_premium: number;
  strike: number;
  expiry: string;
  option_type: string;
  quantity: number;
  pnl: number;
  return_pct: number;
  max_adverse_pct: number;
  max_favorable_pct: number;
  stop_level: number;
  target_level: number;
  exit_reason: string;
  confidence: number;
  daily_shape: string;
  hourly_shape: string;
};

type ReplayReport = {
  symbol: string;
  generated_at: string;
  metrics: {
    trade_count: number;
    win_rate: number;
    expectancy: number;
    profit_factor: number | null;
    max_drawdown: number;
    avg_risk_reward: number;
    trades_per_week: number;
    net_pnl: number;
  };
  thresholds: Record<string, number>;
  gate_status: Record<string, boolean>;
  equity_curve: Array<{ time: string; equity: number }>;
  setup_breakdown: Array<{
    setup_name: string;
    count: number;
    pnl: number;
    win_rate: number;
  }>;
  trades: ReplayTrade[];
};

type ReplaySuiteResponse = {
  generated_at: string;
  symbols: string[];
  reports: ReplayReport[];
};

type SummaryResponse = {
  description: string;
  supported_symbols: string[];
  paper_summary: PaperPositionsResponse["summary"];
  replay_reports: ReplayReport[];
};

type LiveSnapshotResponse = {
  session: {
    symbol: string;
    session_date: string;
    last_price: number;
    current_hour?: number | null;
    minutes_to_close: number;
  };
  daily_profile: FMPProfile;
  prior_daily_profile?: FMPProfile | null;
  hourly_profiles: FMPProfile[];
  current_hour_profile?: FMPProfile | null;
  intraday_bars_3m: FMPBar[];
  intraday_bars_30m: FMPBar[];
  filters: {
    oscillating_hourly_va: boolean;
    wide_daily_ib: boolean;
  };
  order_flow: FMPOrderFlow;
  current_signal: FMPSignal;
  paper_positions: PaperPositionsResponse;
  paper_journal: PaperJournalResponse;
  symbol_code: string;
  supported_symbols: string[];
  generated_at: string;
};

type ProfileStripColumn = {
  label: string;
  shape: string;
  score: number;
  direction: string;
  close: number;
  valueTop: number;
  valueHeight: number;
  ibTop: number;
  ibHeight: number;
  pocTop: number;
  closeTop: number;
  poorHigh: boolean;
  poorLow: boolean;
};

type ProfileStripPanelProps = {
  dailyProfile?: FMPProfile | null;
  hourlyProfiles: FMPProfile[];
  currentHour?: number | null;
};

type PriceStructurePanelProps = {
  bars: FMPBar[];
  dailyProfile?: FMPProfile | null;
  signal?: FMPSignal | null;
};

type OrderFlowPanelProps = {
  orderFlow?: FMPOrderFlow | null;
};

type ValueMigrationPanelProps = {
  dailyProfile?: FMPProfile | null;
  hourlyProfiles: FMPProfile[];
};

type TpoDistributionPanelProps = {
  dailyProfile?: FMPProfile | null;
  currentHourProfile?: FMPProfile | null;
};

type OrderFlowChartRow = {
  timestamp: string;
  label: string;
  mid: number;
  micro: number;
  spread: number;
  topImbalance: number;
  bookPressure: number;
  tradeDelta: number;
  cumulativeDelta: number;
};

function formatPrice(value?: number | null, digits = 2) {
  if (value == null || Number.isNaN(value)) return "—";
  return value.toLocaleString("en-IN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

function formatCurrency(value?: number | null, digits = 0) {
  if (value == null || Number.isNaN(value)) return "—";
  return `₹${value.toLocaleString("en-IN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`;
}

function formatSignedCurrency(value?: number | null, digits = 0) {
  if (value == null || Number.isNaN(value)) return "—";
  const prefix = value > 0 ? "+" : "";
  return `${prefix}₹${value.toLocaleString("en-IN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`;
}

function formatSignedNumber(value?: number | null, digits = 2) {
  if (value == null || Number.isNaN(value)) return "—";
  const prefix = value > 0 ? "+" : "";
  return `${prefix}${value.toLocaleString("en-IN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`;
}

function formatPercent(value?: number | null, digits = 0) {
  if (value == null || Number.isNaN(value)) return "—";
  return `${(value * 100).toLocaleString("en-IN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}%`;
}

function formatRawPercent(value?: number | null, digits = 1) {
  if (value == null || Number.isNaN(value)) return "—";
  return `${value.toLocaleString("en-IN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}%`;
}

function formatCompact(value?: number | null) {
  if (value == null || Number.isNaN(value)) return "—";
  return new Intl.NumberFormat("en-IN", {
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(value);
}

function formatDateTime(value?: string | null) {
  if (!value) return "—";
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

function formatIntradayLabel(value: string) {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleTimeString("en-IN", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

function formatShapeLabel(value?: string | null) {
  if (!value) return "—";
  return value.replaceAll("-", " ");
}

function sectionChrome(className?: string) {
  return clsx(
    "overflow-hidden rounded-[30px] border border-white/10 bg-[radial-gradient(circle_at_top_left,rgba(56,189,248,0.12),transparent_38%),linear-gradient(180deg,rgba(9,15,28,0.96),rgba(6,10,20,0.98))] shadow-[0_35px_90px_-60px_rgba(15,23,42,0.96)]",
    className,
  );
}

function toneClass(value?: number | null) {
  if (value == null || Number.isNaN(value)) return "text-slate-200";
  if (value > 0) return "text-emerald-300";
  if (value < 0) return "text-rose-300";
  return "text-slate-200";
}

function signalTone(action?: string | null) {
  if (action === "LONG") return "border-emerald-400/30 bg-emerald-400/10 text-emerald-200";
  if (action === "SHORT") return "border-rose-400/30 bg-rose-400/10 text-rose-200";
  return "border-white/10 bg-white/6 text-slate-200";
}

function replayTone(report?: ReplayReport | null) {
  if (!report) return "border-white/10 bg-white/5 text-slate-200";
  if ((report.metrics.profit_factor ?? 0) >= 1.5 && report.metrics.net_pnl > 0) {
    return "border-emerald-400/25 bg-emerald-400/8 text-emerald-200";
  }
  if (report.metrics.net_pnl < 0) {
    return "border-rose-400/25 bg-rose-400/8 text-rose-200";
  }
  return "border-sky-400/25 bg-sky-400/8 text-sky-200";
}

function normalize(value: number, min: number, max: number) {
  if (!Number.isFinite(value) || !Number.isFinite(min) || !Number.isFinite(max) || max <= min) return 50;
  return ((max - value) / (max - min)) * 100;
}

function buildOrderFlowRows(orderFlow?: FMPOrderFlow | null): OrderFlowChartRow[] {
  const quotes = [...(orderFlow?.quote_history ?? [])].sort(
    (left, right) => new Date(left.timestamp).getTime() - new Date(right.timestamp).getTime(),
  );
  if (!quotes.length) return [];
  const trades = [...(orderFlow?.trade_prints ?? [])].sort(
    (left, right) => new Date(left.timestamp).getTime() - new Date(right.timestamp).getTime(),
  );

  const rows: OrderFlowChartRow[] = [];
  let tradeIndex = 0;
  let cumulativeDelta = 0;
  let previousQuoteTs = 0;

  for (const quote of quotes) {
    const quoteTs = new Date(quote.timestamp).getTime();
    let tradeDelta = 0;

    while (tradeIndex < trades.length && new Date(trades[tradeIndex].timestamp).getTime() <= quoteTs) {
      const tradeTs = new Date(trades[tradeIndex].timestamp).getTime();
      const trade = trades[tradeIndex];
      const signedQty = (trade.aggressor_side === "buy" ? 1 : -1) * Number(trade.quantity || 0);
      if (tradeTs > previousQuoteTs) {
        tradeDelta += signedQty;
        cumulativeDelta += signedQty;
      }
      tradeIndex += 1;
    }

    const mid = (Number(quote.bid) + Number(quote.ask)) / 2;
    const denominator = Math.max(Number(quote.bid_size) + Number(quote.ask_size), 1);
    const micro = ((Number(quote.ask) * Number(quote.bid_size)) + (Number(quote.bid) * Number(quote.ask_size))) / denominator;
    rows.push({
      timestamp: quote.timestamp,
      label: formatIntradayLabel(quote.timestamp),
      mid,
      micro,
      spread: Number(quote.ask) - Number(quote.bid),
      topImbalance: (Number(quote.bid_size) - Number(quote.ask_size)) / denominator,
      bookPressure: Number(orderFlow?.book_pressure ?? 0),
      tradeDelta,
      cumulativeDelta,
    });
    previousQuoteTs = quoteTs;
  }

  return rows;
}

const SectionTitle = memo(function SectionTitle({
  icon,
  eyebrow,
  title,
  detail,
  action,
}: {
  icon: ReactNode;
  eyebrow: string;
  title: string;
  detail: string;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-start justify-between gap-4">
      <div>
        <div className="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-400">
          <span className="flex h-7 w-7 items-center justify-center rounded-full border border-white/10 bg-white/6">
            {icon}
          </span>
          <span>{eyebrow}</span>
        </div>
        <div className="mt-3 text-xl font-semibold tracking-tight text-slate-50 md:text-2xl">{title}</div>
        <p className="mt-2 max-w-3xl text-sm leading-6 text-slate-400">{detail}</p>
      </div>
      {action ? <div className="flex items-center gap-2">{action}</div> : null}
    </div>
  );
});

const StatusPill = memo(function StatusPill({
  label,
  className,
}: {
  label: string;
  className?: string;
}) {
  return (
    <span className={clsx("rounded-full border px-3 py-1 text-[11px] font-semibold uppercase tracking-[0.16em]", className)}>
      {label}
    </span>
  );
});

const MetricTile = memo(function MetricTile({
  label,
  value,
  detail,
  tone,
}: {
  label: string;
  value: string;
  detail: string;
  tone?: string;
}) {
  return (
    <div className="rounded-[24px] border border-white/8 bg-white/[0.04] px-4 py-4">
      <div className="text-[11px] uppercase tracking-[0.18em] text-slate-400">{label}</div>
      <div className={clsx("mt-2 font-mono text-lg font-semibold text-slate-50", tone)}>{value}</div>
      <div className="mt-1.5 line-clamp-2 text-xs leading-5 text-slate-400">{detail}</div>
    </div>
  );
});

const MetricRow = memo(function MetricRow({
  label,
  value,
  hint,
}: {
  label: string;
  value: string;
  hint: string;
}) {
  return (
    <div className="rounded-[22px] border border-white/8 bg-white/[0.04] px-4 py-3">
      <div className="text-[11px] uppercase tracking-[0.16em] text-slate-400">{label}</div>
      <div className="mt-1.5 font-mono text-base font-semibold text-slate-50">{value}</div>
      <div className="mt-1 text-xs leading-5 text-slate-400">{hint}</div>
    </div>
  );
});

const ReplayCard = memo(function ReplayCard({
  report,
  active,
  onClick,
}: {
  report: ReplayReport;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={clsx(
        "rounded-[24px] border px-4 py-4 text-left transition hover:border-sky-300/40 hover:bg-white/[0.08]",
        replayTone(report),
        active ? "ring-1 ring-sky-300/35" : "",
      )}
    >
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="text-sm font-semibold text-slate-50">{report.symbol}</div>
          <div className="mt-1 text-[11px] uppercase tracking-[0.16em] text-slate-400">
            {report.metrics.trade_count} trades · PF {report.metrics.profit_factor?.toFixed(2) ?? "—"}
          </div>
        </div>
        <ArrowUpRight size={15} className="text-slate-400" />
      </div>
      <div className="mt-3 grid gap-2 sm:grid-cols-3">
        <div>
          <div className="text-[11px] uppercase tracking-[0.16em] text-slate-400">Net</div>
          <div className={clsx("mt-1 font-mono text-sm font-semibold", toneClass(report.metrics.net_pnl))}>
            {formatSignedCurrency(report.metrics.net_pnl)}
          </div>
        </div>
        <div>
          <div className="text-[11px] uppercase tracking-[0.16em] text-slate-400">Win rate</div>
          <div className="mt-1 font-mono text-sm font-semibold text-slate-100">
            {formatRawPercent(report.metrics.win_rate, 1)}
          </div>
        </div>
        <div>
          <div className="text-[11px] uppercase tracking-[0.16em] text-slate-400">Max DD</div>
          <div className="mt-1 font-mono text-sm font-semibold text-rose-200">
            {formatSignedCurrency(report.metrics.max_drawdown)}
          </div>
        </div>
      </div>
    </button>
  );
});

const ProfileStripPanel = memo(function ProfileStripPanel({
  dailyProfile,
  hourlyProfiles,
  currentHour,
}: ProfileStripPanelProps) {
  const priceRange = useMemo(() => {
    const allPrices = [
      Number(dailyProfile?.high_price ?? 0),
      Number(dailyProfile?.low_price ?? 0),
      ...hourlyProfiles.flatMap((profile) => [
        Number(profile.high_price),
        Number(profile.low_price),
        Number(profile.vah),
        Number(profile.val),
        Number(profile.poc),
        Number(profile.initial_balance_high),
        Number(profile.initial_balance_low),
      ]),
    ].filter((value) => Number.isFinite(value) && value > 0);
    const min = Math.min(...allPrices);
    const max = Math.max(...allPrices);
    return {
      min,
      max,
      ticks: Array.from({ length: 6 }, (_, index) => max - ((max - min) * index) / 5),
    };
  }, [dailyProfile, hourlyProfiles]);

  const columns = useMemo<ProfileStripColumn[]>(() => {
    const min = priceRange.min;
    const max = priceRange.max;
    return hourlyProfiles.map((profile) => {
      const valueTop = normalize(profile.vah, min, max);
      const valueBottom = normalize(profile.val, min, max);
      const ibTop = normalize(profile.initial_balance_high, min, max);
      const ibBottom = normalize(profile.initial_balance_low, min, max);
      return {
        label: `H${profile.hour_number ?? "?"}`,
        shape: profile.shape,
        score: Number(profile.value_migration_score ?? 0),
        direction: profile.direction_bias,
        close: profile.close_price,
        valueTop,
        valueHeight: Math.max(valueBottom - valueTop, 1.5),
        ibTop,
        ibHeight: Math.max(ibBottom - ibTop, 1.5),
        pocTop: normalize(profile.poc, min, max),
        closeTop: normalize(profile.close_price, min, max),
        poorHigh: Boolean(profile.poor_high),
        poorLow: Boolean(profile.poor_low),
      };
    });
  }, [hourlyProfiles, priceRange.max, priceRange.min]);

  if (!dailyProfile || !hourlyProfiles.length) {
    return (
      <div className="flex h-[420px] items-center justify-center rounded-[26px] border border-white/8 bg-black/20 text-sm text-slate-400">
        No hourly profile strips are available for this session yet.
      </div>
    );
  }

  const dailyVahTop = normalize(dailyProfile.vah, priceRange.min, priceRange.max);
  const dailyValTop = normalize(dailyProfile.val, priceRange.min, priceRange.max);
  const dailyPocTop = normalize(dailyProfile.poc, priceRange.min, priceRange.max);

  return (
    <div className="rounded-[28px] border border-white/8 bg-black/20 p-4">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <div className="text-sm font-semibold text-slate-100">Profile strip panel</div>
        <div className="text-[11px] uppercase tracking-[0.16em] text-slate-400">
          Daily {dailyProfile.shape} · {dailyProfile.direction_bias}
        </div>
      </div>

      <div className="grid h-[380px] grid-cols-[56px_1fr] gap-3">
        <div className="relative h-full">
          {priceRange.ticks.map((tick) => (
            <div
              key={tick}
              className="absolute left-0 right-0 text-right text-[11px] text-slate-500"
              style={{ top: `${normalize(tick, priceRange.min, priceRange.max)}%`, transform: "translateY(-50%)" }}
            >
              {formatPrice(tick)}
            </div>
          ))}
        </div>

        <div className="relative h-full overflow-hidden rounded-[24px] border border-white/8 bg-[linear-gradient(180deg,rgba(15,23,42,0.66),rgba(6,10,20,0.94))]">
          {priceRange.ticks.map((tick) => (
            <div
              key={`grid-${tick}`}
              className="absolute inset-x-0 border-t border-dashed border-white/[0.06]"
              style={{ top: `${normalize(tick, priceRange.min, priceRange.max)}%` }}
            />
          ))}

          <div
            className="absolute inset-x-0 border-t border-sky-300/45"
            style={{ top: `${dailyVahTop}%` }}
          />
          <div
            className="absolute inset-x-0 border-t border-sky-300/45"
            style={{ top: `${dailyValTop}%` }}
          />
          <div
            className="absolute inset-x-0 border-t border-emerald-300/55"
            style={{ top: `${dailyPocTop}%` }}
          />

          <div className="absolute inset-0 grid grid-cols-6 gap-3 px-4 py-4">
            {columns.map((column, index) => {
              const isCurrent = Number(hourlyProfiles[index].hour_number ?? 0) === Number(currentHour ?? 0);
              return (
                <div
                  key={`${column.label}-${index}`}
                  className={clsx(
                    "relative h-full rounded-[22px] border px-2 py-3",
                    isCurrent
                      ? "border-sky-300/35 bg-sky-400/8 shadow-[inset_0_0_0_1px_rgba(125,211,252,0.06)]"
                      : "border-white/8 bg-white/[0.03]",
                  )}
                >
                  <div className="absolute inset-x-[22%] rounded-xl border border-sky-300/28 bg-sky-400/14" style={{ top: `${column.valueTop}%`, height: `${column.valueHeight}%` }} />
                  <div className="absolute inset-x-[34%] rounded-xl border border-amber-300/45 bg-amber-300/10" style={{ top: `${column.ibTop}%`, height: `${column.ibHeight}%` }} />
                  <div className="absolute inset-x-[18%] border-t-2 border-emerald-300" style={{ top: `${column.pocTop}%` }} />
                  <div className="absolute left-1/2 h-2.5 w-2.5 -translate-x-1/2 rounded-full border border-slate-950 bg-slate-100" style={{ top: `${column.closeTop}%`, transform: "translate(-50%, -50%)" }} />

                  <div className="absolute inset-x-0 bottom-2 text-center">
                    <div className="text-[11px] font-semibold uppercase tracking-[0.16em] text-slate-300">
                      {column.label}
                    </div>
                    <div className="mt-1 line-clamp-2 text-[10px] uppercase tracking-[0.12em] text-slate-500">
                      {column.shape.replaceAll("-", " ")}
                    </div>
                    <div className={clsx("mt-1 text-[11px] font-semibold", column.score >= 0 ? "text-emerald-200" : "text-rose-200")}>
                      {column.score >= 0 ? `+${column.score}` : column.score}
                    </div>
                    <div className="mt-1 flex items-center justify-center gap-1 text-[10px] uppercase tracking-[0.12em] text-slate-500">
                      {column.poorHigh ? <span>PH</span> : null}
                      {column.poorLow ? <span>PL</span> : null}
                      {!column.poorHigh && !column.poorLow ? <span>{column.direction}</span> : null}
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      </div>
    </div>
  );
});

const PriceStructurePanel = memo(function PriceStructurePanel({
  bars,
  dailyProfile,
  signal,
}: PriceStructurePanelProps) {
  const chartRows = useMemo(
    () =>
      bars.map((bar) => ({
        ...bar,
        label: formatIntradayLabel(bar.time),
      })),
    [bars],
  );

  const domain = useMemo(() => {
    const values = [
      ...(bars.flatMap((bar) => [Number(bar.low), Number(bar.high)])),
      Number(dailyProfile?.poc ?? 0),
      Number(dailyProfile?.vah ?? 0),
      Number(dailyProfile?.val ?? 0),
      Number(signal?.entry_trigger ?? 0),
      Number(signal?.stop_level ?? 0),
      Number(signal?.target_level ?? 0),
    ].filter((value) => Number.isFinite(value) && value > 0);
    const min = Math.min(...values);
    const max = Math.max(...values);
    const padding = (max - min) * 0.08 || 10;
    return [Math.max(min - padding, 0), max + padding];
  }, [bars, dailyProfile, signal]);

  if (!chartRows.length) {
    return (
      <div className="flex h-[420px] items-center justify-center rounded-[28px] border border-white/8 bg-black/20 text-sm text-slate-400">
        No intraday bars are available for the price structure view.
      </div>
    );
  }

  return (
    <div className="rounded-[28px] border border-white/8 bg-black/20 p-4">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <div className="text-sm font-semibold text-slate-100">Execution structure</div>
        <div className="text-[11px] uppercase tracking-[0.16em] text-slate-400">
          3-minute bars · confirmed close triggers only
        </div>
      </div>
      <div className="h-[380px]">
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart data={chartRows} margin={{ top: 12, right: 18, left: 4, bottom: 6 }}>
            <defs>
              <linearGradient id="fmpPriceFill" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="rgba(125,211,252,0.28)" />
                <stop offset="100%" stopColor="rgba(125,211,252,0.02)" />
              </linearGradient>
            </defs>
            <CartesianGrid stroke="rgba(255,255,255,0.06)" vertical={false} />
            <XAxis dataKey="label" tick={{ fill: "#94a3b8", fontSize: 11 }} axisLine={false} tickLine={false} minTickGap={18} />
            <YAxis yAxisId="price" domain={domain as [number, number]} tick={{ fill: "#94a3b8", fontSize: 11 }} axisLine={false} tickLine={false} width={68} />
            <YAxis yAxisId="volume" orientation="right" tick={{ fill: "#64748b", fontSize: 10 }} axisLine={false} tickLine={false} width={44} />
            <Tooltip
              contentStyle={{ backgroundColor: "#091120", border: "1px solid rgba(255,255,255,0.08)", borderRadius: 18 }}
              formatter={(value: number, name: string) => {
                if (name === "volume") return [formatCompact(Number(value)), "volume"];
                return [formatPrice(Number(value)), name];
              }}
            />
            {dailyProfile ? (
              <>
                <ReferenceLine yAxisId="price" y={dailyProfile.poc} stroke="#34d399" strokeDasharray="4 4" />
                <ReferenceLine yAxisId="price" y={dailyProfile.vah} stroke="#60a5fa" strokeDasharray="3 4" />
                <ReferenceLine yAxisId="price" y={dailyProfile.val} stroke="#60a5fa" strokeDasharray="3 4" />
              </>
            ) : null}
            {signal && signal.action !== "FLAT" ? (
              <>
                <ReferenceLine yAxisId="price" y={signal.entry_trigger} stroke="#facc15" strokeDasharray="3 4" />
                <ReferenceLine yAxisId="price" y={signal.stop_level} stroke="#fb7185" strokeDasharray="5 4" />
                <ReferenceLine yAxisId="price" y={signal.target_level} stroke="#22c55e" strokeDasharray="5 4" />
              </>
            ) : null}
            <Bar yAxisId="volume" dataKey="volume" barSize={11} fill="rgba(96,165,250,0.18)" radius={[5, 5, 0, 0]} />
            <Area yAxisId="price" type="monotone" dataKey="close" stroke="#e2e8f0" strokeWidth={2.2} fill="url(#fmpPriceFill)" dot={false} />
            <Line yAxisId="price" type="monotone" dataKey="high" stroke="rgba(250,204,21,0.36)" dot={false} strokeWidth={1.2} />
            <Line yAxisId="price" type="monotone" dataKey="low" stroke="rgba(248,113,113,0.26)" dot={false} strokeWidth={1.2} />
          </ComposedChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
});

const ValueMigrationPanel = memo(function ValueMigrationPanel({
  dailyProfile,
  hourlyProfiles,
}: ValueMigrationPanelProps) {
  const rows = useMemo(
    () =>
      hourlyProfiles.map((profile) => ({
        label: `H${profile.hour_number ?? "?"}`,
        close: Number(profile.close_price),
        poc: Number(profile.poc),
        vah: Number(profile.vah),
        val: Number(profile.val),
        score: Number(profile.value_migration_score ?? 0),
        dayRange: Number(profile.day_range),
        shape: profile.shape,
      })),
    [hourlyProfiles],
  );

  const priceDomain = useMemo<[number, number]>(() => {
    const values = rows.flatMap((row) => [row.close, row.poc, row.vah, row.val]);
    if (!values.length) return [0, 100];
    const min = Math.min(...values);
    const max = Math.max(...values);
    const padding = Math.max((max - min) * 0.08, 12);
    return [min - padding, max + padding];
  }, [rows]);

  if (!rows.length) {
    return (
      <div className={sectionChrome("p-5")}>
        <SectionTitle
          icon={<Activity size={14} className="text-sky-300" />}
          eyebrow="Value Migration"
          title="Hourly migration path"
          detail="The session has not built enough hourly profiles yet to draw a migration path."
        />
        <div className="mt-5 flex h-[320px] items-center justify-center rounded-[26px] border border-white/8 bg-black/20 text-sm text-slate-400">
          Waiting for completed hourly profiles.
        </div>
      </div>
    );
  }

  const latest = rows[rows.length - 1];

  return (
    <div className={sectionChrome("p-5")}>
      <SectionTitle
        icon={<Activity size={14} className="text-sky-300" />}
        eyebrow="Value Migration"
        title="Hourly migration path"
        detail="POC, value, and the close are plotted hour by hour on the same axis. This shows whether the intraday auction is truly migrating or just rotating around prior acceptance."
        action={
          dailyProfile ? (
            <StatusPill
              label={`${formatShapeLabel(dailyProfile.shape)} · ${dailyProfile.direction_bias}`}
              className="border-sky-300/25 bg-sky-400/10 text-sky-200"
            />
          ) : null
        }
      />

      <div className="mt-5 h-[320px] rounded-[26px] border border-white/8 bg-black/20 p-3">
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart data={rows} margin={{ top: 12, right: 18, left: 4, bottom: 0 }}>
            <CartesianGrid stroke="rgba(255,255,255,0.06)" vertical={false} />
            <XAxis dataKey="label" tick={{ fill: "#94a3b8", fontSize: 11 }} axisLine={false} tickLine={false} />
            <YAxis yAxisId="price" domain={priceDomain} tick={{ fill: "#94a3b8", fontSize: 11 }} axisLine={false} tickLine={false} width={70} />
            <YAxis yAxisId="score" orientation="right" domain={[-4, 4]} tick={{ fill: "#94a3b8", fontSize: 10 }} axisLine={false} tickLine={false} width={42} />
            <ReferenceLine yAxisId="score" y={0} stroke="rgba(255,255,255,0.12)" strokeDasharray="4 4" />
            <Tooltip
              contentStyle={{ backgroundColor: "#091120", border: "1px solid rgba(255,255,255,0.08)", borderRadius: 18 }}
              formatter={(value: number, name: string) => (
                name === "Score"
                  ? [formatSignedNumber(Number(value), 0), name]
                  : [formatPrice(Number(value)), name]
              )}
            />
            <Line yAxisId="price" type="monotone" dataKey="close" name="Close" stroke="#e2e8f0" strokeWidth={2.2} dot={false} />
            <Line yAxisId="price" type="monotone" dataKey="poc" name="POC" stroke="#34d399" strokeWidth={2.1} dot={false} />
            <Line yAxisId="price" type="monotone" dataKey="vah" name="VAH" stroke="#60a5fa" strokeWidth={1.5} dot={false} strokeDasharray="3 4" />
            <Line yAxisId="price" type="monotone" dataKey="val" name="VAL" stroke="#f59e0b" strokeWidth={1.5} dot={false} strokeDasharray="3 4" />
            <Line yAxisId="score" type="monotone" dataKey="score" name="Score" stroke="#c084fc" strokeWidth={2} dot={false} />
          </ComposedChart>
        </ResponsiveContainer>
      </div>

      <div className="mt-4 grid gap-3 sm:grid-cols-3">
        <MetricRow
          label="Latest migration"
          value={formatSignedNumber(latest.score, 0)}
          hint={`Current hour ${formatShapeLabel(latest.shape)} · close ${formatPrice(latest.close)}`}
        />
        <MetricRow
          label="Daily value"
          value={dailyProfile ? `${formatPrice(dailyProfile.val)} / ${formatPrice(dailyProfile.vah)}` : "—"}
          hint={dailyProfile ? `POC ${formatPrice(dailyProfile.poc)} · IB ${formatPrice(dailyProfile.initial_balance_range)}` : "Daily profile is unavailable."}
        />
        <MetricRow
          label="Session range"
          value={formatPrice(latest.dayRange)}
          hint="Latest completed hourly day-range snapshot."
        />
      </div>
    </div>
  );
});

const TpoDistributionPanel = memo(function TpoDistributionPanel({
  dailyProfile,
  currentHourProfile,
}: TpoDistributionPanelProps) {
  const renderProfile = (label: string, profile?: FMPProfile | null, accent = "bg-sky-400") => {
    if (!profile) {
      return (
        <div className="rounded-[26px] border border-white/8 bg-black/20 p-4 text-sm text-slate-400">
          {label}: no TPO distribution is available yet.
        </div>
      );
    }

    const rows = [...profile.tpo_rows]
      .sort((left, right) => right.price - left.price)
      .slice(0, 18);
    const maxCount = Math.max(...rows.map((row) => row.count), 1);

    return (
      <div className="rounded-[26px] border border-white/8 bg-black/20 p-4">
        <div className="flex items-center justify-between gap-3">
          <div>
            <div className="text-sm font-semibold text-slate-100">{label}</div>
            <div className="mt-1 text-[11px] uppercase tracking-[0.16em] text-slate-500">
              {formatShapeLabel(profile.shape)} · {profile.direction_bias}
            </div>
          </div>
          <StatusPill
            label={profile.scope === "daily" ? "30m TPO" : `H${profile.hour_number ?? "?"}`}
            className="border-white/10 bg-white/6 text-slate-300"
          />
        </div>

        <div className="mt-4 space-y-2">
          {rows.map((row) => (
            <div key={`${label}:${row.price}`} className="grid grid-cols-[72px_1fr_64px] items-center gap-3 text-xs">
              <div className="font-mono text-slate-300">{formatPrice(row.price)}</div>
              <div className="h-5 overflow-hidden rounded-full bg-white/6">
                <div
                  className={clsx("flex h-5 items-center px-2 text-[10px] text-slate-950", accent)}
                  style={{ width: `${Math.max((row.count / maxCount) * 100, 10)}%` }}
                >
                  {row.count}
                </div>
              </div>
              <div className="truncate text-right font-mono text-slate-500">{row.letters || "—"}</div>
            </div>
          ))}
        </div>

        <div className="mt-4 grid gap-3 sm:grid-cols-2">
          <MetricRow
            label="Single prints"
            value={String(profile.single_prints.length)}
            hint={profile.single_prints.length ? profile.single_prints.slice(0, 3).map((value) => formatPrice(value)).join(" · ") : "No single prints recorded."}
          />
          <MetricRow
            label="Poor extremes"
            value={`${profile.poor_high ? "PH" : "—"} / ${profile.poor_low ? "PL" : "—"}`}
            hint={`Value ${formatPrice(profile.val)} to ${formatPrice(profile.vah)}`}
          />
        </div>
      </div>
    );
  };

  return (
    <div className={sectionChrome("p-5")}>
      <SectionTitle
        icon={<Layers3 size={14} className="text-amber-300" />}
        eyebrow="TPO Distribution"
        title="Daily and current-hour distributions"
        detail="This is the raw auction print view. The daily 30-minute distribution and the active hourly fractal distribution are shown side by side so acceptance, excess, and unfinished business stand out immediately."
      />

      <div className="mt-5 grid gap-4 xl:grid-cols-2">
        {renderProfile("Daily profile", dailyProfile, "bg-sky-400")}
        {renderProfile(
          currentHourProfile ? `Current hour H${currentHourProfile.hour_number ?? "?"}` : "Current hour",
          currentHourProfile,
          "bg-emerald-400",
        )}
      </div>
    </div>
  );
});

const OrderFlowPanel = memo(function OrderFlowPanel({
  orderFlow,
}: OrderFlowPanelProps) {
  const rows = useMemo(() => buildOrderFlowRows(orderFlow), [orderFlow]);
  const depthScale = useMemo(() => {
    const levels = [
      ...(orderFlow?.depth_snapshot?.bids ?? []).map((level) => Number(level.quantity)),
      ...(orderFlow?.depth_snapshot?.asks ?? []).map((level) => Number(level.quantity)),
    ];
    return Math.max(...levels, 1);
  }, [orderFlow?.depth_snapshot?.asks, orderFlow?.depth_snapshot?.bids]);
  const priceDomain = useMemo(() => {
    if (!rows.length) return [0, 1];
    const values = rows.flatMap((row) => [row.mid, row.micro]);
    const min = Math.min(...values);
    const max = Math.max(...values);
    const padding = (max - min) * 0.08 || 2;
    return [min - padding, max + padding];
  }, [rows]);

  return (
    <div className={sectionChrome("p-5")}>
      <SectionTitle
        icon={<Radar size={14} className="text-emerald-300" />}
        eyebrow="Order Flow"
        title="Microstructure timing"
        detail="Live tape reconstruction feeds the top-book bias and signed trade pressure. When raw ticks are absent, the module falls back to a bar-proxy series so the session never goes blind."
        action={
          <StatusPill
            label={orderFlow?.source === "market_ticks" ? "Tick tape" : orderFlow?.source === "bar_proxy" ? "Bar proxy" : "Unknown"}
            className={orderFlow?.source === "market_ticks" ? "border-emerald-400/25 bg-emerald-400/10 text-emerald-200" : "border-amber-400/25 bg-amber-400/10 text-amber-200"}
          />
        }
      />

      <div className="mt-5 space-y-4">
        <div className="h-[220px] rounded-[26px] border border-white/8 bg-black/20 p-3">
          {rows.length ? (
            <ResponsiveContainer width="100%" height="100%">
              <ComposedChart data={rows} margin={{ top: 12, right: 18, left: 4, bottom: 0 }}>
                <defs>
                  <linearGradient id="fmpFlowFill" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="rgba(52,211,153,0.24)" />
                    <stop offset="100%" stopColor="rgba(52,211,153,0.02)" />
                  </linearGradient>
                </defs>
                <CartesianGrid stroke="rgba(255,255,255,0.06)" vertical={false} />
                <XAxis dataKey="label" tick={{ fill: "#94a3b8", fontSize: 11 }} axisLine={false} tickLine={false} minTickGap={18} />
                <YAxis yAxisId="price" domain={priceDomain as [number, number]} tick={{ fill: "#94a3b8", fontSize: 11 }} axisLine={false} tickLine={false} width={68} />
                <Tooltip
                  contentStyle={{ backgroundColor: "#091120", border: "1px solid rgba(255,255,255,0.08)", borderRadius: 18 }}
                  formatter={(value: number) => [formatPrice(Number(value)), "price"]}
                />
                <Area yAxisId="price" type="monotone" dataKey="mid" stroke="#e2e8f0" strokeWidth={1.9} fill="url(#fmpFlowFill)" dot={false} />
                <Line yAxisId="price" type="monotone" dataKey="micro" stroke="#34d399" strokeWidth={2.2} dot={false} />
              </ComposedChart>
            </ResponsiveContainer>
          ) : (
            <div className="flex h-full items-center justify-center text-sm text-slate-400">
              No quote or tape history is available for the order-flow chart.
            </div>
          )}
        </div>

        <div className="h-[170px] rounded-[26px] border border-white/8 bg-black/20 p-3">
          {rows.length ? (
            <ResponsiveContainer width="100%" height="100%">
              <ComposedChart data={rows} margin={{ top: 12, right: 18, left: 4, bottom: 0 }}>
                <CartesianGrid stroke="rgba(255,255,255,0.06)" vertical={false} />
                <XAxis dataKey="label" tick={{ fill: "#94a3b8", fontSize: 11 }} axisLine={false} tickLine={false} minTickGap={18} />
                <YAxis yAxisId="delta" tick={{ fill: "#94a3b8", fontSize: 11 }} axisLine={false} tickLine={false} width={60} />
                <YAxis yAxisId="ratio" orientation="right" domain={[-1, 1]} tick={{ fill: "#94a3b8", fontSize: 10 }} axisLine={false} tickLine={false} width={48} />
                <Tooltip contentStyle={{ backgroundColor: "#091120", border: "1px solid rgba(255,255,255,0.08)", borderRadius: 18 }} />
                <Bar yAxisId="delta" dataKey="tradeDelta" barSize={13} radius={[4, 4, 0, 0]} fill="rgba(52,211,153,0.50)" />
                <Line yAxisId="ratio" type="monotone" dataKey="topImbalance" stroke="#38bdf8" strokeWidth={2} dot={false} />
                <Line yAxisId="delta" type="monotone" dataKey="cumulativeDelta" stroke="#f59e0b" strokeWidth={1.8} dot={false} />
              </ComposedChart>
            </ResponsiveContainer>
          ) : (
            <div className="flex h-full items-center justify-center text-sm text-slate-400">
              No aligned signed trade flow is available for this session.
            </div>
          )}
        </div>

        <div className="grid gap-3 sm:grid-cols-2">
          <MetricRow
            label="Timing confidence"
            value={formatPercent(orderFlow?.timing_confidence, 0)}
            hint={`Execution ${orderFlow?.execution_aggression ?? "—"} · micro stop ${formatPrice(orderFlow?.micro_stop_distance)}`}
          />
          <MetricRow
            label="Trade imbalance"
            value={formatRawPercent((orderFlow?.trade_imbalance ?? 0) * 100, 1)}
            hint={`Delta ${formatSignedNumber(orderFlow?.delta, 1)} · cumulative ${formatSignedNumber(orderFlow?.cumulative_delta, 1)}`}
          />
          <MetricRow
            label="Book pressure"
            value={formatSignedNumber(orderFlow?.book_pressure, 2)}
            hint={`Top imbalance ${formatRawPercent((orderFlow?.top_imbalance ?? 0) * 100, 1)} · depth ${formatRawPercent((orderFlow?.depth_imbalance ?? 0) * 100, 1)}`}
          />
          <MetricRow
            label="Toxicity"
            value={formatPercent(orderFlow?.toxicity_score, 0)}
            hint={`Repricing ${formatPrice(orderFlow?.quote_repricing_rate, 2)} · VWAP drift ${formatPrice(orderFlow?.vwap_drift, 2)}`}
          />
        </div>

        <div className="rounded-[26px] border border-white/8 bg-black/20 p-4">
          <div className="flex items-center justify-between gap-3">
            <div>
              <div className="text-sm font-semibold text-slate-100">Depth ladder</div>
              <div className="mt-1 text-[11px] uppercase tracking-[0.16em] text-slate-500">
                Best three bid and ask levels from the live snapshot
              </div>
            </div>
            <StatusPill
              label={orderFlow?.depth_snapshot ? "Live book" : "Unavailable"}
              className={orderFlow?.depth_snapshot ? "border-emerald-400/25 bg-emerald-400/10 text-emerald-200" : "border-white/10 bg-white/6 text-slate-300"}
            />
          </div>

          {orderFlow?.depth_snapshot ? (
            <div className="mt-4 grid gap-4 xl:grid-cols-2">
              <div className="rounded-[22px] border border-white/8 bg-white/[0.04] px-4 py-4">
                <div className="mb-3 text-[11px] uppercase tracking-[0.16em] text-slate-400">Bids</div>
                <div className="space-y-2">
                  {(orderFlow.depth_snapshot.bids ?? []).map((level) => (
                    <div key={`bid-${level.price}`} className="grid grid-cols-[78px_1fr_72px] items-center gap-3 text-xs">
                      <div className="font-mono text-emerald-200">{formatPrice(level.price)}</div>
                      <div className="h-5 rounded-full bg-white/6">
                        <div
                          className="h-5 rounded-full bg-emerald-400"
                          style={{ width: `${Math.max((Number(level.quantity) / depthScale) * 100, 12)}%` }}
                        />
                      </div>
                      <div className="text-right font-mono text-slate-300">{formatCompact(level.quantity)}</div>
                    </div>
                  ))}
                </div>
              </div>

              <div className="rounded-[22px] border border-white/8 bg-white/[0.04] px-4 py-4">
                <div className="mb-3 text-[11px] uppercase tracking-[0.16em] text-slate-400">Asks</div>
                <div className="space-y-2">
                  {(orderFlow.depth_snapshot.asks ?? []).map((level) => (
                    <div key={`ask-${level.price}`} className="grid grid-cols-[78px_1fr_72px] items-center gap-3 text-xs">
                      <div className="font-mono text-rose-200">{formatPrice(level.price)}</div>
                      <div className="h-5 rounded-full bg-white/6">
                        <div
                          className="h-5 rounded-full bg-rose-400"
                          style={{ width: `${Math.max((Number(level.quantity) / depthScale) * 100, 12)}%` }}
                        />
                      </div>
                      <div className="text-right font-mono text-slate-300">{formatCompact(level.quantity)}</div>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          ) : (
            <div className="mt-4 text-sm text-slate-400">
              No depth snapshot is attached to the current tape.
            </div>
          )}
        </div>
      </div>
    </div>
  );
});

export default function FractalMarketProfileWorkspace() {
  const queryClient = useQueryClient();
  const [symbol, setSymbol] = useState("NIFTY");
  const [isPending, startTransition] = useTransition();
  const deferredSymbol = useDeferredValue(symbol);

  const summaryQuery = useQuery<SummaryResponse>({
    queryKey: ["fmp-summary"],
    queryFn: () => getFractalMarketProfileSummary().then((response) => response.data),
  });

  const liveQuery = useLiveSnapshotQuery<LiveSnapshotResponse>({
    queryKey: ["fmp-live", deferredSymbol],
    queryFn: () => getFractalMarketProfileLiveSnapshot(deferredSymbol).then((response) => response.data),
    storageKey: `nomad-curie.fmp.live.${deferredSymbol.toLowerCase()}`,
    streamFactory: (onData, onStatusChange) =>
      createFractalMarketProfileSocket(
        deferredSymbol,
        (data) => onData(data as LiveSnapshotResponse),
        onStatusChange,
      ),
  });

  const positionsQuery = useQuery<PaperPositionsResponse>({
    queryKey: ["fmp-paper-positions", deferredSymbol],
    queryFn: () =>
      getFractalMarketProfilePaperPositions(deferredSymbol, "all", 30).then((response) => response.data),
  });

  const journalQuery = useQuery<PaperJournalResponse>({
    queryKey: ["fmp-paper-journal", deferredSymbol],
    queryFn: () =>
      getFractalMarketProfilePaperJournal(deferredSymbol, 30).then((response) => response.data),
  });

  const writeLedgerMutation = useMutation({
    mutationFn: () => runFractalMarketProfilePaperProposal(deferredSymbol).then((response) => response.data as LiveSnapshotResponse),
    onSuccess: (snapshot) => {
      queryClient.setQueryData(["fmp-live", deferredSymbol], snapshot);
      queryClient.invalidateQueries({ queryKey: ["fmp-paper-positions", deferredSymbol] });
      queryClient.invalidateQueries({ queryKey: ["fmp-paper-journal", deferredSymbol] });
      queryClient.invalidateQueries({ queryKey: ["fmp-summary"] });
    },
  });

  const replayRefreshMutation = useMutation({
    mutationFn: async () => {
      const { getFractalMarketProfileReplaySuite } = await import("@/lib/api");
      return getFractalMarketProfileReplaySuite(true).then((response) => response.data as ReplaySuiteResponse);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["fmp-summary"] });
    },
  });

  const live = liveQuery.data;
  const positions = positionsQuery.data ?? live?.paper_positions;
  const journal = journalQuery.data ?? live?.paper_journal;
  const replaySuite = replayRefreshMutation.data;
  const activeReplay = useMemo(
    () =>
      replaySuite?.reports.find((report) => report.symbol.toUpperCase() === deferredSymbol.toUpperCase())
      ?? summaryQuery.data?.replay_reports.find((report) => report.symbol.toUpperCase() === deferredSymbol.toUpperCase()),
    [deferredSymbol, replaySuite, summaryQuery.data?.replay_reports],
  );

  const profileContext = live?.current_signal;
  const replayCards = useMemo(
    () => replaySuite?.reports ?? summaryQuery.data?.replay_reports ?? [],
    [replaySuite?.reports, summaryQuery.data?.replay_reports],
  );
  const equityCurveRows = useMemo(
    () =>
      (activeReplay?.equity_curve ?? []).map((point) => ({
        ...point,
        label: formatIntradayLabel(point.time),
      })),
    [activeReplay?.equity_curve],
  );

  const openPositions = positions?.open_positions ?? [];
  const closedPositions = positions?.closed_positions ?? [];
  const journalRecords = journal?.records ?? [];

  return (
    <div className="mx-auto max-w-[1660px] space-y-6 pb-10">
      <section className={sectionChrome("px-6 py-6 md:px-8")}>
        <SectionTitle
          icon={<Fingerprint size={15} className="text-sky-300" />}
          eyebrow="Separate Strategy Desk"
          title="Fractal Market Profile"
          detail="Daily 30-minute TPO and nested hourly 3-minute profile logic now run as an isolated options-buying strategy. The desk tracks live profile migration, order-flow confirmation, paper positions, and replay validation for NIFTY and SENSEX."
          action={
            <div className="flex flex-wrap items-center gap-2">
              <StatusPill
                label={liveQuery.isStreamConnected ? "Streaming" : liveQuery.isShowingSnapshot ? "Snapshot" : "Bootstrap"}
                className={liveQuery.isStreamConnected ? "border-emerald-400/25 bg-emerald-400/10 text-emerald-200" : "border-amber-400/25 bg-amber-400/10 text-amber-200"}
              />
              <button
                type="button"
                onClick={() => liveQuery.refetch()}
                className="inline-flex items-center gap-2 rounded-full border border-white/10 bg-white/6 px-4 py-2 text-sm font-semibold text-slate-100 transition hover:bg-white/10"
              >
                <RefreshCw size={14} />
                Refresh snapshot
              </button>
              <button
                type="button"
                onClick={() => writeLedgerMutation.mutate()}
                disabled={writeLedgerMutation.isPending}
                className="inline-flex items-center gap-2 rounded-full border border-sky-300/25 bg-sky-400/10 px-4 py-2 text-sm font-semibold text-sky-100 transition hover:bg-sky-400/16 disabled:cursor-not-allowed disabled:opacity-60"
              >
                {writeLedgerMutation.isPending ? <Loader2 size={14} className="animate-spin" /> : <WalletCards size={14} />}
                Write ledger
              </button>
              <button
                type="button"
                onClick={() => replayRefreshMutation.mutate()}
                disabled={replayRefreshMutation.isPending}
                className="inline-flex items-center gap-2 rounded-full border border-emerald-300/25 bg-emerald-400/10 px-4 py-2 text-sm font-semibold text-emerald-100 transition hover:bg-emerald-400/16 disabled:cursor-not-allowed disabled:opacity-60"
              >
                {replayRefreshMutation.isPending ? <Loader2 size={14} className="animate-spin" /> : <Activity size={14} />}
                Refresh replays
              </button>
            </div>
          }
        />

        <div className="mt-6 flex flex-wrap items-center justify-between gap-4">
          <div className="flex flex-wrap gap-2">
            {(summaryQuery.data?.supported_symbols ?? ["NIFTY", "SENSEX"]).map((candidate) => (
              <button
                key={candidate}
                type="button"
                onClick={() => startTransition(() => setSymbol(candidate))}
                className={clsx(
                  "rounded-full border px-4 py-2 text-sm font-semibold transition",
                  deferredSymbol === candidate
                    ? "border-sky-300/35 bg-sky-400/12 text-sky-100"
                    : "border-white/10 bg-white/6 text-slate-300 hover:bg-white/10",
                )}
              >
                {isPending && deferredSymbol !== candidate ? `${candidate}…` : candidate}
              </button>
            ))}
          </div>
          <div className="flex flex-wrap items-center gap-3 text-xs text-slate-400">
            <span>Generated {formatDateTime(live?.generated_at)}</span>
            <span>Session {live?.session.session_date ?? "—"}</span>
            <span>{live?.session.minutes_to_close ?? "—"} min to close</span>
          </div>
        </div>
      </section>

      <section className="grid gap-4 xl:grid-cols-6">
        <MetricTile
          label="Current setup"
          value={(profileContext?.setup_name ?? "waiting").replaceAll("_", " ")}
          detail={profileContext ? `${profileContext.daily_shape} daily · ${profileContext.hourly_shape} hourly` : "Waiting for the latest live session analysis."}
          tone={profileContext?.actionable ? "text-sky-200" : "text-slate-100"}
        />
        <MetricTile
          label="Signal state"
          value={profileContext?.action ?? "FLAT"}
          detail={profileContext ? `${profileContext.horizon} horizon · confidence ${formatPercent(profileContext.confidence, 0)}` : "No current action."}
          tone={profileContext?.action === "LONG" ? "text-emerald-200" : profileContext?.action === "SHORT" ? "text-rose-200" : "text-slate-100"}
        />
        <MetricTile
          label="Value migration"
          value={profileContext ? `${profileContext.value_migration_score >= 0 ? "+" : ""}${profileContext.value_migration_score}` : "—"}
          detail={live?.filters.oscillating_hourly_va ? "Oscillating hourly value detected." : "Hourly migration is stable enough for signal scoring."}
          tone={live?.filters.oscillating_hourly_va ? "text-amber-200" : "text-slate-100"}
        />
        <MetricTile
          label="Paper book"
          value={String(positions?.summary.open_positions ?? 0)}
          detail={positions ? `Realized ${formatSignedCurrency(positions.summary.realized_pnl)} · unrealized ${formatSignedCurrency(positions.summary.unrealized_pnl)}` : "Persisted paper positions unavailable."}
        />
        <MetricTile
          label="Replay edge"
          value={activeReplay ? `${activeReplay.metrics.profit_factor?.toFixed(2) ?? "—"} PF` : "—"}
          detail={activeReplay ? `${activeReplay.metrics.trade_count} trades · net ${formatSignedCurrency(activeReplay.metrics.net_pnl)}` : "Replay suite not loaded yet."}
          tone={activeReplay && (activeReplay.metrics.profit_factor ?? 0) >= 1.4 ? "text-emerald-200" : "text-slate-100"}
        />
        <MetricTile
          label="Options mapping"
          value={profileContext?.options?.option_type ? `${profileContext.options.option_type} ${profileContext.options.moneyness ?? "ATM"}` : "—"}
          detail={profileContext?.options ? `${formatPrice(profileContext.options.strike)} strike · ${profileContext.options.expiry} · premium ${formatCurrency(profileContext.options.premium, 2)}` : "No active options mapping on the current signal."}
        />
      </section>

      <section className="grid gap-6 xl:grid-cols-[1.12fr_0.88fr]">
        <div className={sectionChrome("p-5")}>
          <SectionTitle
            icon={<Layers3 size={14} className="text-sky-300" />}
            eyebrow="Market Profile"
            title={`${deferredSymbol} day structure`}
            detail="The strip panel scales every completed hourly profile against the same price ladder, with daily value references carried across the session. This is the operator view for the nested fractal auction."
            action={
              live?.daily_profile ? (
                <StatusPill
                  label={`${live.daily_profile.shape} · ${live.daily_profile.direction_bias}`}
                  className="border-sky-300/25 bg-sky-400/10 text-sky-200"
                />
              ) : null
            }
          />
          <div className="mt-5">
            <ProfileStripPanel
              dailyProfile={live?.daily_profile}
              hourlyProfiles={live?.hourly_profiles ?? []}
              currentHour={live?.session.current_hour}
            />
          </div>
        </div>

        <div className={sectionChrome("p-5")}>
          <SectionTitle
            icon={<CandlestickChart size={14} className="text-amber-300" />}
            eyebrow="Execution Context"
            title="Intraday trigger map"
            detail="Confirmed 3-minute closes drive entries. The chart keeps the daily value ladder, live trigger, stop, and target on the same axis so option execution can stay tied to the underlying auction."
          />
          <div className="mt-5">
            <PriceStructurePanel
              bars={live?.intraday_bars_3m ?? []}
              dailyProfile={live?.daily_profile}
              signal={live?.current_signal}
            />
          </div>
        </div>
      </section>

      <section className="grid gap-6 xl:grid-cols-[1.02fr_0.98fr]">
        <ValueMigrationPanel
          dailyProfile={live?.daily_profile}
          hourlyProfiles={live?.hourly_profiles ?? []}
        />
        <TpoDistributionPanel
          dailyProfile={live?.daily_profile}
          currentHourProfile={live?.current_hour_profile}
        />
      </section>

      <section className="grid gap-6 xl:grid-cols-[0.92fr_1.08fr]">
        <OrderFlowPanel orderFlow={live?.order_flow} />

        <div className={sectionChrome("p-5")}>
          <SectionTitle
            icon={<ShieldCheck size={14} className="text-emerald-300" />}
            eyebrow="Signal and Options"
            title="Decision packet"
            detail="This pane is the exact operator packet: thesis, filters, option mapping, and risk ladder. It is meant to answer whether the profile, the tape, and the option overlay agree enough to buy premium."
            action={
              profileContext ? (
                <StatusPill
                  label={profileContext.actionable ? "Actionable" : "Stand down"}
                  className={profileContext.actionable ? "border-emerald-400/25 bg-emerald-400/10 text-emerald-200" : "border-amber-400/25 bg-amber-400/10 text-amber-200"}
                />
              ) : null
            }
          />

          <div className="mt-5 grid gap-4 xl:grid-cols-[0.9fr_1.1fr]">
            <div className="space-y-3">
              <div className={clsx("rounded-[24px] border px-4 py-4", signalTone(profileContext?.action))}>
                <div className="text-[11px] uppercase tracking-[0.16em] text-slate-300">Signal</div>
                <div className="mt-2 text-xl font-semibold text-slate-50">
                  {profileContext?.action ?? "FLAT"} · {(profileContext?.setup_name ?? "waiting").replaceAll("_", " ")}
                </div>
                <div className="mt-2 text-sm text-slate-300">
                  Hour {live?.session.current_hour ?? "—"} · {profileContext?.daily_context ?? "No context"} · confidence {formatPercent(profileContext?.confidence, 0)}
                </div>
              </div>

              <MetricRow
                label="Entry / stop / target"
                value={profileContext ? `${formatPrice(profileContext.entry_trigger)} · ${formatPrice(profileContext.stop_level)} · ${formatPrice(profileContext.target_level)}` : "—"}
                hint="Underlying levels only. The options trade is mapped after the signal passes the profile and order-flow filters."
              />
              <MetricRow
                label="Daily filters"
                value={live?.filters.wide_daily_ib ? "Wide IB" : "Clear"}
                hint={live?.filters.oscillating_hourly_va ? "Hourly value is oscillating." : "Hourly value migration is directional."}
              />
              <MetricRow
                label="Order-flow alignment"
                value={formatPercent(profileContext?.metadata?.order_flow_alignment, 0)}
                hint={`${profileContext?.metadata?.order_flow_direction ?? "neutral"} flow vs ${profileContext?.metadata?.daily_direction ?? "neutral"} daily bias`}
              />
            </div>

            <div className="space-y-4">
              <div className="rounded-[24px] border border-white/8 bg-white/[0.04] px-4 py-4">
                <div className="text-[11px] uppercase tracking-[0.16em] text-slate-400">Option mapping</div>
                {profileContext?.options ? (
                  <div className="mt-3 grid gap-3 sm:grid-cols-2">
                    <MetricRow
                      label="Contract"
                      value={`${profileContext.options.option_type ?? "—"} ${formatPrice(profileContext.options.strike)} ${profileContext.options.expiry ?? ""}`.trim()}
                      hint={profileContext.options.trading_symbol ?? profileContext.options.instrument_key ?? "No live symbol attached."}
                    />
                    <MetricRow
                      label="Premium"
                      value={formatCurrency(profileContext.options.premium, 2)}
                      hint={`Prev ${formatCurrency(profileContext.options.previous_premium, 2)} · lot ${profileContext.options.lot_size ?? "—"}`}
                    />
                    <MetricRow
                      label="Flow overlay"
                      value={`OI ${formatCompact(profileContext.options.oi)} · Δ ${formatCompact(profileContext.options.oi_change)}`}
                      hint={`PCR ${formatPrice(profileContext.options.pcr_oi, 2)} · IV rank ${formatPrice(profileContext.options.iv_rank, 1)}`}
                    />
                    <MetricRow
                      label="Selection"
                      value={`${profileContext.options.moneyness ?? "ATM"} · ${profileContext.options.horizon ?? profileContext.horizon}`}
                      hint={profileContext.options.selection_reason ?? "No selection rationale captured."}
                    />
                  </div>
                ) : (
                  <div className="mt-3 text-sm text-slate-400">
                    No options contract is attached to the current signal.
                  </div>
                )}
              </div>

              <div className="grid gap-4 xl:grid-cols-2">
                <div className="rounded-[24px] border border-white/8 bg-white/[0.04] px-4 py-4">
                  <div className="text-[11px] uppercase tracking-[0.16em] text-slate-400">Rationale</div>
                  <div className="mt-3 space-y-2">
                    {(profileContext?.rationale?.length ? profileContext.rationale : ["Waiting for the next profile trigger."]).map((item) => (
                      <div key={item} className="rounded-2xl border border-white/8 bg-white/[0.03] px-3 py-2 text-sm leading-6 text-slate-300">
                        {item}
                      </div>
                    ))}
                  </div>
                </div>
                <div className="rounded-[24px] border border-white/8 bg-white/[0.04] px-4 py-4">
                  <div className="text-[11px] uppercase tracking-[0.16em] text-slate-400">Active filters</div>
                  <div className="mt-3 space-y-2">
                    {(profileContext?.filters?.length ? profileContext.filters : ["No blocking filters on the current packet."]).map((item) => (
                      <div
                        key={item}
                        className={clsx(
                          "rounded-2xl border px-3 py-2 text-sm leading-6",
                          profileContext?.filters?.length
                            ? "border-amber-400/22 bg-amber-400/8 text-amber-100"
                            : "border-white/8 bg-white/[0.03] text-slate-300",
                        )}
                      >
                        {item}
                      </div>
                    ))}
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </section>

      <section className="grid gap-6 xl:grid-cols-[1.04fr_0.96fr]">
        <div className={sectionChrome("p-5")}>
          <SectionTitle
            icon={<WalletCards size={14} className="text-amber-300" />}
            eyebrow="Persisted Paper Book"
            title="Positions and ledger"
            detail="The FMP desk keeps its own persisted paper ledger. Open and closed options positions are stored separately from the Auction IQ book so this strategy can be evaluated on its own lifecycle and metrics."
          />

          <div className="mt-5 grid gap-4 xl:grid-cols-3">
            <MetricRow
              label="Open"
              value={String(positions?.summary.open_positions ?? 0)}
              hint={`Unrealized ${formatSignedCurrency(positions?.summary.unrealized_pnl)}`}
            />
            <MetricRow
              label="Closed"
              value={String(positions?.summary.closed_positions ?? 0)}
              hint={`Realized ${formatSignedCurrency(positions?.summary.realized_pnl)}`}
            />
            <MetricRow
              label="Total"
              value={formatSignedCurrency(positions?.summary.total_pnl)}
              hint={`Journal entries ${journal?.count ?? 0}`}
            />
          </div>

          <div className="mt-5 grid gap-4 xl:grid-cols-2">
            <div className="rounded-[26px] border border-white/8 bg-black/20 p-4">
              <div className="mb-3 text-sm font-semibold text-slate-100">Open positions</div>
              <div className="space-y-2">
                {openPositions.length ? (
                  openPositions.map((position) => (
                    <div key={position.position_id} className="rounded-[20px] border border-white/8 bg-white/[0.04] px-3 py-3">
                      <div className="flex items-start justify-between gap-3">
                        <div>
                          <div className="text-sm font-semibold text-slate-100">
                            {position.action} {position.option_type} {formatPrice(position.strike)} {position.expiry}
                          </div>
                          <div className="mt-1 text-[11px] uppercase tracking-[0.16em] text-slate-400">
                            {position.setup_name.replaceAll("_", " ")} · {position.horizon}
                          </div>
                        </div>
                        <div className={clsx("font-mono text-sm font-semibold", toneClass(position.unrealized_pnl))}>
                          {formatSignedCurrency(position.unrealized_pnl)}
                        </div>
                      </div>
                      <div className="mt-3 grid gap-2 sm:grid-cols-3">
                        <MetricRow label="Premium" value={`${formatCurrency(position.entry_premium, 2)} → ${formatCurrency(position.latest_premium, 2)}`} hint={`Qty ${position.quantity}`} />
                        <MetricRow label="Risk" value={`${formatPrice(position.stop_level)} / ${formatPrice(position.target_level)}`} hint={`${position.daily_shape} · ${position.hourly_shape}`} />
                        <MetricRow label="Opened" value={formatDateTime(position.opened_at)} hint={`Confidence ${formatPercent(position.confidence, 0)}`} />
                      </div>
                    </div>
                  ))
                ) : (
                  <div className="rounded-[20px] border border-dashed border-white/10 px-3 py-8 text-center text-sm text-slate-400">
                    No open FMP paper positions for {deferredSymbol}.
                  </div>
                )}
              </div>
            </div>

            <div className="rounded-[26px] border border-white/8 bg-black/20 p-4">
              <div className="mb-3 text-sm font-semibold text-slate-100">Ledger tape</div>
              <div className="space-y-2">
                {journalRecords.length ? (
                  journalRecords.map((record, index) => (
                    <div key={`${record.recorded_at}-${index}`} className="rounded-[20px] border border-white/8 bg-white/[0.04] px-3 py-3">
                      <div className="flex items-start justify-between gap-3">
                        <div>
                          <div className="text-sm font-semibold text-slate-100">
                            {(record.action ?? "FLAT")} · {(record.setup_name ?? "no setup").replaceAll("_", " ")}
                          </div>
                          <div className="mt-1 text-[11px] uppercase tracking-[0.16em] text-slate-400">
                            {formatDateTime(record.recorded_at)} · hour {record.hourly_number ?? "—"}
                          </div>
                        </div>
                        <StatusPill
                          label={record.actionable ? "actionable" : "logged"}
                          className={record.actionable ? "border-emerald-400/25 bg-emerald-400/10 text-emerald-200" : "border-white/10 bg-white/6 text-slate-300"}
                        />
                      </div>
                      <div className="mt-3 grid gap-2 sm:grid-cols-2">
                        <MetricRow
                          label="Trigger ladder"
                          value={`${formatPrice(record.entry_trigger)} · ${formatPrice(record.stop_level)} · ${formatPrice(record.target_level)}`}
                          hint={`${record.daily_shape ?? "—"} daily · ${record.hourly_shape ?? "—"} hourly`}
                        />
                        <MetricRow
                          label="Contract"
                          value={record.options?.option_type ? `${record.options.option_type} ${formatPrice(record.options.strike)} ${record.options.expiry ?? ""}` : "No option"}
                          hint={record.options?.selection_reason ?? "No mapped option on this packet."}
                        />
                      </div>
                    </div>
                  ))
                ) : (
                  <div className="rounded-[20px] border border-dashed border-white/10 px-3 py-8 text-center text-sm text-slate-400">
                    No persisted FMP journal entries for {deferredSymbol}.
                  </div>
                )}
              </div>
            </div>
          </div>

          <div className="mt-4 rounded-[26px] border border-white/8 bg-black/20 p-4">
            <div className="mb-3 text-sm font-semibold text-slate-100">Recently closed</div>
            <div className="overflow-x-auto">
              <table className="min-w-full text-left text-sm">
                <thead className="text-[11px] uppercase tracking-[0.16em] text-slate-500">
                  <tr>
                    <th className="pb-3 font-medium">Contract</th>
                    <th className="pb-3 font-medium">Setup</th>
                    <th className="pb-3 font-medium">Opened</th>
                    <th className="pb-3 font-medium">Closed</th>
                    <th className="pb-3 font-medium">P&L</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-white/8">
                  {closedPositions.length ? (
                    closedPositions.slice(0, 10).map((position) => (
                      <tr key={position.position_id} className="text-slate-300">
                        <td className="py-3">
                          {position.option_type} {formatPrice(position.strike)} {position.expiry}
                        </td>
                        <td className="py-3">{position.setup_name.replaceAll("_", " ")}</td>
                        <td className="py-3">{formatDateTime(position.opened_at)}</td>
                        <td className="py-3">{formatDateTime(position.closed_at)}</td>
                        <td className={clsx("py-3 font-mono font-semibold", toneClass(position.realized_pnl))}>
                          {formatSignedCurrency(position.realized_pnl)}
                        </td>
                      </tr>
                    ))
                  ) : (
                    <tr>
                      <td colSpan={5} className="py-6 text-center text-slate-400">
                        No closed FMP positions yet.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </div>

        <div className={sectionChrome("p-5")}>
          <SectionTitle
            icon={<Activity size={14} className="text-emerald-300" />}
            eyebrow="Replay Validation"
            title="NIFTY and SENSEX results"
            detail="This panel shows the cached replay suite on local minute history. It keeps the strategy isolated from Auction IQ, with dedicated metrics and trade logs per index."
            action={
              summaryQuery.isFetching || replayRefreshMutation.isPending ? (
                <StatusPill
                  label="Running"
                  className="border-amber-400/25 bg-amber-400/10 text-amber-200"
                />
              ) : (
                <StatusPill
                  label={replayCards.length ? "Ready" : "Waiting"}
                  className={replayCards.length ? "border-emerald-400/25 bg-emerald-400/10 text-emerald-200" : "border-white/10 bg-white/6 text-slate-300"}
                />
              )
            }
          />

          <div className="mt-5 grid gap-3">
            {replayCards.length ? (
              replayCards.map((report) => (
                <ReplayCard
                  key={report.symbol}
                  report={report}
                  active={report.symbol === deferredSymbol}
                  onClick={() => startTransition(() => setSymbol(report.symbol))}
                />
              ))
            ) : (
              <div className="rounded-[24px] border border-dashed border-white/10 px-4 py-10 text-center text-sm text-slate-400">
                Replay suite is not available yet.
              </div>
            )}
          </div>

          <div className="mt-5 space-y-4">
            <div className="grid gap-3 sm:grid-cols-2">
              <MetricRow
                label="Trades / week"
                value={activeReplay ? String(activeReplay.metrics.trades_per_week) : "—"}
                hint={`Expectancy ${activeReplay ? formatCurrency(activeReplay.metrics.expectancy) : "—"} · RR ${activeReplay ? activeReplay.metrics.avg_risk_reward.toFixed(2) : "—"}`}
              />
              <MetricRow
                label="Gate status"
                value={activeReplay ? `${Object.values(activeReplay.gate_status).filter(Boolean).length}/${Object.keys(activeReplay.gate_status).length} pass` : "—"}
                hint={activeReplay ? Object.entries(activeReplay.gate_status).map(([key, ok]) => `${key}:${ok ? "ok" : "fail"}`).join(" · ") : "No replay gate state."}
              />
            </div>

            <div className="h-[220px] rounded-[26px] border border-white/8 bg-black/20 p-3">
              {equityCurveRows.length ? (
                <ResponsiveContainer width="100%" height="100%">
                  <ComposedChart data={equityCurveRows} margin={{ top: 12, right: 18, left: 4, bottom: 0 }}>
                    <defs>
                      <linearGradient id="fmpReplayFill" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="0%" stopColor="rgba(34,197,94,0.28)" />
                        <stop offset="100%" stopColor="rgba(34,197,94,0.02)" />
                      </linearGradient>
                    </defs>
                    <CartesianGrid stroke="rgba(255,255,255,0.06)" vertical={false} />
                    <XAxis dataKey="label" tick={{ fill: "#94a3b8", fontSize: 11 }} axisLine={false} tickLine={false} minTickGap={20} />
                    <YAxis tick={{ fill: "#94a3b8", fontSize: 11 }} axisLine={false} tickLine={false} width={70} />
                    <Tooltip
                      contentStyle={{ backgroundColor: "#091120", border: "1px solid rgba(255,255,255,0.08)", borderRadius: 18 }}
                      formatter={(value: number) => [formatCurrency(Number(value)), "equity"]}
                    />
                    <Line type="monotone" dataKey="equity" stroke="#22c55e" strokeWidth={2.2} dot={false} />
                    <Area type="monotone" dataKey="equity" stroke="transparent" fill="url(#fmpReplayFill)" />
                  </ComposedChart>
                </ResponsiveContainer>
              ) : (
                <div className="flex h-full items-center justify-center text-sm text-slate-400">
                  No replay equity curve loaded.
                </div>
              )}
            </div>

            <div className="rounded-[26px] border border-white/8 bg-black/20 p-4">
              <div className="mb-3 text-sm font-semibold text-slate-100">Setup breakdown</div>
              <div className="space-y-2">
                {(activeReplay?.setup_breakdown ?? []).length ? (
                  activeReplay?.setup_breakdown.map((setup) => (
                    <div key={setup.setup_name} className="rounded-[20px] border border-white/8 bg-white/[0.04] px-3 py-3">
                      <div className="flex items-center justify-between gap-3">
                        <div>
                          <div className="text-sm font-semibold text-slate-100">{setup.setup_name.replaceAll("_", " ")}</div>
                          <div className="mt-1 text-[11px] uppercase tracking-[0.16em] text-slate-500">
                            {setup.count} trades · win rate {formatRawPercent(setup.win_rate, 1)}
                          </div>
                        </div>
                        <div className={clsx("font-mono text-sm font-semibold", toneClass(setup.pnl))}>
                          {formatSignedCurrency(setup.pnl)}
                        </div>
                      </div>
                    </div>
                  ))
                ) : (
                  <div className="rounded-[20px] border border-dashed border-white/10 px-3 py-8 text-center text-sm text-slate-400">
                    No setup breakdown is available for this replay.
                  </div>
                )}
              </div>
            </div>

            <div className="rounded-[26px] border border-white/8 bg-black/20 p-4">
              <div className="mb-3 text-sm font-semibold text-slate-100">Recent replay trades</div>
              <div className="overflow-x-auto">
                <table className="min-w-full text-left text-sm">
                  <thead className="text-[11px] uppercase tracking-[0.16em] text-slate-500">
                    <tr>
                      <th className="pb-3 font-medium">Setup</th>
                      <th className="pb-3 font-medium">Contract</th>
                      <th className="pb-3 font-medium">Entry</th>
                      <th className="pb-3 font-medium">Exit</th>
                      <th className="pb-3 font-medium">P&L</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-white/8">
                    {activeReplay?.trades?.length ? (
                      activeReplay.trades.slice(0, 10).map((trade) => (
                        <tr key={trade.trade_id} className="text-slate-300">
                          <td className="py-3">{trade.setup_name.replaceAll("_", " ")}</td>
                          <td className="py-3">{trade.option_type} {formatPrice(trade.strike)} {trade.expiry}</td>
                          <td className="py-3">{formatDateTime(trade.entry_time)}</td>
                          <td className="py-3">{formatDateTime(trade.exit_time)}</td>
                          <td className={clsx("py-3 font-mono font-semibold", toneClass(trade.pnl))}>
                            {formatSignedCurrency(trade.pnl)}
                          </td>
                        </tr>
                      ))
                    ) : (
                      <tr>
                        <td colSpan={5} className="py-6 text-center text-slate-400">
                          No replay trades loaded for {deferredSymbol}.
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        </div>
      </section>

      {(liveQuery.isError || summaryQuery.isError || positionsQuery.isError || journalQuery.isError) ? (
        <section className="rounded-[28px] border border-rose-400/20 bg-rose-400/8 px-5 py-4 text-sm text-rose-100">
          <div className="flex items-start gap-3">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
            <div>
              <div className="font-semibold">One or more FMP surfaces failed to load.</div>
              <div className="mt-1 text-rose-100/80">
                The page is still usable with the latest cached snapshot where available, but one of the live, replay, or ledger queries returned an error.
              </div>
            </div>
          </div>
        </section>
      ) : null}
    </div>
  );
}

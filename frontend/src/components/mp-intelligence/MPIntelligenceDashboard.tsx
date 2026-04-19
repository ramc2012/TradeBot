"use client";

import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { clsx } from "clsx";
import {
  Activity,
  AlertTriangle,
  BarChart2,
  Brain,
  CheckCircle2,
  ChevronDown,
  Layers3,
  RefreshCw,
  TrendingDown,
  TrendingUp,
  Zap,
} from "lucide-react";
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ComposedChart,
  Legend,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { getMPAnalytics } from "@/lib/api";

// ─── Colour palette ───────────────────────────────────────────────────────────

const COLORS = {
  trend_up: "#22c55e",
  trend_dn: "#ef4444",
  normal_var_up: "#86efac",
  normal_var_dn: "#fca5a5",
  failed_auction: "#f59e0b",
  double_dist: "#a78bfa",
  normal: "#94a3b8",
  unknown: "#475569",
  poc: "#f59e0b",
  vah: "#34d399",
  val: "#60a5fa",
  va_center: "#c084fc",
  cvd_bull: "#22c55e",
  cvd_bear: "#ef4444",
  drift_alert: "#f97316",
  stable: "#22c55e",
};

const DAY_TYPE_COLOR: Record<string, string> = {
  TREND_UP: COLORS.trend_up,
  TREND_DN: COLORS.trend_dn,
  NORMAL_VAR_UP: COLORS.normal_var_up,
  NORMAL_VAR_DN: COLORS.normal_var_dn,
  FAILED_AUCTION: COLORS.failed_auction,
  DOUBLE_DIST: COLORS.double_dist,
  NORMAL: COLORS.normal,
  UNKNOWN: COLORS.unknown,
};

const UNDERLYINGS = ["NIFTY", "BANKNIFTY", "SENSEX"];

// ─── Helpers ──────────────────────────────────────────────────────────────────

const fmt = (n: number | undefined | null, dp = 1) =>
  n == null ? "—" : n.toFixed(dp);

const pct = (n: number | undefined | null) =>
  n == null ? "—" : `${n.toFixed(1)}%`;

const shortDate = (s: string) => s?.slice(5) ?? "";

// ─── Sub-components ───────────────────────────────────────────────────────────

function SectionHeader({
  icon: Icon,
  title,
  sub,
}: {
  icon: React.ElementType;
  title: string;
  sub?: string;
}) {
  return (
    <div className="flex items-center gap-2 mb-3">
      <Icon className="w-4 h-4 text-blue-400 shrink-0" />
      <span className="text-sm font-semibold text-zinc-200">{title}</span>
      {sub && <span className="text-xs text-zinc-500 ml-1">{sub}</span>}
    </div>
  );
}

function Pill({
  label,
  color,
  small,
}: {
  label: string;
  color: string;
  small?: boolean;
}) {
  return (
    <span
      className={clsx(
        "inline-flex items-center rounded-full font-medium",
        small ? "px-1.5 py-0.5 text-[10px]" : "px-2 py-0.5 text-xs",
      )}
      style={{ backgroundColor: `${color}22`, color }}
    >
      {label}
    </span>
  );
}

function StatCard({
  label,
  value,
  sub,
  accent,
}: {
  label: string;
  value: string | number;
  sub?: string;
  accent?: string;
}) {
  return (
    <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-3">
      <p className="text-[11px] text-zinc-500 mb-1">{label}</p>
      <p
        className="text-xl font-bold font-mono"
        style={{ color: accent ?? "#e4e4e7" }}
      >
        {value}
      </p>
      {sub && <p className="text-[10px] text-zinc-600 mt-0.5">{sub}</p>}
    </div>
  );
}

// ─── Value Migration Panel ────────────────────────────────────────────────────

function ValueMigrationPanel({ data }: { data: any }) {
  if (!data?.sessions?.length)
    return (
      <p className="text-xs text-zinc-500">No value migration data available.</p>
    );

  const sessions = data.sessions;
  const summary = data.summary;

  // ── Compute tight domains from actual data ──────────────────────────────
  const allPriceLevels = sessions.flatMap((s: any) => [s.vah, s.val, s.poc]).filter(Boolean);
  const priceMin = Math.min(...allPriceLevels);
  const priceMax = Math.max(...allPriceLevels);
  const pricePad = Math.max((priceMax - priceMin) * 0.08, 50);
  const priceDomain = [Math.floor(priceMin - pricePad), Math.ceil(priceMax + pricePad)];

  const vaWidths = sessions.map((s: any) => s.va_width).filter(Boolean);
  const vaWidthMin = Math.min(...vaWidths);
  const vaWidthMax = Math.max(...vaWidths);
  const vaWidthPad = (vaWidthMax - vaWidthMin) * 0.12;
  const vaWidthDomain = [Math.floor(vaWidthMin - vaWidthPad), Math.ceil(vaWidthMax + vaWidthPad)];

  const netFails = sessions.map((s: any) => s.net_failure).filter((v: any) => v != null);
  const failAbs = Math.max(...netFails.map(Math.abs), 1);
  const failDomain = [-(failAbs + 1), failAbs + 1];

  const pocShifts = sessions.map((s: any) => s.poc_shift).filter((v: any) => v != null && v !== 0);
  const shiftAbs = pocShifts.length ? Math.max(...pocShifts.map(Math.abs), 10) : 50;
  const shiftDomain = [-(shiftAbs + 10), shiftAbs + 10];

  return (
    <div className="space-y-4">
      {/* Summary row */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
        <StatCard
          label="Cumulative POC Shift"
          value={fmt(summary?.cumulative_poc_shift, 0)}
          accent={
            (summary?.cumulative_poc_shift ?? 0) >= 0
              ? COLORS.trend_up
              : COLORS.trend_dn
          }
        />
        <StatCard
          label="Upward Migration"
          value={pct(summary?.upward_migration_pct)}
          accent={COLORS.trend_up}
        />
        <StatCard
          label="Avg VA Width"
          value={fmt(summary?.avg_va_width, 0)}
        />
        <StatCard
          label="Avg Net Failure"
          value={fmt(summary?.avg_net_failure, 2)}
          accent={
            (summary?.avg_net_failure ?? 0) < 0
              ? COLORS.trend_up
              : COLORS.trend_dn
          }
        />
      </div>

      {/* POC + VA band — tight Y domain so moves are visible */}
      <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-3">
        <p className="text-[11px] text-zinc-500 mb-1">
          POC & Value Area — last {sessions.length} sessions
        </p>
        <p className="text-[9px] text-zinc-600 mb-2">
          Y axis zoomed to data range {priceMin.toLocaleString()} – {priceMax.toLocaleString()} (±{Math.round(pricePad)} pad)
        </p>
        <ResponsiveContainer width="100%" height={220}>
          <ComposedChart data={sessions} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#27272a" />
            <XAxis
              dataKey="date"
              tickFormatter={shortDate}
              tick={{ fontSize: 9, fill: "#71717a" }}
              interval="preserveStartEnd"
            />
            <YAxis
              domain={priceDomain}
              tick={{ fontSize: 9, fill: "#71717a" }}
              tickFormatter={(v) => v.toLocaleString()}
              width={56}
            />
            <Tooltip
              contentStyle={{ background: "#18181b", border: "1px solid #3f3f46", fontSize: 11 }}
              formatter={(val: number, name: string) => [
                val.toLocaleString(undefined, { maximumFractionDigits: 0 }),
                name,
              ]}
            />
            <Legend wrapperStyle={{ fontSize: 10, color: "#71717a" }} />
            {/* VAH filled area on top */}
            <Area dataKey="vah" name="VAH" stroke={COLORS.vah} fill={`${COLORS.vah}20`} strokeWidth={1.5} dot={false} legendType="line" />
            {/* VAL filled area underneath — fill between val and chart bottom */}
            <Area dataKey="val" name="VAL" stroke={COLORS.val} fill={`${COLORS.val}20`} strokeWidth={1.5} dot={false} legendType="line" />
            <Line dataKey="poc" name="POC" stroke={COLORS.poc} strokeWidth={2.5} dot={false} />
            <Line dataKey="poc_ma" name={`POC MA`} stroke={`${COLORS.poc}77`} strokeWidth={1} strokeDasharray="5 3" dot={false} />
            <Line dataKey="va_center" name="VA Centre" stroke={COLORS.va_center} strokeWidth={1.5} strokeDasharray="3 2" dot={false} />
          </ComposedChart>
        </ResponsiveContainer>
      </div>

      {/* POC daily shift bars — separated from price scale */}
      <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-3">
        <p className="text-[11px] text-zinc-500 mb-2">
          Session-on-Session POC Shift — direction of value migration
        </p>
        <ResponsiveContainer width="100%" height={130}>
          <ComposedChart data={sessions} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#27272a" />
            <XAxis dataKey="date" tickFormatter={shortDate} tick={{ fontSize: 9, fill: "#71717a" }} interval="preserveStartEnd" />
            <YAxis domain={shiftDomain} tick={{ fontSize: 9, fill: "#71717a" }} width={44} tickFormatter={(v) => (v > 0 ? "+" : "") + v} />
            <Tooltip contentStyle={{ background: "#18181b", border: "1px solid #3f3f46", fontSize: 11 }} formatter={(v: number) => [(v > 0 ? "+" : "") + v.toFixed(0), "POC Shift"]} />
            <ReferenceLine y={0} stroke="#52525b" />
            <Bar dataKey="poc_shift" name="POC Shift" maxBarSize={10}>
              {sessions.map((s: any, i: number) => (
                <Cell key={i} fill={s.poc_shift >= 0 ? COLORS.trend_up : COLORS.trend_dn} />
              ))}
            </Bar>
          </ComposedChart>
        </ResponsiveContainer>
      </div>

      {/* VA Width — own tight axis; Net Failure — own axis on right */}
      <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-3">
        <p className="text-[11px] text-zinc-500 mb-2">
          VA Width <span className="text-blue-400">(left)</span> &amp; Net Failure Score <span className="text-zinc-400">(right, +seller / −buyer)</span>
        </p>
        <ResponsiveContainer width="100%" height={150}>
          <ComposedChart data={sessions} margin={{ top: 4, right: 40, bottom: 0, left: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#27272a" />
            <XAxis dataKey="date" tickFormatter={shortDate} tick={{ fontSize: 9, fill: "#71717a" }} interval="preserveStartEnd" />
            <YAxis yAxisId="width" domain={vaWidthDomain} tick={{ fontSize: 9, fill: "#60a5fa" }} width={40} />
            <YAxis yAxisId="fail" orientation="right" domain={failDomain} tick={{ fontSize: 9, fill: "#71717a" }} width={36} tickFormatter={(v) => (v > 0 ? "+" : "") + v} />
            <Tooltip contentStyle={{ background: "#18181b", border: "1px solid #3f3f46", fontSize: 11 }} />
            <ReferenceLine yAxisId="fail" y={0} stroke="#52525b" strokeDasharray="2 2" />
            <Area yAxisId="width" dataKey="va_width" name="VA Width" stroke="#60a5fa" fill="#60a5fa18" strokeWidth={1.5} dot={false} />
            <Bar yAxisId="fail" dataKey="net_failure" name="Net Fail" maxBarSize={8}>
              {sessions.map((s: any, i: number) => (
                <Cell key={i} fill={s.net_failure < 0 ? COLORS.trend_dn : COLORS.trend_up} />
              ))}
            </Bar>
          </ComposedChart>
        </ResponsiveContainer>
      </div>

      {/* Close location — fixed 0–1 domain is correct; add bull/bear zones */}
      <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-3">
        <p className="text-[11px] text-zinc-500 mb-2">
          Close Location — 0 = session low · 0.5 = midpoint · 1 = session high
        </p>
        <ResponsiveContainer width="100%" height={110}>
          <AreaChart data={sessions} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#27272a" />
            <XAxis dataKey="date" tickFormatter={shortDate} tick={{ fontSize: 9, fill: "#71717a" }} interval="preserveStartEnd" />
            <YAxis domain={[0, 1]} ticks={[0, 0.25, 0.5, 0.75, 1]} tick={{ fontSize: 9, fill: "#71717a" }} width={28}
              tickFormatter={(v) => (v === 0 ? "Low" : v === 0.5 ? "Mid" : v === 1 ? "High" : v.toFixed(2))}
            />
            <Tooltip contentStyle={{ background: "#18181b", border: "1px solid #3f3f46", fontSize: 11 }}
              formatter={(v: number) => [(v * 100).toFixed(1) + "%", "Close Loc."]}
            />
            <ReferenceLine y={0.5} stroke="#52525b" strokeDasharray="3 3" label={{ value: "Midpoint", position: "right", fontSize: 8, fill: "#52525b" }} />
            <ReferenceLine y={0.7} stroke={`${COLORS.trend_up}44`} strokeDasharray="2 2" />
            <ReferenceLine y={0.3} stroke={`${COLORS.trend_dn}44`} strokeDasharray="2 2" />
            <Area dataKey="close_location" name="Close Loc." stroke="#a78bfa" fill="#a78bfa25" strokeWidth={2} dot={false} />
          </AreaChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}

// ─── Regime History Panel ─────────────────────────────────────────────────────

function RegimeHistoryPanel({ data }: { data: any }) {
  if (!data?.sessions?.length)
    return <p className="text-xs text-zinc-500">No regime data available.</p>;

  const sessions: any[] = data.sessions;
  const distribution: any[] = data.distribution ?? [];
  const streaks: any[] = data.streaks ?? [];
  const matrix: Record<string, Record<string, number>> = data.transition_matrix ?? {};

  return (
    <div className="space-y-4">
      {/* Distribution bar */}
      <div className="grid grid-cols-2 gap-4">
        <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-3">
          <p className="text-[11px] text-zinc-500 mb-2">Day-Type Distribution</p>
          <div className="space-y-1">
            {distribution.map((d) => (
              <div key={d.day_type} className="flex items-center gap-2">
                <div
                  className="h-3 rounded"
                  style={{
                    width: `${Math.max(d.pct, 4)}%`,
                    background: DAY_TYPE_COLOR[d.day_type] ?? "#94a3b8",
                    minWidth: 4,
                  }}
                />
                <span className="text-[10px] text-zinc-400 whitespace-nowrap">
                  {d.day_type} <span className="text-zinc-600">{d.pct}%</span>
                </span>
              </div>
            ))}
          </div>
        </div>

        {/* Longest streaks */}
        <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-3">
          <p className="text-[11px] text-zinc-500 mb-2">Notable Streaks</p>
          {streaks.length === 0 ? (
            <p className="text-xs text-zinc-600">No 2+ session streaks.</p>
          ) : (
            <div className="space-y-1">
              {streaks.slice(0, 5).map((s, i) => (
                <div key={i} className="flex items-center gap-2 text-[10px]">
                  <span
                    className="w-2 h-2 rounded-full shrink-0"
                    style={{ background: DAY_TYPE_COLOR[s.day_type] ?? "#94a3b8" }}
                  />
                  <span className="text-zinc-300 font-medium">
                    {s.length}× {s.day_type}
                  </span>
                  <span className="text-zinc-600">
                    {shortDate(s.start_date)}→{shortDate(s.end_date)}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* Timeline strip */}
      <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-3">
        <p className="text-[11px] text-zinc-500 mb-2">Session Timeline (latest right)</p>
        <div className="flex flex-wrap gap-0.5">
          {sessions.map((s, i) => (
            <div
              key={i}
              className="h-5 rounded-sm cursor-default transition-opacity hover:opacity-80"
              style={{
                width: Math.max(100 / sessions.length - 0.3, 2) + "%",
                background: DAY_TYPE_COLOR[s.day_type] ?? "#94a3b8",
              }}
              title={`${s.date} — ${s.day_type}`}
            />
          ))}
        </div>
        <div className="flex items-center gap-3 mt-2 flex-wrap">
          {Object.entries(DAY_TYPE_COLOR).map(([k, c]) => (
            <div key={k} className="flex items-center gap-1">
              <div className="w-2 h-2 rounded-sm" style={{ background: c }} />
              <span className="text-[9px] text-zinc-500">{k.replace("_", " ")}</span>
            </div>
          ))}
        </div>
      </div>

      {/* Transition matrix */}
      {Object.keys(matrix).length > 0 && (
        <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-3 overflow-x-auto">
          <p className="text-[11px] text-zinc-500 mb-2">Transition Probability Matrix</p>
          <table className="text-[10px] border-collapse w-full">
            <thead>
              <tr>
                <th className="text-left text-zinc-600 pr-3 pb-1">From ↓ / To →</th>
                {Object.keys(matrix).map((k) => (
                  <th key={k} className="text-center text-zinc-500 px-1 pb-1 whitespace-nowrap">
                    {k.replace("_", " ")}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {Object.entries(matrix).map(([from, toMap]) => (
                <tr key={from} className="border-t border-zinc-800">
                  <td
                    className="pr-3 py-0.5 font-medium whitespace-nowrap"
                    style={{ color: DAY_TYPE_COLOR[from] ?? "#94a3b8" }}
                  >
                    {from}
                  </td>
                  {Object.keys(matrix).map((to) => {
                    const prob = toMap[to] ?? 0;
                    return (
                      <td
                        key={to}
                        className="text-center px-1 py-0.5 font-mono"
                        style={{
                          color: prob >= 0.4 ? "#f59e0b" : prob >= 0.2 ? "#e4e4e7" : "#52525b",
                          fontWeight: prob >= 0.4 ? 700 : 400,
                        }}
                      >
                        {prob > 0 ? (prob * 100).toFixed(0) + "%" : "—"}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ─── Setup Performance Panel ──────────────────────────────────────────────────

function SetupPerformancePanel({ data }: { data: any }) {
  if (!data?.cells?.length)
    return <p className="text-xs text-zinc-500">No setup performance data (need forward outcomes).</p>;

  const cells: any[] = data.cells ?? [];
  const calibration: any[] = data.calibration ?? [];
  const dayTypeSummary: any[] = data.day_type_summary ?? [];

  function wrColor(wr: number) {
    if (wr >= 60) return COLORS.trend_up;
    if (wr >= 50) return "#86efac";
    if (wr >= 40) return "#fca5a5";
    return COLORS.trend_dn;
  }

  // ── Tight domain for day-type move bar chart ──────────────────────────────
  // Use 5th–95th percentile so a single extreme day-type doesn't squash the rest
  const allMoves = dayTypeSummary.map((d: any) => d.avg_next_day_move).filter(Boolean).sort((a: number, b: number) => a - b);
  const moveAbs = allMoves.length ? Math.max(...allMoves.map(Math.abs)) : 50;
  // Compute a "view max" that clips at 1.5× median abs, labelling true value
  const medianAbs = allMoves.length ? allMoves[Math.floor(allMoves.length / 2)] : 50;
  const viewMax = Math.min(moveAbs, Math.max(Math.abs(medianAbs) * 3, 30));
  const moveDomain = [-Math.ceil(viewMax + 5), Math.ceil(viewMax + 5)];

  return (
    <div className="space-y-4">
      {/* Calibration strip */}
      <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-3">
        <p className="text-[11px] text-zinc-500 mb-2">
          Conviction Calibration — signal strength vs realised win rate
        </p>
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
          {calibration.map((c) => (
            <div
              key={c.strength}
              className="rounded p-2 text-center"
              style={{ background: `${wrColor(c.avg_win_rate_1d)}18`, borderColor: `${wrColor(c.avg_win_rate_1d)}44` }}
            >
              <p className="text-[9px] text-zinc-500 mb-0.5 uppercase tracking-wide">
                {c.strength}
              </p>
              <p
                className="text-lg font-bold font-mono"
                style={{ color: wrColor(c.avg_win_rate_1d) }}
              >
                {pct(c.avg_win_rate_1d)}
              </p>
              <p className="text-[9px] text-zinc-600">
                3d: {pct(c.avg_win_rate_3d)} · n={c.total_signals}
              </p>
            </div>
          ))}
        </div>
      </div>

      {/* Day-type summary bars — clipped domain so small moves are readable */}
      <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-3">
        <p className="text-[11px] text-zinc-500 mb-1">
          Avg Next-Day Move by Day-Type (points)
        </p>
        <p className="text-[9px] text-zinc-600 mb-2">
          Y axis capped at ±{Math.ceil(viewMax + 5)} — true values shown in tooltip
        </p>
        <ResponsiveContainer width="100%" height={170}>
          <BarChart
            data={dayTypeSummary}
            margin={{ top: 4, right: 8, bottom: 28, left: 0 }}
          >
            <CartesianGrid strokeDasharray="3 3" stroke="#27272a" />
            <XAxis
              dataKey="day_type"
              tick={{ fontSize: 8, fill: "#71717a" }}
              angle={-30}
              textAnchor="end"
            />
            <YAxis domain={moveDomain} tick={{ fontSize: 9, fill: "#71717a" }} width={44}
              tickFormatter={(v) => (v > 0 ? "+" : "") + v}
            />
            <Tooltip
              contentStyle={{ background: "#18181b", border: "1px solid #3f3f46", fontSize: 11 }}
              formatter={(v: number, name: string) => [
                (v > 0 ? "+" : "") + v.toFixed(1) + " pts · std " + (dayTypeSummary.find((d: any) => d.avg_next_day_move === v)?.std_next_day_move?.toFixed(1) ?? "—"),
                name,
              ]}
            />
            <ReferenceLine y={0} stroke="#52525b" />
            <Bar dataKey="avg_next_day_move" name="Avg Next-Day" maxBarSize={36}
              label={{ position: "top", fontSize: 8, fill: "#71717a", formatter: (v: number) => v > viewMax || v < -viewMax ? ">" + Math.round(v) : "" }}
            >
              {dayTypeSummary.map((d: any, i: number) => (
                <Cell
                  key={i}
                  fill={
                    d.avg_next_day_move > 15
                      ? COLORS.trend_up
                      : d.avg_next_day_move < -15
                        ? COLORS.trend_dn
                        : "#71717a"
                  }
                />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>

      {/* Performance cells table */}
      <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-3 overflow-x-auto">
        <p className="text-[11px] text-zinc-500 mb-2">
          Setup Matrix — (Day Type × Direction × Strength)
        </p>
        <table className="text-[10px] w-full border-collapse">
          <thead>
            <tr className="border-b border-zinc-800">
              {["Day Type", "Dir", "Strength", "N", "WR 1d", "WR 3d", "Avg Move", "Expectancy", "Sharpe"].map(
                (h) => (
                  <th key={h} className="text-left text-zinc-500 px-2 py-1 whitespace-nowrap">
                    {h}
                  </th>
                ),
              )}
            </tr>
          </thead>
          <tbody>
            {[...cells]
              .sort((a, b) => b.win_rate_1d - a.win_rate_1d)
              .map((c, i) => (
                <tr
                  key={i}
                  className="border-t border-zinc-800/60 hover:bg-zinc-800/30"
                >
                  <td
                    className="px-2 py-0.5 font-medium whitespace-nowrap"
                    style={{ color: DAY_TYPE_COLOR[c.day_type] ?? "#94a3b8" }}
                  >
                    {c.day_type}
                  </td>
                  <td className="px-2 py-0.5">
                    <Pill
                      label={c.direction}
                      color={
                        c.direction === "CE"
                          ? COLORS.trend_up
                          : c.direction === "PE"
                            ? COLORS.trend_dn
                            : "#94a3b8"
                      }
                      small
                    />
                  </td>
                  <td className="px-2 py-0.5 text-zinc-400 capitalize">{c.strength}</td>
                  <td className="px-2 py-0.5 text-zinc-300 font-mono">{c.count}</td>
                  <td
                    className="px-2 py-0.5 font-mono font-semibold"
                    style={{ color: wrColor(c.win_rate_1d) }}
                  >
                    {pct(c.win_rate_1d)}
                  </td>
                  <td
                    className="px-2 py-0.5 font-mono"
                    style={{ color: wrColor(c.win_rate_3d) }}
                  >
                    {pct(c.win_rate_3d)}
                  </td>
                  <td
                    className="px-2 py-0.5 font-mono"
                    style={{
                      color:
                        c.avg_next_day_move > 0 ? COLORS.trend_up : COLORS.trend_dn,
                    }}
                  >
                    {fmt(c.avg_next_day_move, 0)}
                  </td>
                  <td
                    className="px-2 py-0.5 font-mono"
                    style={{
                      color: c.expectancy_1d > 0 ? COLORS.trend_up : COLORS.trend_dn,
                    }}
                  >
                    {fmt(c.expectancy_1d, 0)}
                  </td>
                  <td
                    className="px-2 py-0.5 font-mono text-zinc-400"
                  >
                    {fmt(c.sharpe_proxy, 2)}
                  </td>
                </tr>
              ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ─── CVD / Orderflow Proxy Panel ──────────────────────────────────────────────

function OrderflowPanel({ data }: { data: any }) {
  if (!data?.series?.length)
    return <p className="text-xs text-zinc-500">No orderflow data available.</p>;

  const series: any[] = data.series;
  const divergences: any[] = data.divergences ?? [];
  const summary = data.summary ?? {};

  // ── Tight domains for CVD charts ─────────────────────────────────────────
  const cvdVals = series.map((s: any) => s.cvd).filter(Boolean);
  const cvdMin = Math.min(...cvdVals);
  const cvdMax = Math.max(...cvdVals);
  const cvdPad = Math.max((cvdMax - cvdMin) * 0.1, 0.5);
  const cvdDomain = [cvdMin - cvdPad, cvdMax + cvdPad];

  const closePrices = series.map((s: any) => s.close).filter(Boolean);
  const closeMin = Math.min(...closePrices);
  const closeMax = Math.max(...closePrices);
  const closePad = (closeMax - closeMin) * 0.05;
  const closeDomain = [Math.floor(closeMin - closePad), Math.ceil(closeMax + closePad)];

  const deltaVals = series.map((s: any) => s.daily_delta).filter((v: any) => v != null);
  const deltaAbs = Math.max(...deltaVals.map(Math.abs), 0.5);
  const deltaDomain = [-(deltaAbs + 0.1), deltaAbs + 0.1];

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
        <StatCard
          label="Current CVD"
          value={fmt(summary.net_cvd, 2)}
          accent={(summary.net_cvd ?? 0) >= 0 ? COLORS.trend_up : COLORS.trend_dn}
        />
        <StatCard label="Bull Days" value={summary.total_bull_days ?? "—"} accent={COLORS.trend_up} />
        <StatCard label="Bear Days" value={summary.total_bear_days ?? "—"} accent={COLORS.trend_dn} />
        <StatCard
          label="Divergences"
          value={summary.divergences_count ?? 0}
          accent={COLORS.drift_alert}
        />
      </div>

      {/* Accumulated CVD vs Close price — separate Y axes, both zoomed */}
      <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-3">
        <p className="text-[11px] text-zinc-500 mb-1">
          Cumulative Volume Delta <span className="text-purple-400">(left)</span> vs Close <span className="text-blue-400">(right)</span>
        </p>
        <p className="text-[9px] text-zinc-600 mb-2">
          CVD axis: {cvdMin.toFixed(1)} – {cvdMax.toFixed(1)} · Price axis: {closeMin.toLocaleString()} – {closeMax.toLocaleString()}
        </p>
        <ResponsiveContainer width="100%" height={200}>
          <ComposedChart data={series} margin={{ top: 4, right: 56, bottom: 0, left: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#27272a" />
            <XAxis dataKey="date" tickFormatter={shortDate} tick={{ fontSize: 9, fill: "#71717a" }} interval="preserveStartEnd" />
            <YAxis yAxisId="cvd" domain={cvdDomain} tick={{ fontSize: 9, fill: "#a78bfa" }} width={44} tickFormatter={(v) => v.toFixed(1)} />
            <YAxis yAxisId="close" orientation="right" domain={closeDomain} tick={{ fontSize: 9, fill: "#60a5fa" }} width={52} tickFormatter={(v) => v.toLocaleString()} />
            <Tooltip
              contentStyle={{ background: "#18181b", border: "1px solid #3f3f46", fontSize: 11 }}
              formatter={(v: number, name: string) => [
                name === "Close" ? v.toLocaleString() : v.toFixed(3), name,
              ]}
            />
            <Legend wrapperStyle={{ fontSize: 10, color: "#71717a" }} />
            <ReferenceLine yAxisId="cvd" y={0} stroke="#52525b" strokeDasharray="2 2" />
            <Area yAxisId="cvd" dataKey="cvd" name="CVD (accum.)" stroke="#a78bfa" fill="#a78bfa20" strokeWidth={2} dot={false} />
            <Line yAxisId="close" dataKey="close" name="Close" stroke="#60a5fa" strokeWidth={1.5} dot={false} opacity={0.7} />
          </ComposedChart>
        </ResponsiveContainer>
      </div>

      {/* Daily delta bars — own axis so ±0.5 bars are clearly visible */}
      <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-3">
        <p className="text-[11px] text-zinc-500 mb-2">
          Daily Auction Delta — signed session pressure (green = bull, red = bear)
        </p>
        <ResponsiveContainer width="100%" height={130}>
          <ComposedChart data={series} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#27272a" />
            <XAxis dataKey="date" tickFormatter={shortDate} tick={{ fontSize: 9, fill: "#71717a" }} interval="preserveStartEnd" />
            <YAxis domain={deltaDomain} tick={{ fontSize: 9, fill: "#71717a" }} width={40} tickFormatter={(v) => v.toFixed(2)} />
            <Tooltip contentStyle={{ background: "#18181b", border: "1px solid #3f3f46", fontSize: 11 }}
              formatter={(v: number) => [v.toFixed(3), "Daily Δ"]}
            />
            <ReferenceLine y={0} stroke="#52525b" />
            <Bar dataKey="daily_delta" name="Daily Δ" maxBarSize={12}>
              {series.map((s: any, i: number) => (
                <Cell key={i} fill={s.daily_delta >= 0 ? COLORS.cvd_bull : COLORS.cvd_bear} />
              ))}
            </Bar>
          </ComposedChart>
        </ResponsiveContainer>
      </div>

      {/* Divergences table */}
      {divergences.length > 0 && (
        <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-3">
          <p className="text-[11px] text-zinc-500 mb-2">CVD Divergences</p>
          <div className="space-y-1">
            {divergences.map((d, i) => (
              <div
                key={i}
                className="flex items-center gap-3 text-[11px] border-b border-zinc-800/60 pb-1"
              >
                <span className="text-zinc-400 font-mono">{d.date}</span>
                <Pill
                  label={d.type === "bullish_divergence" ? "↑ Bullish" : "↓ Bearish"}
                  color={d.type === "bullish_divergence" ? COLORS.trend_up : COLORS.trend_dn}
                  small
                />
                <span className="text-zinc-500">
                  Price {d.price_change > 0 ? "+" : ""}
                  {fmt(d.price_change, 0)} · CVD {d.cvd_change > 0 ? "+" : ""}
                  {fmt(d.cvd_change, 3)}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

// ─── Concept Drift Panel ──────────────────────────────────────────────────────

function ConceptDriftPanel({ data }: { data: any }) {
  if (!data?.series?.length)
    return (
      <p className="text-xs text-zinc-500">
        {data?.current_state === "insufficient_data"
          ? "Need more sessions with directional signals for drift analysis."
          : "No drift data available."}
      </p>
    );

  const series: any[] = data.series;
  const driftEvents: any[] = data.drift_events ?? [];
  const state: string = data.current_state ?? "stable";
  const driftColor =
    state === "drift"
      ? COLORS.drift_alert
      : state === "recovering"
        ? COLORS.failed_auction
        : COLORS.stable;

  // Tight Y domain for rolling win rate — zoom to actual variation, not 0–100
  const wrValues = series.map((s: any) => s.rolling_win_rate).filter(Boolean);
  const wrMin = Math.min(...wrValues);
  const wrMax = Math.max(...wrValues);
  const wrPad = Math.max((wrMax - wrMin) * 0.15, 4);
  // Always include 50 so reference line is visible
  const wrDomainLow = Math.floor(Math.min(wrMin - wrPad, 45));
  const wrDomainHigh = Math.ceil(Math.max(wrMax + wrPad, 55));

  const phValues = series.map((s: any) => s.ph_stat).filter((v: any) => v != null);
  const phMax = Math.max(...phValues, data.ph_threshold ?? 8);
  const phDomain = [0, Math.ceil(phMax * 1.15)];

  return (
    <div className="space-y-4">
      {/* Status banner */}
      <div
        className="rounded-lg p-3 flex items-center gap-3"
        style={{ background: `${driftColor}18`, border: `1px solid ${driftColor}44` }}
      >
        {state === "drift" ? (
          <AlertTriangle className="w-5 h-5 shrink-0" style={{ color: driftColor }} />
        ) : (
          <CheckCircle2 className="w-5 h-5 shrink-0" style={{ color: driftColor }} />
        )}
        <div>
          <p className="text-sm font-semibold" style={{ color: driftColor }}>
            {state === "drift"
              ? "Concept Drift Detected"
              : state === "recovering"
                ? "Signal Performance Below Mean"
                : "Signal Performance Stable"}
          </p>
          <p className="text-xs text-zinc-400 mt-0.5">
            Rolling win rate:{" "}
            <span className="font-mono font-semibold">
              {pct(data.current_rolling_win_rate)}
            </span>
            {" "}vs historical mean{" "}
            <span className="font-mono">{pct(data.historical_mean_win_rate)}</span>
            {" · "}deviation{" "}
            <span
              className="font-mono font-semibold"
              style={{
                color:
                  (data.drift_magnitude ?? 0) < -3
                    ? COLORS.trend_dn
                    : COLORS.trend_up,
              }}
            >
              {(data.drift_magnitude ?? 0) > 0 ? "+" : ""}
              {fmt(data.drift_magnitude, 1)}pp
            </span>
          </p>
        </div>
        {driftEvents.length > 0 && (
          <div className="ml-auto text-right">
            <p className="text-[10px] text-zinc-500">{driftEvents.length} drift events</p>
            <p className="text-[10px] text-zinc-600">
              Latest: {driftEvents.at(-1)?.date ?? "—"}
            </p>
          </div>
        )}
      </div>

      {/* Rolling win rate — zoomed domain so 45–55% swings are visible */}
      <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-3">
        <p className="text-[11px] text-zinc-500 mb-1">
          Rolling Win Rate <span className="text-blue-400">(left)</span>
          — zoomed to {wrDomainLow}%–{wrDomainHigh}% (not 0–100)
        </p>
        <ResponsiveContainer width="100%" height={160}>
          <AreaChart data={series} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#27272a" />
            <XAxis dataKey="date" tickFormatter={shortDate} tick={{ fontSize: 9, fill: "#71717a" }} interval="preserveStartEnd" />
            <YAxis
              domain={[wrDomainLow, wrDomainHigh]}
              tick={{ fontSize: 9, fill: "#71717a" }}
              width={34}
              tickFormatter={(v) => v + "%"}
            />
            <Tooltip
              contentStyle={{ background: "#18181b", border: "1px solid #3f3f46", fontSize: 11 }}
              formatter={(v: number) => [v.toFixed(1) + "%", "Rolling WR"]}
            />
            <ReferenceLine y={50} stroke="#52525b" strokeDasharray="3 3"
              label={{ value: "50%", position: "right", fontSize: 8, fill: "#71717a" }}
            />
            <ReferenceLine y={data.historical_mean_win_rate} stroke="#a78bfa88" strokeDasharray="3 3"
              label={{ value: "Mean", position: "right", fontSize: 8, fill: "#a78bfa" }}
            />
            {/* Colour fill: red below 50, green above */}
            <Area dataKey="rolling_win_rate" name="Rolling WR" stroke="#60a5fa" fill="#60a5fa18" strokeWidth={2.5} dot={false} />
            {driftEvents.map((ev, i) => (
              <ReferenceLine key={i} x={ev.date} stroke={`${COLORS.drift_alert}99`} strokeDasharray="2 2" />
            ))}
          </AreaChart>
        </ResponsiveContainer>
      </div>

      {/* PH Statistic — own chart so it isn't compressed against win-rate scale */}
      <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-3">
        <p className="text-[11px] text-zinc-500 mb-1">
          Page-Hinkley Statistic — rises when win rate persistently falls below mean
        </p>
        <ResponsiveContainer width="100%" height={130}>
          <ComposedChart data={series} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#27272a" />
            <XAxis dataKey="date" tickFormatter={shortDate} tick={{ fontSize: 9, fill: "#71717a" }} interval="preserveStartEnd" />
            <YAxis domain={phDomain} tick={{ fontSize: 9, fill: "#71717a" }} width={34} />
            <Tooltip contentStyle={{ background: "#18181b", border: "1px solid #3f3f46", fontSize: 11 }}
              formatter={(v: number) => [v.toFixed(1), "PH Stat"]}
            />
            <ReferenceLine y={data.ph_threshold ?? 8} stroke={COLORS.drift_alert}
              strokeDasharray="3 3"
              label={{ value: `Drift (${data.ph_threshold ?? 8})`, position: "right", fontSize: 8, fill: COLORS.drift_alert }}
            />
            <Area dataKey="ph_stat" name="PH Stat" stroke={COLORS.drift_alert} fill={`${COLORS.drift_alert}18`} strokeWidth={2} dot={false} />
            {driftEvents.map((ev, i) => (
              <ReferenceLine key={i} x={ev.date} stroke={`${COLORS.drift_alert}66`} strokeDasharray="2 2" />
            ))}
          </ComposedChart>
        </ResponsiveContainer>
      </div>

      <p className="text-[10px] text-zinc-600 leading-relaxed">
        The Page-Hinkley statistic accumulates when each session's win rate falls
        below the historical mean. When it crosses {data.ph_threshold ?? 8}, the
        signal → outcome relationship may have shifted — consider reducing position
        size or re-examining the day-type logic for recent sessions.
      </p>
    </div>
  );
}

// ─── Composite Profile Panel ──────────────────────────────────────────────────

function CompositeProfilePanel({ profiles, weeklyProfiles }: { profiles: any; weeklyProfiles: any[] }) {
  if (!profiles || Object.keys(profiles).length === 0)
    return <p className="text-xs text-zinc-500">No composite profile data.</p>;

  const p20 = profiles["composite_20d"];
  const p60 = profiles["composite_60d"];

  function ProfileCard({ p, label }: { p: any; label: string }) {
    if (!p) return null;
    const tpoRows: any[] = p.tpo_rows ?? [];
    const maxCount = Math.max(...tpoRows.map((r: any) => r.count), 1);

    return (
      <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-3">
        <p className="text-[11px] text-zinc-500 mb-1">{label}</p>
        <div className="grid grid-cols-3 gap-2 mb-2">
          <div>
            <p className="text-[9px] text-zinc-600">POC</p>
            <p className="text-sm font-mono font-semibold text-amber-400">
              {p.poc?.toLocaleString()}
            </p>
          </div>
          <div>
            <p className="text-[9px] text-zinc-600">VAH</p>
            <p className="text-sm font-mono font-semibold text-emerald-400">
              {p.vah?.toLocaleString()}
            </p>
          </div>
          <div>
            <p className="text-[9px] text-zinc-600">VAL</p>
            <p className="text-sm font-mono font-semibold text-blue-400">
              {p.val?.toLocaleString()}
            </p>
          </div>
        </div>
        <div className="grid grid-cols-3 gap-2 mb-2 text-[10px]">
          <span className="text-zinc-500">
            High: <span className="text-zinc-300">{p.high_price?.toLocaleString()}</span>
          </span>
          <span className="text-zinc-500">
            VA Width: <span className="text-zinc-300">{p.va_width?.toFixed(0)}</span>
          </span>
          <span className="text-zinc-500">
            Sessions: <span className="text-zinc-300">{p.lookback_sessions}</span>
          </span>
        </div>
        {/* Mini TPO visualisation */}
        <div className="mt-1 max-h-48 overflow-y-auto">
          {[...tpoRows]
            .sort((a: any, b: any) => b.price - a.price)
            .map((row: any, i: number) => {
              const isPOC = Math.abs(row.price - p.poc) < (p.tick_size ?? 10);
              const isVAH = Math.abs(row.price - p.vah) < (p.tick_size ?? 10);
              const isVAL = Math.abs(row.price - p.val) < (p.tick_size ?? 10);
              const barWidth = Math.max((row.count / maxCount) * 100, 2);
              return (
                <div key={i} className="flex items-center gap-1 h-[5px] mb-px">
                  <span
                    className="text-[7px] text-zinc-600 w-10 text-right shrink-0"
                    style={{ color: isPOC ? "#f59e0b" : isVAH ? "#34d399" : isVAL ? "#60a5fa" : "#71717a" }}
                  >
                    {row.price.toLocaleString()}
                  </span>
                  <div
                    className="h-full rounded-sm"
                    style={{
                      width: `${barWidth}%`,
                      background: isPOC
                        ? "#f59e0b"
                        : isVAH || isVAL
                          ? "#60a5fa"
                          : "#3f3f46",
                    }}
                  />
                </div>
              );
            })}
        </div>
        <p className="text-[9px] text-zinc-600 mt-1">
          {p.session_start} → {p.session_end}
        </p>
      </div>
    );
  }

  // Weekly profiles bar chart — compute tight domain from actual data
  const weeklyData = weeklyProfiles?.map((w: any) => ({
    week: w.week,
    poc: w.poc,
    vah: w.vah,
    val: w.val,
    sessions: w.sessions,
    range: w.high_price - w.low_price,
  }));
  const weeklyPrices = weeklyData?.flatMap((w: any) => [w.vah, w.val, w.poc]).filter(Boolean) ?? [];
  const weeklyMin = weeklyPrices.length ? Math.min(...weeklyPrices) : 0;
  const weeklyMax = weeklyPrices.length ? Math.max(...weeklyPrices) : 100;
  const weeklyPad = Math.max((weeklyMax - weeklyMin) * 0.06, 100);
  const weeklyDomain = [Math.floor(weeklyMin - weeklyPad), Math.ceil(weeklyMax + weeklyPad)];

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <ProfileCard p={p20} label="Composite 20D" />
        <ProfileCard p={p60} label="Composite 60D" />
      </div>

      {weeklyData?.length > 0 && (
        <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-3">
          <p className="text-[11px] text-zinc-500 mb-2">Weekly POC / VA Levels</p>
          <ResponsiveContainer width="100%" height={180}>
            <ComposedChart data={weeklyData} margin={{ top: 4, right: 8, bottom: 16, left: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#27272a" />
              <XAxis
                dataKey="week"
                tick={{ fontSize: 8, fill: "#71717a" }}
                angle={-30}
                textAnchor="end"
              />
              <YAxis
                domain={weeklyDomain}
                tick={{ fontSize: 9, fill: "#71717a" }}
                width={56}
                tickFormatter={(v) => v.toLocaleString()}
              />
              <Tooltip
                contentStyle={{ background: "#18181b", border: "1px solid #3f3f46", fontSize: 11 }}
                formatter={(v: number, name: string) => [v.toLocaleString(), name]}
              />
              <Area dataKey="vah" name="VAH" stroke={COLORS.vah} fill={`${COLORS.vah}18`} strokeWidth={1} dot={false} />
              <Area dataKey="val" name="VAL" stroke={COLORS.val} fill={`${COLORS.val}18`} strokeWidth={1} dot={false} />
              <Line dataKey="poc" name="POC" stroke={COLORS.poc} strokeWidth={2} dot={{ r: 2, fill: COLORS.poc }} />
            </ComposedChart>
          </ResponsiveContainer>
        </div>
      )}
    </div>
  );
}

// ─── Main Dashboard ───────────────────────────────────────────────────────────

type Tab = "profiles" | "migration" | "regime" | "performance" | "cvd" | "drift";

const TABS: { id: Tab; label: string; icon: React.ElementType }[] = [
  { id: "profiles", label: "Multi-TF Profiles", icon: Layers3 },
  { id: "migration", label: "Value Migration", icon: TrendingUp },
  { id: "regime", label: "Regime History", icon: Activity },
  { id: "performance", label: "Setup Performance", icon: BarChart2 },
  { id: "cvd", label: "Orderflow Proxy", icon: Zap },
  { id: "drift", label: "Concept Drift", icon: Brain },
];

export default function MPIntelligenceDashboard() {
  const [underlying, setUnderlying] = useState("NIFTY");
  const [lookback, setLookback] = useState(60);
  const [activeTab, setActiveTab] = useState<Tab>("migration");

  const { data, isLoading, isError, refetch, isFetching } = useQuery({
    queryKey: ["mp-analytics", underlying, lookback],
    queryFn: () =>
      getMPAnalytics(underlying, lookback).then((r) => r.data),
    staleTime: 5 * 60 * 1000,
    refetchInterval: 10 * 60 * 1000,
  });

  const driftState = data?.concept_drift?.current_state;
  const driftBadge =
    driftState === "drift"
      ? { label: "DRIFT", color: COLORS.drift_alert }
      : driftState === "recovering"
        ? { label: "RECOVERING", color: COLORS.failed_auction }
        : driftState === "stable"
          ? { label: "STABLE", color: COLORS.stable }
          : null;

  return (
    <div className="min-h-screen bg-zinc-950 text-zinc-100 p-4 space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-lg font-bold text-zinc-100 flex items-center gap-2">
            <Brain className="w-5 h-5 text-purple-400" />
            MP Intelligence
          </h1>
          <p className="text-xs text-zinc-500 mt-0.5">
            Multi-timeframe profiles · Regime history · Setup performance · Concept drift
          </p>
        </div>

        <div className="flex items-center gap-2 flex-wrap">
          {/* Underlying selector */}
          <div className="flex items-center gap-1">
            {UNDERLYINGS.map((u) => (
              <button
                key={u}
                onClick={() => setUnderlying(u)}
                className={clsx(
                  "px-3 py-1 rounded text-xs font-medium transition-colors",
                  underlying === u
                    ? "bg-blue-600 text-white"
                    : "bg-zinc-800 text-zinc-400 hover:bg-zinc-700",
                )}
              >
                {u}
              </button>
            ))}
          </div>

          {/* Lookback selector */}
          <select
            value={lookback}
            onChange={(e) => setLookback(Number(e.target.value))}
            className="bg-zinc-800 border border-zinc-700 text-zinc-300 text-xs px-2 py-1 rounded"
          >
            {[30, 45, 60, 90, 120, 180].map((l) => (
              <option key={l} value={l}>
                {l}d
              </option>
            ))}
          </select>

          {/* Drift badge */}
          {driftBadge && (
            <span
              className="px-2 py-0.5 rounded text-[10px] font-bold"
              style={{
                background: `${driftBadge.color}22`,
                color: driftBadge.color,
                border: `1px solid ${driftBadge.color}44`,
              }}
            >
              {driftBadge.label}
            </span>
          )}

          <button
            onClick={() => refetch()}
            disabled={isFetching}
            className="p-1.5 rounded bg-zinc-800 hover:bg-zinc-700 text-zinc-400 hover:text-zinc-200 transition-colors disabled:opacity-50"
          >
            <RefreshCw className={clsx("w-3.5 h-3.5", isFetching && "animate-spin")} />
          </button>
        </div>
      </div>

      {/* Error state */}
      {isError && (
        <div className="bg-red-950/30 border border-red-800 rounded-lg p-4 text-sm text-red-400">
          Failed to load MP analytics. Ensure the backend is running and data exists for {underlying}.
        </div>
      )}

      {/* Loading skeleton */}
      {isLoading && (
        <div className="space-y-3">
          {[200, 160, 160].map((h, i) => (
            <div
              key={i}
              className="bg-zinc-900 border border-zinc-800 rounded-lg animate-pulse"
              style={{ height: h }}
            />
          ))}
        </div>
      )}

      {/* Top stat strip */}
      {data && (
        <div className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-6 gap-2">
          <StatCard
            label="Sessions Analysed"
            value={data.total_sessions ?? "—"}
          />
          <StatCard
            label="Overall 1d Win Rate"
            value={pct(data.setup_performance?.overall_next_day_win_rate)}
            accent={
              (data.setup_performance?.overall_next_day_win_rate ?? 50) >= 50
                ? COLORS.trend_up
                : COLORS.trend_dn
            }
          />
          <StatCard
            label="Cum POC Shift"
            value={fmt(
              data.value_migration?.summary?.cumulative_poc_shift,
              0,
            )}
            accent={
              (data.value_migration?.summary?.cumulative_poc_shift ?? 0) >= 0
                ? COLORS.trend_up
                : COLORS.trend_dn
            }
          />
          <StatCard
            label="Upward Migration"
            value={pct(data.value_migration?.summary?.upward_migration_pct)}
          />
          <StatCard
            label="CVD Divergences"
            value={data.orderflow_proxy?.summary?.divergences_count ?? 0}
            accent={COLORS.drift_alert}
          />
          <StatCard
            label="Signal State"
            value={driftBadge?.label ?? "—"}
            accent={driftBadge?.color}
          />
        </div>
      )}

      {/* Tabs */}
      {data && (
        <div className="space-y-4">
          <div className="flex gap-1 flex-wrap border-b border-zinc-800 pb-0">
            {TABS.map(({ id, label, icon: Icon }) => (
              <button
                key={id}
                onClick={() => setActiveTab(id)}
                className={clsx(
                  "flex items-center gap-1.5 px-3 py-2 text-xs font-medium rounded-t transition-colors",
                  activeTab === id
                    ? "bg-zinc-800 text-zinc-100 border-b-2 border-blue-500"
                    : "text-zinc-500 hover:text-zinc-300 hover:bg-zinc-800/50",
                )}
              >
                <Icon className="w-3.5 h-3.5" />
                {label}
                {id === "drift" && driftBadge?.label === "DRIFT" && (
                  <span className="w-1.5 h-1.5 rounded-full bg-orange-400 ml-0.5" />
                )}
              </button>
            ))}
          </div>

          <div className="pt-1">
            {activeTab === "profiles" && (
              <section>
                <SectionHeader
                  icon={Layers3}
                  title="Multi-Timeframe Profile Stack"
                  sub="Composite 20D / 60D + weekly aggregates"
                />
                <CompositeProfilePanel
                  profiles={data.profiles}
                  weeklyProfiles={data.weekly_profiles}
                />
              </section>
            )}

            {activeTab === "migration" && (
              <section>
                <SectionHeader
                  icon={TrendingUp}
                  title="Value Migration Trend"
                  sub="POC drift, VA centre, VA width, close location"
                />
                <ValueMigrationPanel data={data.value_migration} />
              </section>
            )}

            {activeTab === "regime" && (
              <section>
                <SectionHeader
                  icon={Activity}
                  title="Regime History"
                  sub="Day-type sequence, transition matrix, streaks"
                />
                <RegimeHistoryPanel data={data.regime_history} />
              </section>
            )}

            {activeTab === "performance" && (
              <section>
                <SectionHeader
                  icon={BarChart2}
                  title="Setup Performance Matrix"
                  sub="Empirical win rates & expectancy from historical signals"
                />
                <SetupPerformancePanel data={data.setup_performance} />
              </section>
            )}

            {activeTab === "cvd" && (
              <section>
                <SectionHeader
                  icon={Zap}
                  title="Orderflow Proxy"
                  sub="CVD approximation from daily auction structure — NSE MBO not available"
                />
                <OrderflowPanel data={data.orderflow_proxy} />
              </section>
            )}

            {activeTab === "drift" && (
              <section>
                <SectionHeader
                  icon={Brain}
                  title="Concept Drift Detection"
                  sub="Page-Hinkley test on rolling signal win rate"
                />
                <ConceptDriftPanel data={data.concept_drift} />
              </section>
            )}
          </div>
        </div>
      )}

      <p className="text-[10px] text-zinc-700 text-center pt-2">
        NSE F&O · MP Intelligence · Nomad Curie · {underlying} · Last {lookback} sessions
      </p>
    </div>
  );
}

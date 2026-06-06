"use client";

/**
 * Value-migration chart — POC / VAH / VAL plotted across the session's
 * completed hourly profiles, with the close path overlaid. The shaded
 * band between VAH and VAL is the value area; watching the band drift
 * up/down (and the close lead or lag it) is the core fractal read on
 * where value is migrating through the day.
 */
import {
  Area,
  CartesianGrid,
  ComposedChart,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { formatNumber } from "@/components/desk-ui";
import { CHART } from "@/components/strategies/shared";
import type { HourlyProfile } from "./HourlyStrip";

const AXIS = { stroke: CHART.axis, fontSize: 10, tickLine: false } as const;

type Row = {
  hour: string;
  poc: number | null;
  vah: number | null;
  val: number | null;
  close: number | null;
  shape: string;
  bias: string;
};

function TipBox({ rows }: { rows: Array<{ k: string; v: string; c?: string }> }) {
  return (
    <div className="rounded-lg border px-3 py-2 text-[11px] shadow-lg" style={{ background: CHART.surface, borderColor: CHART.border }}>
      {rows.map((r) => (
        <div key={r.k} className="flex justify-between gap-4">
          <span className="text-text-muted">{r.k}</span>
          <span className="font-mono" style={{ color: r.c ?? "#e6edf3" }}>{r.v}</span>
        </div>
      ))}
    </div>
  );
}

export function ValueMigrationChart({ profiles, height = 280 }: { profiles: HourlyProfile[]; height?: number }) {
  const data: Row[] = profiles
    .filter((p) => Number.isFinite(Number(p.poc)))
    .map((p) => ({
      hour: `H${p.hour_number}`,
      poc: Number(p.poc),
      vah: Number(p.vah),
      val: Number(p.val),
      close: Number(p.close_price),
      shape: String(p.shape ?? ""),
      bias: String(p.direction_bias ?? ""),
    }));

  if (data.length < 2) {
    return (
      <div className="flex items-center justify-center rounded-xl border border-dashed border-bg-border/60 text-sm text-text-muted" style={{ height }}>
        Need at least two completed hours to chart value migration.
      </div>
    );
  }

  const all = data.flatMap((d) => [d.poc, d.vah, d.val, d.close]).filter((v): v is number => v != null && Number.isFinite(v));
  const lo = Math.min(...all);
  const hi = Math.max(...all);
  const pad = (hi - lo) * 0.08 || 1;

  return (
    <div className="w-full" style={{ height }}>
      <ResponsiveContainer width="100%" height="100%">
        <ComposedChart data={data} margin={{ top: 8, right: 12, left: 4, bottom: 0 }}>
          <defs>
            <linearGradient id="vaFill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={CHART.blue} stopOpacity={0.18} />
              <stop offset="100%" stopColor={CHART.blue} stopOpacity={0.05} />
            </linearGradient>
          </defs>
          <CartesianGrid stroke={CHART.grid} vertical={false} />
          <XAxis dataKey="hour" {...AXIS} />
          <YAxis {...AXIS} width={52} domain={[lo - pad, hi + pad]} tickFormatter={(v) => formatNumber(v, 0)} allowDecimals={false} />
          <Tooltip
            cursor={{ stroke: CHART.axis, strokeWidth: 1 }}
            content={({ active, payload }) => {
              if (!active || !payload?.length) return null;
              const d = payload[0].payload as Row;
              return (
                <TipBox
                  rows={[
                    { k: d.hour, v: `${d.shape} · ${d.bias}` },
                    { k: "VAH", v: formatNumber(d.vah, 1), c: CHART.blue },
                    { k: "POC", v: formatNumber(d.poc, 1), c: CHART.amber },
                    { k: "VAL", v: formatNumber(d.val, 1), c: CHART.blue },
                    { k: "Close", v: formatNumber(d.close, 1), c: "#e6edf3" },
                  ]}
                />
              );
            }}
          />
          {/* value-area band: draw VAH as area down to VAL via stacking trick */}
          <Area type="monotone" dataKey="vah" stroke="none" fill="url(#vaFill)" isAnimationActive={false} connectNulls />
          <Area type="monotone" dataKey="val" stroke="none" fill={CHART.surface} fillOpacity={1} isAnimationActive={false} connectNulls />
          <Line type="monotone" dataKey="vah" stroke={CHART.blue} strokeWidth={1} dot={false} strokeDasharray="4 3" isAnimationActive={false} connectNulls />
          <Line type="monotone" dataKey="val" stroke={CHART.blue} strokeWidth={1} dot={false} strokeDasharray="4 3" isAnimationActive={false} connectNulls />
          <Line type="monotone" dataKey="poc" stroke={CHART.amber} strokeWidth={2} dot={{ r: 2.5, fill: CHART.amber }} isAnimationActive={false} connectNulls />
          <Line type="monotone" dataKey="close" stroke="#e6edf3" strokeWidth={1.5} dot={false} isAnimationActive={false} connectNulls />
        </ComposedChart>
      </ResponsiveContainer>
      <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-[10px] text-text-muted">
        <Legend color={CHART.amber} label="POC" solid />
        <Legend color={CHART.blue} label="VAH / VAL" />
        <Legend color="#e6edf3" label="Hourly close" solid />
        <span className="text-text-muted">band = value area</span>
      </div>
    </div>
  );
}

function Legend({ color, label, solid }: { color: string; label: string; solid?: boolean }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className="inline-block h-0.5 w-4 rounded-full" style={{ background: color, opacity: solid ? 1 : 0.7, borderTop: solid ? undefined : `1px dashed ${color}` }} />
      {label}
    </span>
  );
}

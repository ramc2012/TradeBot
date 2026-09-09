"use client";

import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import {
  Area,
  AreaChart,
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { api } from "@/lib/api";

type Slice = {
  expiry: string;
  fit_status: string;
  n_used: number;
  n_rejects: number;
  rmse_vol: number | null;
  reason: string;
  butterfly_ok: boolean;
  calendar_ok: boolean | null;
};
type Point = {
  ts: string;
  tenor_kind: string;
  tenor_days: number;
  atm_iv: number | null;
  realized_vol: number | null;
  rr_25: number | null;
  bf_25: number | null;
  status: string;
};
type Desk = {
  underlying: string;
  as_of: string | null;
  status: string;
  snapshot_id?: string;
  selected_expiry?: string;
  slices: Slice[];
  series: Point[];
  quarantine: { stage: string; status: string; n: number }[];
  limitations: string[];
  participant_scope?: string;
  participant_oi: {
    dt: string;
    participant: string;
    bucket: string;
    long_contracts: number;
    short_contracts: number;
  }[];
  curves: {
    status: string;
    reason: string;
    density_mass: number | null;
    smile: {
      strike: number;
      moneyness_pct: number;
      iv_pct: number;
      density: number;
    }[];
    scenarios: {
      move_pct: number;
      vol_points: number;
      CE: number;
      PE: number;
    }[];
    greeks: Record<
      string,
      {
        delta: number;
        gamma: number;
        vega: number;
        theta: number;
        vanna: number;
        charm: number;
      }
    > | null;
  };
};
const card = "rounded-xl border border-bg-border bg-bg-secondary/50 p-4";
const axis = { fill: "#94a3b8", fontSize: 10 };
const tip = {
  background: "#111827",
  border: "1px solid #334155",
  borderRadius: 8,
  fontSize: 11,
  color: "#e2e8f0",
};
const number = (v: number | null | undefined, digits = 2) =>
  v == null || !Number.isFinite(v)
    ? "—"
    : v.toLocaleString("en-IN", { maximumFractionDigits: digits });
const when = (v: string | null | undefined) =>
  v
    ? new Date(v).toLocaleString("en-IN", {
        timeZone: "Asia/Kolkata",
        day: "2-digit",
        month: "short",
        hour: "2-digit",
        minute: "2-digit",
      }) + " IST"
    : "No observation";
function Empty({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex min-h-[160px] items-center justify-center p-6 text-center text-sm text-text-muted">
      {children}
    </div>
  );
}

export default function DistributionDesk({
  underlying,
}: {
  underlying: string;
}) {
  const [tenor, setTenor] = useState("front");
  const [kind, setKind] = useState<"CE" | "PE">("CE");
  const query = useQuery<Desk>({
    queryKey: ["directional", "distribution", underlying],
    queryFn: async () =>
      (
        await api.get("/api/directional-options/distribution", {
          params: { underlying },
        })
      ).data,
    staleTime: 60_000,
    refetchInterval: 60_000,
    refetchOnWindowFocus: false,
  });
  const d = query.data;
  const history = useMemo(
    () =>
      (d?.series ?? [])
        .filter((p) => p.tenor_kind === tenor)
        .map((p) => ({
          ...p,
          time: new Date(p.ts).getTime(),
          iv: p.atm_iv == null ? null : p.atm_iv * 100,
          rv: p.realized_vol == null ? null : p.realized_vol * 100,
          rr: p.rr_25 == null ? null : p.rr_25 * 100,
          bf: p.bf_25 == null ? null : p.bf_25 * 100,
        })),
    [d, tenor],
  );
  if (query.isError)
    return (
      <div className={card}>
        <p className="text-red-400">Distribution data could not be loaded.</p>
        <button
          className="mt-3 text-sm underline"
          onClick={() => query.refetch()}
        >
          Retry stored data
        </button>
      </div>
    );
  if (!d)
    return (
      <div className={card}>
        <Empty>Loading the shared surface archive…</Empty>
      </div>
    );
  const latest = history.at(-1);
  const valid = d.curves.status === "ok";
  const greeks = d.curves.greeks?.[kind];
  const unsupported = d.status === "unsupported_stock_surface";
  return (
    <div className="space-y-4">
      <div
        className={`${card} flex flex-wrap items-start justify-between gap-4`}
      >
        <div>
          <div className="text-[10px] uppercase tracking-[0.2em] text-cyan-400">
            Distribution laboratory · paper research
          </div>
          <h2 className="mt-1 text-xl font-semibold">
            {underlying} · Beyond the contract
          </h2>
          <p className="mt-1 text-xs text-text-muted">
            Observed {when(d.as_of)} ·{" "}
            {d.selected_expiry
              ? `Surface expiry ${d.selected_expiry}`
              : "Surface unavailable"}
          </p>
        </div>
        <div className="text-right">
          <span
            className={`rounded-full border px-3 py-1 text-xs ${d.status === "available" ? "border-cyan-700 text-cyan-300" : "border-amber-700/60 text-amber-300"}`}
          >
            {d.status.replaceAll("_", " ")}
          </span>
          <p className="mt-3 text-[10px] text-text-muted">
            Shared archive · {d.snapshot_id?.slice(0, 10) || "no snapshot"}
          </p>
          <Link
            className="mt-1 inline-block text-xs text-cyan-400 underline"
            href="/strategies/index-swing"
          >
            Open the separate 1–5 session index book →
          </Link>
        </div>
      </div>
      {unsupported ? (
        <div className={card}>
          <h3 className="font-medium">Stock directional research</h3>
          <p className="mt-2 text-sm text-text-secondary">
            This feed does not support a reliable single-stock volatility
            surface. Use the live overview’s spot structure, futures positioning
            and contract payoff geometry. Index distributions are available for
            NIFTY, BANKNIFTY and SENSEX.
          </p>
        </div>
      ) : (
        <>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="text-xs text-text-muted">
              Annualised volatility · all timestamps IST
            </div>
            <div className="flex rounded-lg border border-bg-border p-1">
              {[
                ["front", "Front expiry"],
                ["grid", "Constant 30 days"],
              ].map(([key, label]) => (
                <button
                  key={key}
                  onClick={() => setTenor(key)}
                  className={`rounded-md px-3 py-1 text-xs ${tenor === key ? "bg-cyan-950 text-cyan-200" : "text-text-muted"}`}
                >
                  {label}
                </button>
              ))}
            </div>
          </div>
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
            {[
              [
                "ATM IV",
                number(latest?.iv) + "%",
                tenor === "front"
                  ? "Expiry changes through time"
                  : "Total-variance interpolation",
              ],
              [
                "Realised volatility",
                number(latest?.rv) + "%",
                "Causal daily history",
              ],
              [
                "25Δ risk reversal",
                number(latest?.rr) + " vp",
                "Call IV − put IV",
              ],
              [
                "25Δ butterfly",
                number(latest?.bf) + " vp",
                "Mean wing IV − ATM IV",
              ],
            ].map(([label, value, hint]) => (
              <div key={label} className={card}>
                <div className="text-xs text-text-muted">{label}</div>
                <div className="mt-2 font-mono text-2xl">{value}</div>
                <div className="mt-1 text-[10px] text-text-muted">{hint}</div>
              </div>
            ))}
          </div>
          <div className="grid gap-4 xl:grid-cols-2">
            <div className={card}>
              <h3 className="text-sm font-medium">
                Volatility and realised risk
              </h3>
              <p className="mb-3 mt-1 text-xs text-text-muted">
                {tenor === "front"
                  ? "Rolling front expiry; not a constant-maturity backtest feature."
                  : "Refused coordinates appear as gaps. No expiry extrapolation."}{" "}
                Latest point {when(latest?.ts)}.
              </p>
              {history.some((p) => p.iv != null || p.rv != null) ? (
                <ResponsiveContainer width="100%" height={240}>
                  <LineChart data={history} syncId="vol-series">
                    <CartesianGrid
                      stroke="#334155"
                      strokeDasharray="3 6"
                      vertical={false}
                    />
                    <XAxis
                      dataKey="time"
                      type="number"
                      domain={["dataMin", "dataMax"]}
                      tick={axis}
                      tickFormatter={(v) =>
                        new Date(v).toLocaleDateString("en-IN", {
                          timeZone: "Asia/Kolkata",
                          day: "2-digit",
                          month: "short",
                        })
                      }
                    />
                    <YAxis tick={axis} unit="%" domain={["auto", "auto"]} />
                    <Tooltip
                      contentStyle={tip}
                      labelFormatter={(v) =>
                        when(new Date(Number(v)).toISOString())
                      }
                      formatter={(v: number) => `${number(v)}%`}
                    />
                    <Legend />
                    <Line
                      name="Implied vol"
                      dataKey="iv"
                      stroke="#22d3ee"
                      strokeWidth={2}
                      dot={false}
                      connectNulls={false}
                    />
                    <Line
                      name="Realised vol"
                      dataKey="rv"
                      stroke="#c084fc"
                      strokeWidth={1.5}
                      dot={false}
                      connectNulls={false}
                    />
                  </LineChart>
                </ResponsiveContainer>
              ) : (
                <Empty>
                  No usable {tenor === "grid" ? "30-day" : "front"}{" "}
                  observations. More bracketing expiries are needed.
                </Empty>
              )}
            </div>
            <div className={card}>
              <h3 className="text-sm font-medium">Skew and convexity</h3>
              <p className="mb-3 mt-1 text-xs text-text-muted">
                Constant 25Δ wings · vol points · availability checked
                independently
              </p>
              {history.some((p) => p.rr != null || p.bf != null) ? (
                <ResponsiveContainer width="100%" height={240}>
                  <LineChart data={history} syncId="vol-series">
                    <CartesianGrid
                      stroke="#334155"
                      strokeDasharray="3 6"
                      vertical={false}
                    />
                    <XAxis
                      dataKey="time"
                      type="number"
                      domain={["dataMin", "dataMax"]}
                      tick={axis}
                      tickFormatter={(v) =>
                        new Date(v).toLocaleDateString("en-IN", {
                          timeZone: "Asia/Kolkata",
                          day: "2-digit",
                          month: "short",
                        })
                      }
                    />
                    <YAxis tick={axis} />
                    <Tooltip
                      contentStyle={tip}
                      labelFormatter={(v) =>
                        when(new Date(Number(v)).toISOString())
                      }
                      formatter={(v: number) => `${number(v)} vp`}
                    />
                    <Legend />
                    <Line
                      name="Risk reversal"
                      dataKey="rr"
                      stroke="#fb923c"
                      strokeWidth={2}
                      dot={false}
                      connectNulls={false}
                    />
                    <Line
                      name="Butterfly"
                      dataKey="bf"
                      stroke="#a3e635"
                      dot={false}
                      connectNulls={false}
                    />
                  </LineChart>
                </ResponsiveContainer>
              ) : (
                <Empty>
                  The observed strikes do not bracket both 25Δ wings.
                </Empty>
              )}
            </div>
            <div className={card}>
              <h3 className="text-sm font-medium">
                SVI smile · observed strike support
              </h3>
              <p className="mb-3 mt-1 text-xs text-text-muted">
                {d.curves.reason}
              </p>
              {valid ? (
                <ResponsiveContainer width="100%" height={230}>
                  <LineChart data={d.curves.smile} syncId="surface">
                    <CartesianGrid
                      stroke="#334155"
                      strokeDasharray="3 6"
                      vertical={false}
                    />
                    <XAxis
                      dataKey="moneyness_pct"
                      tick={axis}
                      tickFormatter={(v) => `${number(v, 1)}%`}
                      minTickGap={45}
                    />
                    <YAxis tick={axis} domain={["auto", "auto"]} unit="%" />
                    <Tooltip
                      contentStyle={tip}
                      labelFormatter={(v) =>
                        `Forward moneyness ${number(Number(v))}%`
                      }
                      formatter={(v: number) => `${number(v)}%`}
                    />
                    <Line
                      dataKey="iv_pct"
                      name="Fitted IV"
                      stroke="#22d3ee"
                      strokeWidth={2}
                      dot={false}
                    />
                  </LineChart>
                </ResponsiveContainer>
              ) : (
                <Empty>
                  Latest fit unavailable. Older fits are not substituted.
                </Empty>
              )}
            </div>
            <div className={card}>
              <h3 className="text-sm font-medium">
                Risk-neutral density · truncated tails
              </h3>
              <p className="mb-3 mt-1 text-xs text-text-muted">
                {valid
                  ? `${number((d.curves.density_mass ?? 0) * 100)}% mass inside observed support. `
                  : ""}
                A pricing distribution, not a forecast.
              </p>
              {valid ? (
                <ResponsiveContainer width="100%" height={230}>
                  <AreaChart data={d.curves.smile} syncId="surface">
                    <defs>
                      <linearGradient
                        id="densityFill"
                        x1="0"
                        y1="0"
                        x2="0"
                        y2="1"
                      >
                        <stop
                          offset="0%"
                          stopColor="#c084fc"
                          stopOpacity={0.5}
                        />
                        <stop
                          offset="100%"
                          stopColor="#c084fc"
                          stopOpacity={0.02}
                        />
                      </linearGradient>
                    </defs>
                    <CartesianGrid
                      stroke="#334155"
                      strokeDasharray="3 6"
                      vertical={false}
                    />
                    <XAxis
                      dataKey="moneyness_pct"
                      tick={axis}
                      tickFormatter={(v) => `${number(v, 1)}%`}
                      minTickGap={45}
                    />
                    <YAxis
                      tick={axis}
                      tickFormatter={(v) => Number(v).toExponential(1)}
                    />
                    <Tooltip
                      contentStyle={tip}
                      labelFormatter={(v) =>
                        `Forward moneyness ${number(Number(v))}%`
                      }
                      formatter={(v: number) => number(v, 7)}
                    />
                    <Area
                      name="Density per index point"
                      dataKey="density"
                      stroke="#c084fc"
                      fill="url(#densityFill)"
                      isAnimationActive={false}
                    />
                  </AreaChart>
                </ResponsiveContainer>
              ) : (
                <Empty>
                  Density requires a positive-variance fit and nonnegative
                  butterfly checks.
                </Empty>
              )}
            </div>
          </div>
          <div className="grid gap-4 lg:grid-cols-[1.4fr_1fr]">
            <div className={card}>
              <div className="flex items-center justify-between">
                <h3 className="text-sm font-medium">One-day shock grid</h3>
                <div className="flex gap-1">
                  {(["CE", "PE"] as const).map((k) => (
                    <button
                      key={k}
                      onClick={() => setKind(k)}
                      className={`rounded px-3 py-1 text-xs ${kind === k ? "bg-cyan-950 text-cyan-200" : "text-text-muted"}`}
                    >
                      {k === "CE" ? "ATM call" : "ATM put"}
                    </button>
                  ))}
                </div>
              </div>
              <p className="my-3 text-xs text-text-muted">
                Hypothetical ₹ P&L per unit · forward move × IV shock · before
                costs
              </p>
              {d.curves.scenarios.length ? (
                <div className="overflow-x-auto">
                  <table className="w-full min-w-[430px] text-center text-xs">
                    <thead>
                      <tr>
                        <th className="p-2 text-text-muted">IV / F</th>
                        {[-3, -2, -1, 0, 1, 2, 3].map((m) => (
                          <th
                            className="p-2 font-normal text-text-muted"
                            key={m}
                          >
                            {m > 0 ? "+" : ""}
                            {m}%
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {[-2, 0, 2].map((v) => (
                        <tr key={v}>
                          <th className="p-2 font-normal text-text-muted">
                            {v > 0 ? "+" : ""}
                            {v} vp
                          </th>
                          {[-3, -2, -1, 0, 1, 2, 3].map((m) => {
                            const cell = d.curves.scenarios.find(
                              (c) => c.move_pct === m && c.vol_points === v,
                            );
                            const value = cell?.[kind];
                            return (
                              <td
                                key={m}
                                className={`border-2 border-bg-primary p-3 font-mono ${value == null ? "text-text-muted" : value >= 0 ? "bg-emerald-950/70 text-emerald-300" : "bg-red-950/50 text-red-300"}`}
                              >
                                {number(value, 1)}
                              </td>
                            );
                          })}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <Empty>No ATM coordinate within the accepted fit.</Empty>
              )}
            </div>
            <div className={card}>
              <h3 className="text-sm font-medium">
                Unit Greeks · {kind === "CE" ? "ATM call" : "ATM put"}
              </h3>
              <p className="mt-1 text-xs text-text-muted">
                Same hypothetical contract as the shock grid
              </p>
              {greeks ? (
                <dl className="mt-4 grid grid-cols-2 gap-4">
                  {[
                    ["Delta / point", greeks.delta],
                    ["Gamma / point²", greeks.gamma],
                    ["Vega / vol point", greeks.vega * 0.01],
                    ["Theta / calendar day", greeks.theta],
                    ["Vanna / vol point", greeks.vanna * 0.01],
                    ["Charm / calendar day", greeks.charm],
                  ].map(([label, value]) => (
                    <div key={String(label)}>
                      <dt className="text-[10px] text-text-muted">{label}</dt>
                      <dd className="mt-1 font-mono text-lg">
                        {number(Number(value), 5)}
                      </dd>
                    </div>
                  ))}
                </dl>
              ) : (
                <Empty>Greeks unavailable.</Empty>
              )}
            </div>
          </div>
          <div className={card}>
            <h3 className="text-sm font-medium">
              Surface quality · latest batch
            </h3>
            <div className="mt-3 overflow-x-auto">
              <table className="w-full text-left text-xs">
                <thead className="text-text-muted">
                  <tr>
                    {[
                      "Expiry",
                      "Fit",
                      "Used / rejected",
                      "Residual (vp)",
                      "Calendar",
                      "Reason",
                    ].map((h) => (
                      <th key={h} className="p-2 font-normal">
                        {h}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {d.slices.map((s) => (
                    <tr className="border-t border-bg-border" key={s.expiry}>
                      <td className="p-2">{s.expiry}</td>
                      <td
                        className={`p-2 ${s.fit_status === "ok" ? "text-cyan-300" : "text-amber-300"}`}
                      >
                        {s.fit_status}
                      </td>
                      <td className="p-2">
                        {s.n_used} / {s.n_rejects}
                      </td>
                      <td className="p-2">
                        {number(
                          s.rmse_vol == null ? null : s.rmse_vol * 100,
                          3,
                        )}
                      </td>
                      <td className="p-2">
                        {s.calendar_ok == null
                          ? "Not comparable"
                          : s.calendar_ok
                            ? "Passed grid"
                            : "Failed"}
                      </td>
                      <td className="p-2 text-text-muted">{s.reason || "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="mt-3 text-xs text-text-muted">
              30-day quarantine:{" "}
              {d.quarantine
                .map((q) => `${q.stage}/${q.status}: ${q.n}`)
                .join(" · ") || "No records"}
            </p>
          </div>
        </>
      )}
      <details className={card}>
        <summary className="cursor-pointer text-sm">
          Participant positioning · NSE aggregate{" "}
          {d.participant_oi[0]?.dt || "unavailable"}
        </summary>
        <p className="my-3 text-xs text-text-muted">
          {d.participant_scope ||
            "Market-wide NSE data is not a stock-level signal."}
        </p>
        <div className="max-h-72 overflow-auto">
          <table className="w-full text-left text-xs">
            <thead>
              <tr>
                {[
                  "Participant",
                  "Instrument class",
                  "Long contracts",
                  "Short contracts",
                  "Net contracts",
                ].map((h) => (
                  <th key={h} className="p-2 font-normal text-text-muted">
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {d.participant_oi.map((p) => (
                <tr
                  key={`${p.participant}-${p.bucket}`}
                  className="border-t border-bg-border"
                >
                  <td className="p-2">{p.participant}</td>
                  <td className="p-2">{p.bucket}</td>
                  <td className="p-2 font-mono">
                    {number(p.long_contracts, 0)}
                  </td>
                  <td className="p-2 font-mono">
                    {number(p.short_contracts, 0)}
                  </td>
                  <td className="p-2 font-mono">
                    {number(p.long_contracts - p.short_contracts, 0)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
      <details className={card}>
        <summary className="cursor-pointer text-xs text-text-muted">
          Conventions and data boundaries
        </summary>
        <ul className="mt-3 space-y-2 text-xs text-text-secondary">
          {d.limitations.map((note) => (
            <li key={note}>{note}</li>
          ))}
        </ul>
      </details>
    </div>
  );
}

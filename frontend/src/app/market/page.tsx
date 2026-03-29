"use client";

import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { clsx } from "clsx";
import { Activity, ArrowDownRight, ArrowUpRight, BarChart3, Minus, Radar, RefreshCw } from "lucide-react";

import { getMarketProfile, getOptionChain, getOptionExpiries } from "@/lib/api";
import { MARKET_INDEX_SYMBOLS, type MarketIndexSymbol, getMarketIndexLabel } from "@/lib/marketSymbols";
import { useTickSymbol } from "@/store";

type ChainEntry = {
  strike: number;
  option_type: "CE" | "PE";
  ltp: number;
  oi: number;
  volume: number;
  iv?: number | null;
  delta?: number | null;
  gamma?: number | null;
  theta?: number | null;
  vega?: number | null;
  oi_change?: number | null;
  oi_change_pct?: number | null;
  ltp_change?: number | null;
  ltp_change_pct?: number | null;
};

type OptionChainPayload = {
  symbol: string;
  expiry: string;
  spot_price: number;
  entries: ChainEntry[];
  pcr_oi: number;
  pcr_volume: number;
  pcr_prev_oi?: number | null;
  pcr_oi_change?: number | null;
  max_pain: number;
  atm_strike: number;
  atm_iv: number;
  total_ce_oi: number;
  total_pe_oi: number;
  total_ce_oi_change?: number | null;
  total_pe_oi_change?: number | null;
  total_ce_volume?: number | null;
  total_pe_volume?: number | null;
  atm_call_ltp_change?: number | null;
  atm_call_ltp_change_pct?: number | null;
  atm_put_ltp_change?: number | null;
  atm_put_ltp_change_pct?: number | null;
  atm_call_oi_change?: number | null;
  atm_put_oi_change?: number | null;
  timestamp?: string;
  error?: string;
};

type ExpiryPayload = {
  symbol: string;
  expiries: string[];
  default_expiry?: string | null;
};

type MarketProfilePayload = {
  symbol: string;
  timeframe: "day" | "week" | "month" | "hourly";
  date: string;
  poc: number;
  vah: number;
  val: number;
  ib_high: number;
  ib_low: number;
  source_interval?: string;
  sample_count?: number;
  coverage_start?: string | null;
  coverage_end?: string | null;
  error?: string;
};

function formatChangePct(ltp?: number, close?: number) {
  if (!ltp || !close) return "--";
  const pct = ((ltp - close) / close) * 100;
  const prefix = pct > 0 ? "+" : "";
  return `${prefix}${pct.toFixed(2)}%`;
}

function formatSigned(value?: number | null, decimals = 2, suffix = "") {
  if (value == null || Number.isNaN(value)) return "--";
  const prefix = value > 0 ? "+" : "";
  return `${prefix}${value.toFixed(decimals)}${suffix}`;
}

function formatIv(value?: number | null) {
  if (value == null || Number.isNaN(value)) return "--";
  const normalized = value > 5 ? value : value * 100;
  return `${normalized.toFixed(1)}%`;
}

function formatCompact(value?: number | null) {
  if (value == null || Number.isNaN(value)) return "--";
  if (Math.abs(value) >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (Math.abs(value) >= 1_000) return `${(value / 1_000).toFixed(1)}k`;
  return `${Math.round(value)}`;
}

function valueTone(value?: number | null) {
  if (value == null || Number.isNaN(value)) return "text-text-muted";
  if (value > 0) return "text-accent-green";
  if (value < 0) return "text-accent-red";
  return "text-text-secondary";
}

function directionMeta(value?: number | null) {
  if (value == null || Number.isNaN(value)) {
    return {
      badge: "bg-bg-primary text-text-muted",
      icon: <Minus size={12} />,
      label: "Flat",
      tone: "text-text-muted",
    };
  }
  if (value > 0) {
    return {
      badge: "bg-accent-green/12 text-accent-green border-accent-green/20",
      icon: <ArrowUpRight size={12} />,
      label: "Up",
      tone: "text-accent-green",
    };
  }
  if (value < 0) {
    return {
      badge: "bg-accent-red/12 text-accent-red border-accent-red/20",
      icon: <ArrowDownRight size={12} />,
      label: "Down",
      tone: "text-accent-red",
    };
  }
  return {
    badge: "bg-bg-primary text-text-secondary border-bg-border",
    icon: <Minus size={12} />,
    label: "Flat",
    tone: "text-text-secondary",
  };
}

function LiveIndexCard({ symbol, active, onSelect }: {
  symbol: MarketIndexSymbol;
  active: boolean;
  onSelect: (symbol: MarketIndexSymbol) => void;
}) {
  const tick = useTickSymbol(symbol);
  const positive = tick && tick.close > 0 ? tick.ltp >= tick.close : undefined;

  return (
    <button
      onClick={() => onSelect(symbol)}
      className={clsx(
        "rounded-2xl border p-4 text-left transition-colors",
        active
          ? "border-accent-blue bg-accent-blue/10"
          : "border-bg-border bg-bg-secondary/55 hover:border-bg-active hover:bg-bg-secondary/80"
      )}
    >
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="text-[11px] uppercase tracking-[0.14em] text-text-muted">
            {getMarketIndexLabel(symbol)}
          </div>
          <div className="mt-2 font-mono text-2xl font-semibold text-text-primary">
            {tick ? tick.ltp.toFixed(2) : "--"}
          </div>
        </div>
        <div
          className={clsx(
            "rounded-full px-2 py-1 text-[11px] font-semibold",
            positive === undefined
              ? "bg-bg-primary text-text-muted"
              : positive
                ? "bg-accent-green/12 text-accent-green"
                : "bg-accent-red/12 text-accent-red"
          )}
        >
          {tick ? formatChangePct(tick.ltp, tick.close) : "Waiting"}
        </div>
      </div>
      <div className="mt-3 grid grid-cols-2 gap-2 text-xs text-text-secondary">
        <div>Open {tick?.open ? tick.open.toFixed(2) : "--"}</div>
        <div>Prev {tick?.close ? tick.close.toFixed(2) : "--"}</div>
        <div>High {tick?.high ? tick.high.toFixed(2) : "--"}</div>
        <div>Low {tick?.low ? tick.low.toFixed(2) : "--"}</div>
      </div>
    </button>
  );
}

function PulseRow({ label, value, delta, tone = "text-text-primary" }: {
  label: string;
  value: string;
  delta?: string;
  tone?: string;
}) {
  return (
    <div className="border-b border-bg-border/50 py-2 text-sm last:border-b-0">
      <div className="flex items-center justify-between gap-3">
        <span className="text-text-muted">{label}</span>
        <span className={clsx("font-mono font-semibold", tone)}>{value}</span>
      </div>
      {delta && (
        <div className="mt-1 text-right text-[11px] text-text-muted">{delta}</div>
      )}
    </div>
  );
}

function PulseIndicatorCard({
  label,
  value,
  detail,
  tone = "text-text-primary",
  directionValue,
}: {
  label: string;
  value: string;
  detail?: string;
  tone?: string;
  directionValue?: number | null;
}) {
  const direction = directionMeta(directionValue);
  const hasDirection = directionValue != null && !Number.isNaN(directionValue);

  return (
    <div className="rounded-xl border border-bg-border bg-bg-secondary/45 p-3">
      <div className="flex items-center justify-between gap-3">
        <div className="text-[11px] uppercase tracking-[0.14em] text-text-muted">{label}</div>
        {hasDirection && (
          <div className={clsx("inline-flex items-center gap-1 rounded-full border px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.14em]", direction.badge)}>
            {direction.icon}
            {direction.label}
          </div>
        )}
      </div>
      <div className="mt-2 flex items-center gap-2">
        {hasDirection && <span className={direction.tone}>{direction.icon}</span>}
        <span className={clsx("font-mono text-lg font-semibold", tone)}>{value}</span>
      </div>
      {detail && (
        <div className={clsx("mt-2 text-xs font-medium", hasDirection ? direction.tone : "text-text-muted")}>
          {detail}
        </div>
      )}
    </div>
  );
}

export default function MarketPage() {
  const [symbol, setSymbol] = useState<MarketIndexSymbol>("NSE:NIFTY50-INDEX");
  const [expiry, setExpiry] = useState("");
  const [profileTimeframe, setProfileTimeframe] = useState<"day" | "week" | "month">("day");
  const selectedTick = useTickSymbol(symbol);

  const expiriesQuery = useQuery<ExpiryPayload>({
    queryKey: ["optionExpiries", symbol],
    queryFn: () => getOptionExpiries(symbol).then((response) => response.data),
    staleTime: 60000,
    refetchInterval: 60000,
  });

  useEffect(() => {
    const available = expiriesQuery.data?.expiries ?? [];
    const defaultExpiry = expiriesQuery.data?.default_expiry || available[0] || "";
    if (!defaultExpiry) return;
    if (!expiry || !available.includes(expiry)) {
      setExpiry(defaultExpiry);
    }
  }, [expiry, expiriesQuery.data]);

  const chainQuery = useQuery<OptionChainPayload>({
    queryKey: ["optionChain", symbol, expiry],
    queryFn: () => getOptionChain(symbol, expiry || undefined).then((response) => response.data),
    refetchInterval: 15000,
    staleTime: 5000,
  });

  const profileQuery = useQuery<MarketProfilePayload>({
    queryKey: ["marketProfile", symbol, profileTimeframe],
    queryFn: () => getMarketProfile(symbol, profileTimeframe).then((response) => response.data),
    refetchInterval: 30000,
    staleTime: 5000,
  });

  const chain = chainQuery.data;
  const profile = profileQuery.data;
  const entries = chain?.entries ?? [];
  const ceEntries = entries.filter((entry) => entry.option_type === "CE");
  const peEntries = entries.filter((entry) => entry.option_type === "PE");
  const strikes = Array.from(new Set(entries.map((entry) => entry.strike))).sort((a, b) => a - b);
  const atmIndex = Math.max(0, strikes.findIndex((strike) => strike === chain?.atm_strike));
  const visibleStrikes = strikes.length > 0
    ? strikes.slice(Math.max(0, atmIndex - 8), Math.min(strikes.length, atmIndex + 9))
    : [];
  const liveSpot = selectedTick?.ltp || chain?.spot_price || 0;
  const spotPositive = selectedTick && selectedTick.close > 0 ? selectedTick.ltp >= selectedTick.close : undefined;

  return (
    <div className="mx-auto max-w-[1800px] space-y-5 pb-8">
      <section className="space-y-3">
        <div className="flex items-center justify-between gap-3">
          <div>
            <h1 className="font-mono text-2xl font-semibold text-text-primary">Market Intelligence</h1>
            <div className="mt-1 text-sm text-text-muted">
              Live index board, richer option-chain analytics, and market-profile windows from the active broker feed.
            </div>
          </div>
          <div className="flex items-center gap-2 rounded-full border border-bg-border bg-bg-secondary/60 px-3 py-1.5 text-xs text-text-muted">
            <Activity size={13} className="text-accent-green" />
            Streaming index LTP
          </div>
        </div>

        <div className="grid gap-4 xl:grid-cols-4">
          {MARKET_INDEX_SYMBOLS.map((indexSymbol) => (
            <LiveIndexCard
              key={indexSymbol}
              symbol={indexSymbol}
              active={symbol === indexSymbol}
              onSelect={setSymbol}
            />
          ))}
        </div>
      </section>

      <div className="grid gap-5 xl:grid-cols-[1.45fr_0.85fr]">
        <section className="card rounded-2xl p-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <div className="text-[11px] uppercase tracking-[0.14em] text-text-muted">Option Chain</div>
              <div className="mt-1 flex items-center gap-3">
                <div className="font-mono text-xl font-semibold text-text-primary">
                  {getMarketIndexLabel(symbol)}
                </div>
                <div
                  className={clsx(
                    "rounded-full px-2 py-1 text-xs font-semibold",
                    spotPositive === undefined
                      ? "bg-bg-secondary text-text-muted"
                      : spotPositive
                        ? "bg-accent-green/12 text-accent-green"
                        : "bg-accent-red/12 text-accent-red"
                  )}
                >
                  {selectedTick ? formatChangePct(selectedTick.ltp, selectedTick.close) : "Waiting"}
                </div>
              </div>
            </div>

            <div className="flex flex-wrap items-center gap-2">
              {MARKET_INDEX_SYMBOLS.map((indexSymbol) => (
                <button
                  key={indexSymbol}
                  onClick={() => setSymbol(indexSymbol)}
                  className={clsx(
                    "rounded-lg border px-2.5 py-1.5 text-xs font-medium transition-colors",
                    symbol === indexSymbol
                      ? "border-accent-blue bg-accent-blue/12 text-accent-blue"
                      : "border-bg-border bg-bg-secondary/45 text-text-muted hover:text-text-primary"
                  )}
                >
                  {getMarketIndexLabel(indexSymbol)}
                </button>
              ))}
              <select
                value={expiry}
                onChange={(event) => setExpiry(event.target.value)}
                className="terminal-input min-w-[148px] py-1.5 text-xs"
              >
                {(expiriesQuery.data?.expiries ?? []).map((item) => (
                  <option key={item} value={item}>{item}</option>
                ))}
                {!expiriesQuery.data?.expiries?.length && <option value="">Expiry loading...</option>}
              </select>
              <button
                onClick={() => {
                  void expiriesQuery.refetch();
                  void chainQuery.refetch();
                  void profileQuery.refetch();
                }}
                className="rounded-lg border border-bg-border bg-bg-secondary/45 p-2 text-text-muted transition-colors hover:text-text-primary"
                aria-label="Refresh market data"
              >
                <RefreshCw size={14} />
              </button>
            </div>
          </div>

          <div className="mt-4 grid gap-3 md:grid-cols-4">
            <div className="rounded-xl border border-bg-border bg-bg-secondary/50 p-3">
              <div className="text-[11px] uppercase tracking-[0.14em] text-text-muted">Spot</div>
              <div className="mt-1 font-mono text-lg font-semibold text-text-primary">
                {liveSpot > 0 ? liveSpot.toFixed(2) : "--"}
              </div>
            </div>
            <div className="rounded-xl border border-bg-border bg-bg-secondary/50 p-3">
              <div className="text-[11px] uppercase tracking-[0.14em] text-text-muted">Expiry</div>
              <div className="mt-1 font-mono text-lg font-semibold text-text-primary">
                {chain?.expiry || "--"}
              </div>
            </div>
            <div className="rounded-xl border border-bg-border bg-bg-secondary/50 p-3">
              <div className="text-[11px] uppercase tracking-[0.14em] text-text-muted">Session High</div>
              <div className="mt-1 font-mono text-lg font-semibold text-text-primary">
                {selectedTick?.high ? selectedTick.high.toFixed(2) : "--"}
              </div>
            </div>
            <div className="rounded-xl border border-bg-border bg-bg-secondary/50 p-3">
              <div className="text-[11px] uppercase tracking-[0.14em] text-text-muted">Session Low</div>
              <div className="mt-1 font-mono text-lg font-semibold text-text-primary">
                {selectedTick?.low ? selectedTick.low.toFixed(2) : "--"}
              </div>
            </div>
          </div>

          <div className="mt-4 overflow-x-auto">
            <table className="w-full min-w-[1600px] text-xs font-mono">
              <thead>
                <tr className="border-b border-bg-border text-text-muted">
                  <th className="pb-2 pr-2 text-right">CE OI</th>
                  <th className="pb-2 pr-2 text-right">CE Chg OI</th>
                  <th className="pb-2 pr-2 text-right">CE Vol</th>
                  <th className="pb-2 pr-2 text-right">CE IV</th>
                  <th className="pb-2 pr-2 text-right">CE Δ</th>
                  <th className="pb-2 pr-2 text-right">CE Γ</th>
                  <th className="pb-2 pr-2 text-right">CE Θ</th>
                  <th className="pb-2 pr-2 text-right">CE V</th>
                  <th className="pb-2 pr-2 text-right">CE LTP</th>
                  <th className="pb-2 pr-2 text-right">CE Chg%</th>
                  <th className="pb-2 px-3 text-center text-accent-amber">STRIKE</th>
                  <th className="pb-2 pl-2 text-left">PE LTP</th>
                  <th className="pb-2 pl-2 text-left">PE Chg%</th>
                  <th className="pb-2 pl-2 text-left">PE IV</th>
                  <th className="pb-2 pl-2 text-left">PE Δ</th>
                  <th className="pb-2 pl-2 text-left">PE Γ</th>
                  <th className="pb-2 pl-2 text-left">PE Θ</th>
                  <th className="pb-2 pl-2 text-left">PE V</th>
                  <th className="pb-2 pl-2 text-left">PE Vol</th>
                  <th className="pb-2 pl-2 text-left">PE Chg OI</th>
                  <th className="pb-2 pl-2 text-left">PE OI</th>
                </tr>
              </thead>
              <tbody>
                {visibleStrikes.map((strike) => {
                  const ce = ceEntries.find((entry) => entry.strike === strike);
                  const pe = peEntries.find((entry) => entry.strike === strike);
                  const isAtm = chain?.atm_strike === strike;
                  return (
                    <tr
                      key={strike}
                      className={clsx(
                        "border-b border-bg-border/40",
                        isAtm && "bg-accent-amber/8"
                      )}
                    >
                      <td className="py-2 pr-2 text-right text-accent-green">{formatCompact(ce?.oi)}</td>
                      <td className={clsx("py-2 pr-2 text-right", valueTone(ce?.oi_change))}>{formatCompact(ce?.oi_change)}</td>
                      <td className="py-2 pr-2 text-right text-text-secondary">{formatCompact(ce?.volume)}</td>
                      <td className="py-2 pr-2 text-right text-text-secondary">{formatIv(ce?.iv)}</td>
                      <td className="py-2 pr-2 text-right text-text-primary">{formatSigned(ce?.delta, 3)}</td>
                      <td className="py-2 pr-2 text-right text-text-primary">{formatSigned(ce?.gamma, 4)}</td>
                      <td className={clsx("py-2 pr-2 text-right", valueTone(ce?.theta))}>{formatSigned(ce?.theta, 2)}</td>
                      <td className="py-2 pr-2 text-right text-text-primary">{formatSigned(ce?.vega, 2)}</td>
                      <td className="py-2 pr-2 text-right font-semibold text-accent-green">{ce?.ltp?.toFixed(2) || "--"}</td>
                      <td className={clsx("py-2 pr-2 text-right", valueTone(ce?.ltp_change_pct))}>{formatSigned(ce?.ltp_change_pct, 2, "%")}</td>
                      <td className={clsx("py-2 px-3 text-center font-semibold", isAtm ? "text-accent-amber" : "text-text-primary")}>
                        {strike}
                      </td>
                      <td className="py-2 pl-2 text-left font-semibold text-accent-red">{pe?.ltp?.toFixed(2) || "--"}</td>
                      <td className={clsx("py-2 pl-2 text-left", valueTone(pe?.ltp_change_pct))}>{formatSigned(pe?.ltp_change_pct, 2, "%")}</td>
                      <td className="py-2 pl-2 text-left text-text-secondary">{formatIv(pe?.iv)}</td>
                      <td className="py-2 pl-2 text-left text-text-primary">{formatSigned(pe?.delta, 3)}</td>
                      <td className="py-2 pl-2 text-left text-text-primary">{formatSigned(pe?.gamma, 4)}</td>
                      <td className={clsx("py-2 pl-2 text-left", valueTone(pe?.theta))}>{formatSigned(pe?.theta, 2)}</td>
                      <td className="py-2 pl-2 text-left text-text-primary">{formatSigned(pe?.vega, 2)}</td>
                      <td className="py-2 pl-2 text-left text-text-secondary">{formatCompact(pe?.volume)}</td>
                      <td className={clsx("py-2 pl-2 text-left", valueTone(pe?.oi_change))}>{formatCompact(pe?.oi_change)}</td>
                      <td className="py-2 pl-2 text-left text-accent-red">{formatCompact(pe?.oi)}</td>
                    </tr>
                  );
                })}
                {!visibleStrikes.length && (
                  <tr>
                    <td colSpan={21} className="py-8 text-center text-sm text-text-muted">
                      {chain?.error || "No live option chain data available for the selected index."}
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </section>

        <div className="space-y-5">
          <section className="card rounded-2xl p-4">
            <div className="flex items-center gap-2 text-[11px] uppercase tracking-[0.14em] text-text-muted">
              <Radar size={14} className="text-accent-blue" />
              Chain Pulse
            </div>
            <div className="mt-3 grid gap-3 md:grid-cols-2">
              <PulseIndicatorCard
                label="PCR OI"
                value={chain?.pcr_oi?.toFixed(2) || "--"}
                detail={chain?.pcr_oi_change != null ? `vs prev day ${formatSigned(chain.pcr_oi_change, 2)}` : undefined}
                tone="text-accent-amber"
                directionValue={chain?.pcr_oi_change}
              />
              <PulseIndicatorCard label="PCR Volume" value={chain?.pcr_volume?.toFixed(2) || "--"} tone="text-accent-blue" />
              <PulseIndicatorCard label="ATM Strike" value={chain?.atm_strike ? `${chain.atm_strike}` : "--"} />
              <PulseIndicatorCard label="ATM IV" value={formatIv(chain?.atm_iv)} tone="text-accent-green" />
              <PulseIndicatorCard label="Max Pain" value={chain?.max_pain ? `${chain.max_pain}` : "--"} />
              <PulseIndicatorCard
                label="CE OI"
                value={formatCompact(chain?.total_ce_oi)}
                detail={chain?.total_ce_oi_change != null ? `vs prev day ${formatCompact(chain.total_ce_oi_change)}` : undefined}
                directionValue={chain?.total_ce_oi_change}
              />
              <PulseIndicatorCard
                label="PE OI"
                value={formatCompact(chain?.total_pe_oi)}
                detail={chain?.total_pe_oi_change != null ? `vs prev day ${formatCompact(chain.total_pe_oi_change)}` : undefined}
                directionValue={chain?.total_pe_oi_change}
              />
              <PulseIndicatorCard label="CE Volume" value={formatCompact(chain?.total_ce_volume)} />
              <PulseIndicatorCard label="PE Volume" value={formatCompact(chain?.total_pe_volume)} />
              <PulseIndicatorCard
                label="ATM CE"
                value={formatSigned(chain?.atm_call_ltp_change, 2)}
                detail={chain?.atm_call_ltp_change_pct != null ? `${formatSigned(chain.atm_call_ltp_change_pct, 2, "%")} vs prev close` : undefined}
                tone={valueTone(chain?.atm_call_ltp_change)}
                directionValue={chain?.atm_call_ltp_change}
              />
              <PulseIndicatorCard
                label="ATM PE"
                value={formatSigned(chain?.atm_put_ltp_change, 2)}
                detail={chain?.atm_put_ltp_change_pct != null ? `${formatSigned(chain.atm_put_ltp_change_pct, 2, "%")} vs prev close` : undefined}
                tone={valueTone(chain?.atm_put_ltp_change)}
                directionValue={chain?.atm_put_ltp_change}
              />
              <PulseIndicatorCard
                label="ATM CE OI"
                value={formatCompact(chain?.atm_call_oi_change)}
                detail="vs previous day open interest"
                directionValue={chain?.atm_call_oi_change}
              />
              <PulseIndicatorCard
                label="ATM PE OI"
                value={formatCompact(chain?.atm_put_oi_change)}
                detail="vs previous day open interest"
                directionValue={chain?.atm_put_oi_change}
              />
            </div>
          </section>

          <section className="card rounded-2xl p-4">
            <div className="flex items-center justify-between gap-2">
              <div className="flex items-center gap-2 text-[11px] uppercase tracking-[0.14em] text-text-muted">
                <BarChart3 size={14} className="text-accent-green" />
                Market Profile
              </div>
              <div className="flex items-center gap-2">
                {(["day", "week", "month"] as const).map((item) => (
                  <button
                    key={item}
                    onClick={() => setProfileTimeframe(item)}
                    className={clsx(
                      "rounded-lg border px-2.5 py-1 text-[11px] uppercase tracking-[0.08em]",
                      profileTimeframe === item
                        ? "border-accent-green bg-accent-green/12 text-accent-green"
                        : "border-bg-border bg-bg-secondary/45 text-text-muted"
                    )}
                  >
                    {item}
                  </button>
                ))}
              </div>
            </div>
            {profile && !profile.error ? (
              <div className="mt-3">
                <PulseRow label="POC" value={profile.poc?.toFixed(2) || "--"} tone="text-accent-amber" />
                <PulseRow label="VAH" value={profile.vah?.toFixed(2) || "--"} tone="text-accent-green" />
                <PulseRow label="VAL" value={profile.val?.toFixed(2) || "--"} tone="text-accent-red" />
                <PulseRow label="IB High" value={profile.ib_high?.toFixed(2) || "--"} />
                <PulseRow label="IB Low" value={profile.ib_low?.toFixed(2) || "--"} />
                <PulseRow label="Source" value={profile.source_interval?.toUpperCase() || "--"} />
                <PulseRow label="Samples" value={profile.sample_count ? `${profile.sample_count}` : "--"} />
                <PulseRow label="Coverage Start" value={profile.coverage_start ? new Date(profile.coverage_start).toLocaleString() : "--"} />
                <PulseRow label="Coverage End" value={profile.coverage_end ? new Date(profile.coverage_end).toLocaleString() : "--"} />
              </div>
            ) : (
              <div className="mt-4 rounded-xl border border-dashed border-bg-border px-3 py-6 text-sm text-text-muted">
                {profile?.error || "Waiting for enough live ticks to build market profile."}
              </div>
            )}
          </section>
        </div>
      </div>
    </div>
  );
}

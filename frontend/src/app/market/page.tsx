"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { clsx } from "clsx";
import { Activity, BarChart3, Radar, RefreshCw } from "lucide-react";

import { getMarketProfile, getOptionChain } from "@/lib/api";
import { MARKET_INDEX_SYMBOLS, type MarketIndexSymbol, getMarketIndexLabel } from "@/lib/marketSymbols";
import { useTickSymbol } from "@/store";

type ChainEntry = {
  strike: number;
  option_type: "CE" | "PE";
  ltp: number;
  oi: number;
  volume: number;
  iv?: number | null;
};

type OptionChainPayload = {
  symbol: string;
  expiry: string;
  spot_price: number;
  entries: ChainEntry[];
  pcr_oi: number;
  pcr_volume: number;
  max_pain: number;
  atm_strike: number;
  atm_iv: number;
  total_ce_oi: number;
  total_pe_oi: number;
  timestamp?: string;
  error?: string;
};

type MarketProfilePayload = {
  symbol: string;
  timeframe: "daily" | "hourly";
  date: string;
  poc: number;
  vah: number;
  val: number;
  ib_high: number;
  ib_low: number;
  error?: string;
};

function formatChangePct(ltp?: number, close?: number) {
  if (!ltp || !close) return "--";
  const pct = ((ltp - close) / close) * 100;
  const prefix = pct > 0 ? "+" : "";
  return `${prefix}${pct.toFixed(2)}%`;
}

function formatIv(value?: number | null) {
  if (value == null || Number.isNaN(value)) return "--";
  const normalized = value > 5 ? value : value * 100;
  return `${normalized.toFixed(1)}%`;
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

function StatRow({ label, value, tone = "text-text-primary" }: {
  label: string;
  value: string;
  tone?: string;
}) {
  return (
    <div className="flex items-center justify-between gap-3 border-b border-bg-border/50 py-2 text-sm last:border-b-0">
      <span className="text-text-muted">{label}</span>
      <span className={clsx("font-mono font-semibold", tone)}>{value}</span>
    </div>
  );
}

export default function MarketPage() {
  const [symbol, setSymbol] = useState<MarketIndexSymbol>("NSE:NIFTY50-INDEX");
  const [expiry, setExpiry] = useState("");
  const selectedTick = useTickSymbol(symbol);

  const chainQuery = useQuery<OptionChainPayload>({
    queryKey: ["optionChain", symbol, expiry],
    queryFn: () => getOptionChain(symbol, expiry || undefined).then((response) => response.data),
    refetchInterval: 15000,
    staleTime: 5000,
  });

  const profileQuery = useQuery<MarketProfilePayload>({
    queryKey: ["marketProfile", symbol],
    queryFn: () => getMarketProfile(symbol).then((response) => response.data),
    refetchInterval: 15000,
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
    <div className="mx-auto max-w-[1600px] space-y-5 pb-8">
      <section className="space-y-3">
        <div className="flex items-center justify-between gap-3">
          <div>
            <h1 className="font-mono text-2xl font-semibold text-text-primary">Market</h1>
            <div className="mt-1 text-sm text-text-muted">
              Live index board and option-chain context from the active feed.
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

      <div className="grid gap-5 xl:grid-cols-[1.35fr_0.85fr]">
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
              <input
                type="date"
                value={expiry}
                onChange={(event) => setExpiry(event.target.value)}
                className="terminal-input py-1.5 text-xs"
              />
              <button
                onClick={() => {
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
            <table className="w-full min-w-[760px] text-xs font-mono">
              <thead>
                <tr className="border-b border-bg-border text-text-muted">
                  <th className="pb-2 pr-2 text-right">CE OI</th>
                  <th className="pb-2 text-right">CE IV</th>
                  <th className="pb-2 text-right">CE LTP</th>
                  <th className="pb-2 px-3 text-center text-accent-amber">STRIKE</th>
                  <th className="pb-2 text-left">PE LTP</th>
                  <th className="pb-2 text-left">PE IV</th>
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
                      <td className="py-2 pr-2 text-right text-accent-green">{ce?.oi?.toLocaleString("en-IN") || "--"}</td>
                      <td className="py-2 text-right text-text-secondary">{formatIv(ce?.iv)}</td>
                      <td className="py-2 text-right font-semibold text-accent-green">{ce?.ltp?.toFixed(2) || "--"}</td>
                      <td className={clsx("py-2 px-3 text-center font-semibold", isAtm ? "text-accent-amber" : "text-text-primary")}>
                        {strike}
                      </td>
                      <td className="py-2 text-left font-semibold text-accent-red">{pe?.ltp?.toFixed(2) || "--"}</td>
                      <td className="py-2 text-left text-text-secondary">{formatIv(pe?.iv)}</td>
                      <td className="py-2 pl-2 text-left text-accent-red">{pe?.oi?.toLocaleString("en-IN") || "--"}</td>
                    </tr>
                  );
                })}
                {!visibleStrikes.length && (
                  <tr>
                    <td colSpan={7} className="py-8 text-center text-sm text-text-muted">
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
            <div className="mt-3">
              <StatRow label="PCR OI" value={chain?.pcr_oi?.toFixed(2) || "--"} tone="text-accent-amber" />
              <StatRow label="PCR Volume" value={chain?.pcr_volume?.toFixed(2) || "--"} tone="text-accent-blue" />
              <StatRow label="ATM Strike" value={chain?.atm_strike ? `${chain.atm_strike}` : "--"} />
              <StatRow label="ATM IV" value={formatIv(chain?.atm_iv)} tone="text-accent-green" />
              <StatRow label="Max Pain" value={chain?.max_pain ? `${chain.max_pain}` : "--"} />
              <StatRow label="CE OI" value={chain?.total_ce_oi?.toLocaleString("en-IN") || "--"} />
              <StatRow label="PE OI" value={chain?.total_pe_oi?.toLocaleString("en-IN") || "--"} />
            </div>
          </section>

          <section className="card rounded-2xl p-4">
            <div className="flex items-center gap-2 text-[11px] uppercase tracking-[0.14em] text-text-muted">
              <BarChart3 size={14} className="text-accent-green" />
              Market Profile
            </div>
            {profile && !profile.error ? (
              <div className="mt-3">
                <StatRow label="POC" value={profile.poc?.toFixed(2) || "--"} tone="text-accent-amber" />
                <StatRow label="VAH" value={profile.vah?.toFixed(2) || "--"} tone="text-accent-green" />
                <StatRow label="VAL" value={profile.val?.toFixed(2) || "--"} tone="text-accent-red" />
                <StatRow label="IB High" value={profile.ib_high?.toFixed(2) || "--"} />
                <StatRow label="IB Low" value={profile.ib_low?.toFixed(2) || "--"} />
                <StatRow label="Session Date" value={profile.date || "--"} />
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

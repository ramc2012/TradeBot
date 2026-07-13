"use client";

import { Fragment, useMemo, useState, useTransition } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Activity, BarChart3, BookOpen, ChevronDown, ChevronRight, Clock3, Gauge, Layers3, ListChecks, Play, ShieldCheck, Target, Waves } from "lucide-react";
import { CartesianGrid, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { LastUpdated } from "@/components/common/LastUpdated";
import { DeskShell, MetricTile, REFRESH_MS, Section, StatusBadge, formatMoney, formatNumber, useUrlTab } from "@/components/desk-ui";
import { CvdPanel, FootprintGrid, GateChips, OfSourceBadge, ProfileLadder } from "@/components/mpof";
import { MarketProfileChart } from "@/components/strategies/shared";
import { SignalQualityTab } from "@/components/strategies/overview/SignalQualityTab";
import { getCommodityInstitutionalConvergenceStatus, getInstitutionalConvergenceStatus, runCommodityInstitutionalConvergence, runInstitutionalConvergence } from "@/lib/api";

type Gates = Record<string, boolean>;
type Level = { price: number; buy: number; sell: number; buy_ratio: number; sell_ratio: number };
type Result = {
  symbol: string; kind: string; sector?: string; status: string; action: string; score?: number; spot?: number;
  futures_contract?: string; gates?: Gates; long_gates?: Gates; short_gates?: Gates; blocked_reasons?: string[];
  profile?: Record<string, any>; cvd?: { source?: string; series?: Array<{ time: string; cvd: number; close: number }>; divergence?: { kind: string; strength: number } };
  footprint?: { bars?: Array<{ time: string; delta: number; cumulative_delta: number; volume: number; levels: Level[] }>; long_ratio?: number; short_ratio?: number; tick_count?: number; source?: string };
  options?: { expiry?: string; call_wall?: number; put_wall?: number; top_call_walls?: Array<{ strike: number; oi: number }>; top_put_walls?: Array<{ strike: number; oi: number }> };
  risk?: { entry?: number; atr_3m?: number; stop?: number; stop_multiplier?: number; target1?: number; ib_midpoint?: number; target2_long?: number; target2_short?: number; lot_size?: number; risk_fraction?: number };
  vix?: { value?: number; size_multiplier?: number; stop_multiplier?: number }; clock_drift_ms?: number;
  bars?: Array<{ time: string; open: number; high: number; low: number; close: number; volume: number }>;
};
type Position = { position_id: string; symbol: string; direction: string; entry_price: number; current_price: number; stop: number; target1: number; target2?: number; lots: number; initial_lots: number; lot_size: number; target1_done: boolean; realized_pnl?: number; opened_at?: string; closed_at?: string; exit_reason?: string };
type Payload = {
  enabled?: boolean; mode?: string; market?: string; market_open?: boolean;
  universe?: { indices?: string[]; stocks?: Array<{ symbol: string; sector: string }>; stock_count?: number; sector_count?: number; cbe_scan_date?: string; roots?: string[]; contracts?: Record<string, { symbol?: string; expiry?: string }>; resolved_count?: number; unresolved?: string[] };
  latest?: { status?: string; generated_at?: string; actionable_count?: number; result_count?: number; results?: Result[]; gate_breakdown?: Record<string, number>; india_vix?: number; pre_market?: any };
  paper?: { initial_capital?: number; equity?: number; realized_pnl?: number; open_count?: number; closed_count?: number; open_positions?: Position[]; closed_positions?: Position[]; circuit_breaker?: { locked?: boolean; consecutive_losses?: number; day_pnl?: number; loss_limit?: number } };
};

const TABS = [
  { key: "overview", label: "Overview", icon: Activity }, { key: "profile", label: "Profile", icon: Layers3 },
  { key: "orderflow", label: "CVD & Footprint", icon: Waves }, { key: "risk", label: "Risk & Trades", icon: ShieldCheck },
  { key: "gates", label: "Gate audit", icon: ListChecks },
];

export default function InstitutionalConvergenceDesk() {
  const [activeTab, setActiveTab] = useUrlTab("overview");
  const [market, setMarket] = useState<"NSE" | "MCX">("NSE");
  const [symbol, setSymbol] = useState("NIFTY");
  const qc = useQueryClient();
  const [, startTransition] = useTransition();
  const query = useQuery({ queryKey: ["institutional-convergence", market, "status"], queryFn: async () => (await (market === "MCX" ? getCommodityInstitutionalConvergenceStatus() : getInstitutionalConvergenceStatus())).data as Payload, refetchInterval: REFRESH_MS.live });
  const run = useMutation({ mutationFn: market === "MCX" ? runCommodityInstitutionalConvergence : runInstitutionalConvergence, onSuccess: () => startTransition(() => void qc.invalidateQueries({ queryKey: ["institutional-convergence", market] })) });
  const data = query.data; const latest = data?.latest; const paper = data?.paper; const rows = latest?.results ?? [];
  const selected = rows.find((row) => row.symbol === symbol) ?? rows[0];
  const universe = useMemo(() => market === "MCX" ? (data?.universe?.roots ?? []) : [...(data?.universe?.indices ?? []), ...(data?.universe?.stocks ?? []).map((row) => row.symbol)], [data?.universe, market]);
  const circuit = paper?.circuit_breaker;
  const selectMarket = (next: "NSE" | "MCX") => { setMarket(next); setSymbol(next === "MCX" ? "GOLD" : "NIFTY"); };

  return <DeskShell title="Institutional Convergence" description={`Strict 3-minute ${market} profile · futures CVD · footprint imbalance · paper execution`} asOf={latest?.generated_at} isFetching={query.isFetching || run.isPending} paperMode tabs={TABS} activeTab={activeTab} onTabChange={setActiveTab}
    rightSlot={<div className="flex flex-wrap items-center gap-2"><div className="flex rounded-md border border-border bg-surface p-0.5">{(["NSE", "MCX"] as const).map((item) => <button key={item} onClick={() => selectMarket(item)} className={`rounded px-2 py-1 text-[10px] font-semibold ${market === item ? "bg-accent-blue text-white" : "text-text-muted"}`}>{item}</button>)}</div><select value={universe.includes(symbol) ? symbol : universe[0] ?? ""} onChange={(e) => setSymbol(e.target.value)} className="rounded-md border border-border bg-surface px-2 py-1.5 text-xs">{universe.map((item) => <option key={item}>{item}</option>)}</select><StatusBadge label={`${market} ${data?.market_open ? "open" : "closed"}`} variant={data?.market_open ? "success" : "neutral"}/><StatusBadge label={circuit?.locked ? "circuit locked" : "paper armed"} variant={circuit?.locked ? "error" : "warn"}/><button disabled={!data?.market_open || run.isPending} onClick={() => run.mutate()} className="inline-flex items-center gap-1 rounded-md border border-border px-3 py-1.5 text-xs disabled:opacity-40"><Play size={12}/> Run</button></div>}>

    {activeTab === "overview" ? <div className="space-y-4">
      <section className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-8">
        <MetricTile label="Universe" value={market === "MCX" ? String(data?.universe?.roots?.length ?? 0) : `${data?.universe?.indices?.length ?? 0}+${data?.universe?.stock_count ?? 0}`} detail={market === "MCX" ? `${data?.universe?.resolved_count ?? 0} active contracts` : `${data?.universe?.sector_count ?? 0} sectors`}/>
        <MetricTile label="Actionable" value={String(latest?.actionable_count ?? 0)} detail={`${latest?.result_count ?? 0} evaluated`}/>
        <MetricTile label={market === "MCX" ? "Volatility" : "India VIX"} value={market === "MCX" ? "ATR" : formatNumber(latest?.india_vix, 2)} detail={market === "MCX" ? "commodity-local sizing" : selected?.vix?.value && selected.vix.value > 22 ? "3× ATR stops" : selected?.vix?.value && selected.vix.value < 11 ? "half risk" : "normal band"}/>
        <MetricTile label="Clock drift" value={selected?.clock_drift_ms != null ? `${formatNumber(selected.clock_drift_ms, 0)} ms` : "—"} detail="≤1000 ms gate"/>
        <MetricTile label="Equity" value={formatMoney(paper?.equity)} detail={`initial ${formatMoney(paper?.initial_capital)}`}/>
        <MetricTile label="Day P&L" value={formatMoney(circuit?.day_pnl)} detail={`${circuit?.consecutive_losses ?? 0} losses`}/>
        <MetricTile label="Open" value={String(paper?.open_count ?? 0)} detail={`${paper?.closed_count ?? 0} closed`}/>
        <MetricTile label="Mode" value="PAPER" detail="live orders disabled"/>
      </section>
      {latest?.pre_market ? <Section title="08:45 pre-market preparation" icon={<Clock3 size={16}/>} description={market === "MCX" ? "Previous MCX profiles and active futures contracts loaded" : `India VIX ${formatNumber(latest.pre_market.india_vix, 2)} · previous value areas and OI walls loaded`}><div className="grid gap-2 md:grid-cols-3">{(latest.pre_market.instruments ?? []).map((row: any) => <div key={row.symbol} className="rounded-lg border border-border p-3"><div className="font-semibold">{row.symbol}</div><div className="mt-1 text-xs text-text-muted">{row.futures_contract}</div><div className="mt-2 flex gap-1"><StatusBadge label={row.data_ready ? "profile ready" : "missing profile"} variant={row.data_ready ? "success" : "warn"}/><StatusBadge label={row.options?.expiry ?? "no option wall"} variant="neutral"/></div></div>)}</div></Section> : null}
      <Section title="Lane matrix" icon={<Activity size={16}/>} description="Every instrument is independently gated; no relative score can override a failed operational rule." rightSlot={<LastUpdated timestamp={latest?.generated_at} label="scan"/>}><ResultMatrix rows={rows} selected={selected?.symbol} onSelect={setSymbol} generatedAt={latest?.generated_at}/></Section>
      <Section title="Blocked-gate census" icon={<Gauge size={16}/>}><div className="flex flex-wrap gap-2">{Object.entries(latest?.gate_breakdown ?? {}).map(([key, count]) => <StatusBadge key={key} label={`${key} · ${count}`} variant="warn"/>)}</div></Section>
    </div> : null}

    {activeTab === "profile" ? <div className="space-y-4">
      <section className="grid grid-cols-2 gap-3 md:grid-cols-6"><MetricTile label="Spot" value={formatNumber(selected?.spot, 2)}/><MetricTile label="Prior VAH" value={formatNumber(selected?.profile?.prior?.vah, 2)}/><MetricTile label="Prior VAL" value={formatNumber(selected?.profile?.prior?.val, 2)}/><MetricTile label="Prior POC" value={formatNumber(selected?.profile?.prior?.poc, 2)}/><MetricTile label="IB midpoint" value={formatNumber(selected?.risk?.ib_midpoint, 2)}/><MetricTile label="ATR 3m" value={formatNumber(selected?.risk?.atr_3m, 2)}/></section>
      <div className="grid gap-4 xl:grid-cols-[1.1fr_.9fr]">
        <Section title="Current TPO profile" icon={<BarChart3 size={16}/>} rightSlot={<LastUpdated timestamp={latest?.generated_at} label="scan"/>}><MarketProfileChart profile={selected?.profile} lastPrice={selected?.spot} height={420}/></Section>
        <Section title="Level ladder · spot vs value" icon={<Target size={16}/>} description="Today's VA/POC/IB, prior-session ghosts, HVN dots and live spot on one axis">
          <ProfileLadder
            spot={selected?.spot}
            vah={selected?.profile?.vah} val={selected?.profile?.val} poc={selected?.profile?.poc}
            ibHigh={selected?.profile?.initial_balance_high} ibLow={selected?.profile?.initial_balance_low}
            dayHigh={selected?.profile?.high_price} dayLow={selected?.profile?.low_price}
            prior={selected?.profile?.prior} hvnPrices={selected?.profile?.hvn_prices} singlePrints={selected?.profile?.single_prints}
            height={340} digits={2}
          />
          <div className="mt-3 space-y-1.5">{(selected?.profile?.hvn_prices ?? []).map((price: number) => <div key={price} className="flex items-center justify-between rounded-lg border border-border px-3 py-1.5 text-xs"><span>High-volume node</span><span className="font-mono text-accent-amber">{formatNumber(price, 2)}</span></div>)}</div>
        </Section>
      </div>
      <Section title="Three-minute price path" icon={<Activity size={16}/>}><PriceChart result={selected}/></Section>
    </div> : null}

    {activeTab === "orderflow" ? <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2"><OfSourceBadge source={selected?.cvd?.source}/><LastUpdated timestamp={latest?.generated_at} label="scan"/></div>
      <section className="grid grid-cols-2 gap-3 md:grid-cols-6"><MetricTile label="CVD source" value={selected?.cvd?.source ?? "—"}/><MetricTile label="Divergence" value={selected?.cvd?.divergence?.kind ?? "none"}/><MetricTile label="Strength" value={formatNumber((selected?.cvd?.divergence?.strength ?? 0) * 100, 1)} detail="percent"/><MetricTile label="Ticks" value={String(selected?.footprint?.tick_count ?? 0)}/><MetricTile label="Buy ratio" value={`${formatNumber(selected?.footprint?.long_ratio, 2)}×`}/><MetricTile label="Sell ratio" value={`${formatNumber(selected?.footprint?.short_ratio, 2)}×`}/></section>
      <Section title="Price versus cumulative volume delta" icon={<Waves size={16}/>}><CvdPanel series={selected?.cvd?.series} source={selected?.cvd?.source} divergence={selected?.cvd?.divergence} height={300}/></Section>
      <div className="grid gap-4 xl:grid-cols-[1.15fr_.85fr]"><Section title="Recent 3-minute footprints" icon={<BookOpen size={16}/>} description="Executed volume reconstructed at price; ≥3× imbalance is highlighted."><FootprintGrid bars={selected?.footprint?.bars} source={selected?.footprint?.source} maxBars={4}/></Section><Section title="Option OI walls" icon={<Layers3 size={16}/>}><WallPanel result={selected}/></Section></div>
      <div className="grid gap-4 lg:grid-cols-2">
        <Section title="Long sequence" icon={<ListChecks size={16}/>}><GateChips gates={selected?.long_gates} blockedReasons={selected?.action !== "SHORT" ? selected?.blocked_reasons : undefined}/></Section>
        <Section title="Short sequence" icon={<ListChecks size={16}/>}><GateChips gates={selected?.short_gates} blockedReasons={selected?.action === "SHORT" ? selected?.blocked_reasons : undefined}/></Section>
      </div>
    </div> : null}

    {activeTab === "risk" ? <div className="space-y-4">
      <section className="grid grid-cols-2 gap-3 md:grid-cols-6"><MetricTile label="Entry" value={formatNumber(selected?.risk?.entry, 2)}/><MetricTile label="Stop" value={formatNumber(selected?.risk?.stop, 2)} detail={`${selected?.risk?.stop_multiplier ?? 2}× ATR`}/><MetricTile label="Target 1" value={formatNumber(selected?.risk?.target1, 2)} detail="50%, then BE"/><MetricTile label="IB midpoint" value={formatNumber(selected?.risk?.ib_midpoint, 2)}/><MetricTile label="Target 2" value={formatNumber(selected?.action === "SHORT" ? selected?.risk?.target2_short : selected?.risk?.target2_long, 2)} detail="OI wall"/><MetricTile label="Risk" value={`${formatNumber((selected?.risk?.risk_fraction ?? 0) * 100, 2)}%`} detail={`lot ${selected?.risk?.lot_size ?? "—"}`}/></section>
      <Section title="Daily circuit breaker" icon={<ShieldCheck size={16}/>}><div className="flex flex-wrap gap-2"><StatusBadge label={circuit?.locked ? "LOCKED" : "ARMED"} variant={circuit?.locked ? "error" : "success"}/><StatusBadge label={`${circuit?.consecutive_losses ?? 0}/2 consecutive losses`} variant="neutral"/><StatusBadge label={`day ${formatMoney(circuit?.day_pnl)}`} variant="neutral"/><StatusBadge label={`limit ${formatMoney(circuit?.loss_limit)}`} variant="neutral"/></div></Section>
      <Section title="Open paper positions" icon={<Target size={16}/>}><PositionTable rows={paper?.open_positions ?? []}/></Section>
      <Section title="Closed trade journal" icon={<ListChecks size={16}/>}><PositionTable rows={paper?.closed_positions ?? []}/></Section>
    </div> : null}
    {activeTab === "gates" ? <SignalQualityTab laneKeys={[market === "MCX" ? "institutional_convergence_commodity" : "institutional_convergence"]} title={`${market} Institutional Convergence validation`}/> : null}
  </DeskShell>;
}

function ResultMatrix({ rows, selected, onSelect, generatedAt }: { rows: Result[]; selected?: string; onSelect: (s: string) => void; generatedAt?: string }) {
  const [expanded, setExpanded] = useState<string | null>(null);
  return <div className="overflow-x-auto"><table className="w-full min-w-[850px] text-left text-xs"><thead className="text-text-muted"><tr><th className="w-6"/><th>Symbol</th><th>Sector</th><th>Contract</th><th>Action</th><th>Score</th><th>OF source</th><th>Blocked</th></tr></thead><tbody className="divide-y divide-border">{rows.map((row) => <Fragment key={row.symbol}>
    <tr onClick={() => onSelect(row.symbol)} className={`cursor-pointer ${selected === row.symbol ? "bg-accent-blue/5" : ""}`}>
      <td className="py-3 pr-1"><button type="button" aria-label={`${expanded === row.symbol ? "Collapse" : "Expand"} ${row.symbol}`} onClick={(e) => { e.stopPropagation(); setExpanded((cur) => cur === row.symbol ? null : row.symbol); }} className="rounded p-0.5 text-text-muted hover:text-text-primary">{expanded === row.symbol ? <ChevronDown size={14}/> : <ChevronRight size={14}/>}</button></td>
      <td className="py-3 font-semibold">{row.symbol}</td><td>{row.sector}</td><td className="font-mono text-[10px]">{row.futures_contract}</td>
      <td><StatusBadge label={row.action} variant={row.action === "LONG" ? "success" : row.action === "SHORT" ? "error" : "neutral"}/></td>
      <td>{formatNumber(row.score, 1)}</td>
      <td><OfSourceBadge source={row.cvd?.source} size="sm"/></td>
      <td>{(row.blocked_reasons ?? []).slice(0, 3).join(" · ")}</td>
    </tr>
    {expanded === row.symbol ? <tr className="bg-bg-secondary/20"><td colSpan={8} className="p-3">
      <div className="mb-2 flex flex-wrap items-center gap-2"><OfSourceBadge source={row.cvd?.source} size="sm"/><LastUpdated timestamp={row.footprint?.bars?.at(-1)?.time ?? generatedAt} label="last bar"/></div>
      <div className="grid gap-3 xl:grid-cols-[250px_minmax(0,1fr)]">
        <ProfileLadder spot={row.spot} vah={row.profile?.vah} val={row.profile?.val} poc={row.profile?.poc} ibHigh={row.profile?.initial_balance_high} ibLow={row.profile?.initial_balance_low} prior={row.profile?.prior} hvnPrices={row.profile?.hvn_prices} singlePrints={row.profile?.single_prints} height={230} digits={2}/>
        <div className="space-y-3">
          <CvdPanel series={row.cvd?.series} source={row.cvd?.source} divergence={row.cvd?.divergence} height={160} hideHeader/>
          <div className="grid gap-3 md:grid-cols-2">
            <GateChips title="Long gates" gates={row.long_gates} blockedReasons={row.action !== "SHORT" ? row.blocked_reasons : undefined}/>
            <GateChips title="Short gates" gates={row.short_gates} blockedReasons={row.action === "SHORT" ? row.blocked_reasons : undefined}/>
          </div>
        </div>
      </div>
    </td></tr> : null}
  </Fragment>)}</tbody></table></div>;
}
function PriceChart({ result }: { result?: Result }) { const rows = result?.bars ?? []; return <div className="h-[280px]"><ResponsiveContainer><LineChart data={rows}><CartesianGrid strokeDasharray="3 3" opacity={.2}/><XAxis dataKey="time" tickFormatter={(v) => String(v).slice(11,16)} minTickGap={25}/><YAxis domain={["auto","auto"]}/><Tooltip/><Line dataKey="close" stroke="#60a5fa" dot={false}/>{result?.profile?.prior?.vah ? <ReferenceLine y={result.profile.prior.vah} stroke="#ef4444" strokeDasharray="4 3" label="VAH"/> : null}{result?.profile?.prior?.val ? <ReferenceLine y={result.profile.prior.val} stroke="#22c55e" strokeDasharray="4 3" label="VAL"/> : null}</LineChart></ResponsiveContainer></div>; }
function WallPanel({ result }: { result?: Result }) { return <div className="space-y-4"><div><div className="mb-2 text-xs font-semibold text-accent-red">Call resistance</div>{(result?.options?.top_call_walls ?? []).map((w) => <div key={w.strike} className="mb-1 flex justify-between rounded border border-border px-3 py-2"><span>{formatNumber(w.strike,0)}</span><span>{formatNumber(w.oi,0)} OI</span></div>)}</div><div><div className="mb-2 text-xs font-semibold text-accent-green">Put support</div>{(result?.options?.top_put_walls ?? []).map((w) => <div key={w.strike} className="mb-1 flex justify-between rounded border border-border px-3 py-2"><span>{formatNumber(w.strike,0)}</span><span>{formatNumber(w.oi,0)} OI</span></div>)}</div></div>; }
function PositionTable({ rows }: { rows: Position[] }) { if (!rows.length) return <div className="py-8 text-center text-sm text-text-muted">No positions.</div>; return <div className="overflow-x-auto"><table className="w-full min-w-[850px] text-xs"><thead className="text-left text-text-muted"><tr><th>Symbol</th><th>Side</th><th>Entry</th><th>Current/Exit</th><th>Stop</th><th>T1</th><th>T2</th><th>Lots</th><th>P&L / reason</th></tr></thead><tbody>{rows.map((p) => <tr key={p.position_id} className="border-t border-border"><td className="py-3 font-semibold">{p.symbol}</td><td>{p.direction}</td><td>{formatNumber(p.entry_price,2)}</td><td>{formatNumber(p.current_price,2)}</td><td>{formatNumber(p.stop,2)}</td><td>{formatNumber(p.target1,2)}</td><td>{formatNumber(p.target2,2)}</td><td>{p.lots}/{p.initial_lots}</td><td>{formatMoney(p.realized_pnl)} {p.exit_reason}</td></tr>)}</tbody></table></div>; }

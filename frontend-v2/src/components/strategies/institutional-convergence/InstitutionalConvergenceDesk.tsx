"use client";

import { Fragment, useMemo, useState, useTransition } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "next/navigation";
import { Activity, BarChart3, BookOpen, ChevronDown, ChevronRight, Clock3, Gauge, Layers3, ListChecks, Play, ShieldCheck, Target, Waves } from "lucide-react";
import { CartesianGrid, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { LastUpdated } from "@/components/common/LastUpdated";
import { DataModeBadge, DeskShell, MetricTile, ProvenanceChip, REFRESH_MS, Section, StatusBadge, SufficiencyBadge, formatIST, formatISTTime, formatMoney, formatNumber, useUrlTab } from "@/components/desk-ui";
import { useSystemState } from "@/hooks/useSystemState";
import { classifySourceGrade, describeImbalance, imbalanceOf, liveVerdict, provenanceOf, rrRender, rrTile } from "@/lib/market-semantics";
import { CvdPanel, FootprintGrid, GateChips, LiveOrderFlowTape, OfSourceBadge, OrderFlowPulse, ProfileLadder } from "@/components/mpof";
import { MarketProfileChart } from "@/components/strategies/shared";
import { SignalQualityTab } from "@/components/strategies/overview/SignalQualityTab";
import { describeApiError, getCommodityInstitutionalConvergenceStatus, getInstitutionalConvergenceDetail, getInstitutionalConvergenceStatus, runCommodityInstitutionalConvergence, runInstitutionalConvergence } from "@/lib/api";
import { underlyingToTapeSymbol } from "@/lib/marketSymbols";

import {
  type ConvergenceTrade,
  OpenPositionsPanel,
  OrderLogTimeline,
  StatisticsCards,
  TradeBook,
  deriveOrders,
  deriveStatistics,
  normalizeTrade,
  useConvergenceExecution,
} from "./ExecutionPanels";

type Gates = Record<string, boolean>;
// buy_ratio / sell_ratio are UNBOUNDED backend artefacts (observed up to
// 4,006,145 when the opposing side is empty). Typed nullable and never rendered
// as a magnitude — they only feed `imbalanceOf`.
type Level = { price: number; buy: number; sell: number; buy_ratio?: number | null; sell_ratio?: number | null };
type Result = {
  symbol: string; kind: string; sector?: string; status: string; action: string; score?: number; spot?: number;
  futures_contract?: string; gates?: Gates; readiness_gates?: Gates; long_gates?: Gates; short_gates?: Gates; long_confirmations?: Gates; short_confirmations?: Gates; blocked_reasons?: string[];
  preferred_direction?: string; setup_state?: string; quality?: string; confirmation_count?: number; confirmation_required?: number;
  long_setup?: { state?: string; event?: string; level?: number; bar_time?: string; age_bars?: number }; short_setup?: { state?: string; event?: string; level?: number; bar_time?: string; age_bars?: number };
  profile?: Record<string, any>; cvd?: { source?: string; series?: Array<{ time: string; cvd: number; close: number }>; divergence?: { kind: string; strength: number } | null; last_bar_time?: string | null };
  footprint?: { bars?: Array<{ time: string; delta: number; cumulative_delta: number; volume: number; levels: Level[] }>; long_ratio?: number; short_ratio?: number; tick_count?: number; source?: string; last_bar_time?: string | null };
  options?: { expiry?: string; call_wall?: number; put_wall?: number; top_call_walls?: Array<{ strike: number; oi: number }>; top_put_walls?: Array<{ strike: number; oi: number }> };
  risk?: { entry?: number; atr_3m?: number; stop?: number; target1?: number; ib_midpoint?: number; target2_long?: number; target2_short?: number; reward_risk?: number; lot_size?: number; risk_fraction?: number };
  vix?: { value?: number; size_multiplier?: number }; clock_drift_ms?: number; tick_age_ms?: number; tick_freshness_limit_ms?: number;
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
  // `?market=` / `?symbol=` SEED the pickers so the market-structure workspace can
  // deep-link into this desk carrying its pin. Behaviour is otherwise unchanged:
  // absent params keep the original defaults, and the pickers stay authoritative.
  const seedParams = useSearchParams();
  const [market, setMarket] = useState<"NSE" | "MCX">(seedParams?.get("market") === "MCX" ? "MCX" : "NSE");
  const [symbol, setSymbol] = useState((seedParams?.get("symbol") || "NIFTY").toUpperCase());
  const qc = useQueryClient();
  const [, startTransition] = useTransition();
  const query = useQuery({ queryKey: ["institutional-convergence", market, "status"], queryFn: async () => (await (market === "MCX" ? getCommodityInstitutionalConvergenceStatus() : getInstitutionalConvergenceStatus())).data as Payload, refetchInterval: REFRESH_MS.live });
  const run = useMutation({ mutationFn: market === "MCX" ? runCommodityInstitutionalConvergence : runInstitutionalConvergence, onSuccess: () => startTransition(() => void qc.invalidateQueries({ queryKey: ["institutional-convergence", market] })) });
  const data = query.data; const latest = data?.latest; const paper = data?.paper; const rows = latest?.results ?? [];
  const selected = rows.find((row) => row.symbol === symbol) ?? rows[0];
  // The compact /status omits heavy per-instrument detail (full bars, footprint
  // levels, TPO profile, CVD series). Fetch that detail for the ONE selected
  // instrument and merge it over the compact row so the profile / order-flow /
  // risk tabs and the expanded matrix row render exactly as before.
  const detailQuery = useQuery({
    queryKey: ["institutional-convergence", market, "detail", selected?.symbol],
    queryFn: async () => (await getInstitutionalConvergenceDetail(selected!.symbol, market)).data as { symbol?: string; result?: Result | null },
    enabled: !!selected?.symbol,
    refetchInterval: REFRESH_MS.live,
  });
  const detail = detailQuery.data?.result ?? undefined;
  const selectedFull = useMemo<Result | undefined>(
    () => (selected ? { ...selected, ...(detail && detail.symbol === selected.symbol ? detail : {}) } : undefined),
    [selected, detail],
  );
  const universe = useMemo(() => {
    const evaluated = rows.map((row) => row.symbol).filter(Boolean);
    if (evaluated.length) return evaluated;
    return market === "MCX"
      ? (data?.universe?.roots ?? [])
      : [...(data?.universe?.indices ?? []), ...(data?.universe?.stocks ?? []).map((row) => row.symbol)];
  }, [data?.universe, market, rows]);
  const circuit = paper?.circuit_breaker;
  // Volume-at-price for the ladder's volume-profile overlay, reconstructed
  // from the footprint levels (buy+sell executed volume per price).
  const volumeByPrice = useMemo(() => {
    const acc = new Map<number, number>();
    for (const bar of selectedFull?.footprint?.bars ?? []) {
      for (const level of bar.levels ?? []) {
        const price = Number(level.price);
        const volume = (Number(level.buy) || 0) + (Number(level.sell) || 0);
        if (Number.isFinite(price) && volume > 0) acc.set(price, (acc.get(price) ?? 0) + volume);
      }
    }
    return Array.from(acc.entries()).map(([price, volume]) => ({ price, volume }));
  }, [selectedFull?.footprint?.bars]);
  const selectMarket = (next: "NSE" | "MCX") => { setMarket(next); setSymbol(next === "MCX" ? "GOLD" : "NIFTY"); };

  // ── Semantic contract (Phase 0a) ──────────────────────────────────────────
  // This desk previously derived its session state from the BACKEND's
  // `market_open` boolean and carried no data-mode or freshness honesty at all,
  // even though its own rows report cvd/footprint source and tick age against
  // an adaptive limit. All of that now goes through the shared contract, so
  // this desk reads identically to Auction / MP / Orderflow.
  const system = useSystemState();
  const sessionOpen = market === "MCX" ? system.mcxOpen : system.nseOpen;
  const flowSource = selectedFull?.footprint?.source ?? selectedFull?.cvd?.source ?? null;
  const tickAgeMs = selectedFull?.tick_age_ms ?? null;
  const tickLimitMs = selectedFull?.tick_freshness_limit_ms ?? null;
  const tickWithinLimit =
    tickAgeMs != null && tickLimitMs != null ? tickAgeMs <= tickLimitMs : null;
  const lastBarTime =
    selectedFull?.footprint?.bars?.at(-1)?.time ??
    selectedFull?.footprint?.last_bar_time ??
    selectedFull?.cvd?.last_bar_time ??
    latest?.generated_at ??
    null;
  const flowProvenance = useMemo(
    () =>
      provenanceOf({
        source: flowSource,
        // CVD + footprint sides are inferred from the quote stream, so this
        // chip must never grade `observed` (2026-07-19 honesty correction).
        feature: "flow_attribution",
        asOf: lastBarTime,
        timeframe: "3m",
        have: selectedFull?.footprint?.bars?.length ?? null,
        completenessLabel:
          selectedFull?.footprint?.tick_count != null
            ? `${selectedFull.footprint.tick_count} ticks`
            : null,
        // Session closed ⇒ these numbers describe the LAST session, not now.
        dataMode: sessionOpen
          ? classifySourceGrade(flowSource) === "bar_inferred"
            ? "bar_inference"
            : "live"
          : "historical_replay",
        tickAgeMs,
        tickLimitMs,
        blockedReasons: selectedFull?.blocked_reasons ?? null,
      }),
    [flowSource, lastBarTime, selectedFull?.footprint?.bars?.length, selectedFull?.footprint?.tick_count, selectedFull?.blocked_reasons, sessionOpen, tickAgeMs, tickLimitMs],
  );
  const verdict = liveVerdict({
    sessionOpen,
    feedOnline: system.feedOnline,
    dataMode: flowProvenance.dataMode,
    freshness: flowProvenance.freshness,
    hasSymbolObservation: !!flowSource && tickWithinLimit !== false,
  });
  // R/R verdict for the selected row — computed ONCE, consumed by the risk
  // strip and by the gate audit so a phantom ratio can't pass in one place and
  // be suppressed in the other.
  // Structure panels (TPO profile / ladder / price path) are scan-generated,
  // not tick-generated — their provenance is the scan, at its own cadence.
  const profileProvenance = useMemo(
    () =>
      provenanceOf({
        source: selectedFull?.profile ? "market_profile_scan" : null,
        asOf: latest?.generated_at,
        timeframe: "3m TPO",
        have: selectedFull?.bars?.length ?? null,
        completenessLabel: selectedFull?.bars?.length ? `${selectedFull.bars.length} bars` : "no bars",
        dataMode: sessionOpen ? "live" : "historical_replay",
      }),
    [selectedFull?.profile, selectedFull?.bars?.length, latest?.generated_at, sessionOpen],
  );
  const rr = rrRender(selected?.risk);
  const rrStrip = rrTile(selected?.risk);

  return <DeskShell title="Institutional Convergence" description={`Staged 3-minute ${market} setup · 2-of-3 flow confirmation (CVD + footprint sides inferred from quotes, no aggressor tape) · anti-chase paper execution`} asOf={latest?.generated_at} isFetching={query.isFetching || run.isPending} paperMode tabs={TABS} activeTab={activeTab} onTabChange={setActiveTab}
    rightSlot={<div className="flex flex-wrap items-center gap-2"><div className="flex rounded-md border border-border bg-surface p-0.5">{(["NSE", "MCX"] as const).map((item) => <button key={item} onClick={() => selectMarket(item)} className={`rounded px-2 py-1 text-[10px] font-semibold ${market === item ? "bg-accent-blue text-white" : "text-text-muted"}`}>{item}</button>)}</div><select value={universe.includes(symbol) ? symbol : universe[0] ?? ""} onChange={(e) => setSymbol(e.target.value)} className="rounded-md border border-border bg-surface px-2 py-1.5 text-xs">{universe.map((item) => <option key={item}>{item}</option>)}</select><StatusBadge label={`${market} ${sessionOpen ? "open" : "closed"}`} variant={sessionOpen ? "success" : "neutral"} className={data?.market_open !== sessionOpen ? "border-accent-amber/50" : undefined}/><StatusBadge label={verdict.label} variant={verdict.variant}/><DataModeBadge mode={flowProvenance.dataMode} title={`${verdict.reason} · flow source ${flowSource ?? "not reported"}${tickAgeMs != null ? ` · tick ${Math.round(tickAgeMs / 1000)}s` : ""}`}/><StatusBadge label={circuit?.locked ? "circuit locked" : "paper armed"} variant={circuit?.locked ? "error" : "warn"}/><button disabled={!data?.market_open || run.isPending} onClick={() => run.mutate()} className="inline-flex items-center gap-1 rounded-md border border-border px-3 py-1.5 text-xs disabled:opacity-40"><Play size={12}/> Run</button></div>}>

    {query.isLoading && !data ? <Section title={`Loading ${market} Institutional Convergence`} icon={<Activity size={16}/>}><div className="py-8 text-center text-sm text-text-muted">Fetching the latest evaluated instruments and market profile data…</div></Section> : null}
    {query.isError && !data ? <Section title={`${market} Institutional Convergence unavailable`} icon={<Activity size={16}/>}><div className="py-8 text-center text-sm text-accent-red">{describeApiError(query.error, "The lane status could not be loaded.")}</div></Section> : null}

    {data && activeTab === "overview" ? <div className="space-y-4">
      <section className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-8">
        <MetricTile label="Universe" value={market === "MCX" ? String(data?.universe?.roots?.length ?? 0) : `${data?.universe?.indices?.length ?? 0}+${data?.universe?.stock_count ?? 0}`} detail={market === "MCX" ? `${data?.universe?.resolved_count ?? 0} active contracts` : `${data?.universe?.sector_count ?? 0} sectors`}/>
        <MetricTile label="Actionable" value={String(latest?.actionable_count ?? 0)} detail={`${latest?.result_count ?? 0} evaluated`}/>
        <MetricTile label={market === "MCX" ? "Volatility" : "India VIX"} value={market === "MCX" ? "ATR" : formatNumber(latest?.india_vix, 2)} detail={market === "MCX" ? "commodity-local sizing" : selected?.vix?.value && selected.vix.value < 11 ? "risk size halved" : "normal risk band"}/>
        <MetricTile label="Tick age" value={selected?.tick_age_ms != null ? `${formatNumber(selected.tick_age_ms / 1000, 1)}s` : "—"} detail={selected?.tick_freshness_limit_ms != null ? `adaptive limit ${formatNumber(selected.tick_freshness_limit_ms / 1000, 0)}s` : "adaptive freshness"}/>
        <MetricTile label="Equity" value={formatMoney(paper?.equity)} detail={`initial ${formatMoney(paper?.initial_capital)}`}/>
        <MetricTile label="Day P&L" value={formatMoney(circuit?.day_pnl)} detail={`${circuit?.consecutive_losses ?? 0} losses`}/>
        <MetricTile label="Open" value={String(paper?.open_count ?? 0)} detail={`${paper?.closed_count ?? 0} closed`}/>
        <MetricTile label="Mode" value="PAPER" detail="live orders disabled"/>
      </section>
      {latest?.pre_market ? <Section title="08:45 pre-market preparation" icon={<Clock3 size={16}/>} description={market === "MCX" ? "Previous MCX profiles and active futures contracts loaded" : `India VIX ${formatNumber(latest.pre_market.india_vix, 2)} · previous value areas and OI walls loaded`}><div className="grid gap-2 md:grid-cols-3">{(latest.pre_market.instruments ?? []).map((row: any) => <div key={row.symbol} className="rounded-lg border border-border p-3"><div className="font-semibold">{row.symbol}</div><div className="mt-1 text-xs text-text-muted">{row.futures_contract}</div><div className="mt-2 flex gap-1"><StatusBadge label={row.data_ready ? "profile ready" : "missing profile"} variant={row.data_ready ? "success" : "warn"}/><StatusBadge label={row.options?.expiry ?? "no option wall"} variant="neutral"/></div></div>)}</div></Section> : null}
      <Section title="Lane matrix" icon={<Activity size={16}/>} description="Structure stays armed for five bars; any two of CVD, footprint and price reclaim can confirm, while safety and anti-chase rules remain mandatory." rightSlot={<LastUpdated timestamp={latest?.generated_at} label="scan"/>}><ResultMatrix rows={rows} detail={selectedFull} selected={selected?.symbol} onSelect={setSymbol} generatedAt={latest?.generated_at}/></Section>
      <Section title="Blocked-gate census" icon={<Gauge size={16}/>}><div className="flex flex-wrap gap-2">{Object.entries(latest?.gate_breakdown ?? {}).map(([key, count]) => <StatusBadge key={key} label={`${key} · ${count}`} variant="warn"/>)}</div></Section>
    </div> : null}

    {data && activeTab === "profile" ? <div className="space-y-4">
      <section className="grid grid-cols-2 gap-3 md:grid-cols-6"><MetricTile label="Spot" value={formatNumber(selectedFull?.spot, 2)}/><MetricTile label="Prior VAH" value={formatNumber(selectedFull?.profile?.prior?.vah, 2)}/><MetricTile label="Prior VAL" value={formatNumber(selectedFull?.profile?.prior?.val, 2)}/><MetricTile label="Prior POC" value={formatNumber(selectedFull?.profile?.prior?.poc, 2)}/><MetricTile label="IB midpoint" value={formatNumber(selectedFull?.risk?.ib_midpoint, 2)}/><MetricTile label="ATR 3m" value={formatNumber(selectedFull?.risk?.atr_3m, 2)}/></section>
      <div className="grid gap-4 xl:grid-cols-[1.1fr_.9fr]">
        <Section title="Current TPO profile" icon={<BarChart3 size={16}/>} rightSlot={<LastUpdated timestamp={latest?.generated_at} label="scan"/>} provenance={profileProvenance}><MarketProfileChart profile={selectedFull?.profile} lastPrice={selectedFull?.spot} height={420}/></Section>
        <Section title="Level ladder · spot vs value" icon={<Target size={16}/>} description="Today's VA/POC/IB, prior-session ghosts, HVN dots and live spot on one axis" provenance={profileProvenance}>
          <ProfileLadder
            spot={selectedFull?.spot}
            vah={selectedFull?.profile?.vah} val={selectedFull?.profile?.val} poc={selectedFull?.profile?.poc}
            ibHigh={selectedFull?.profile?.initial_balance_high} ibLow={selectedFull?.profile?.initial_balance_low}
            dayHigh={selectedFull?.profile?.high_price} dayLow={selectedFull?.profile?.low_price}
            prior={selectedFull?.profile?.prior} hvnPrices={selectedFull?.profile?.hvn_prices} singlePrints={selectedFull?.profile?.single_prints}
            tpoCounts={selectedFull?.profile?.tpo_counts} tpoLetters={selectedFull?.profile?.tpo_letters}
            volumeByPrice={volumeByPrice}
            expandTitle={`${selectedFull?.symbol ?? ""} · level ladder`}
            height={340} digits={2}
          />
          <div className="mt-3 space-y-1.5">{(selectedFull?.profile?.hvn_prices ?? []).map((price: number) => <div key={price} className="flex items-center justify-between rounded-lg border border-border px-3 py-1.5 text-xs"><span>High-volume node</span><span className="font-mono text-accent-amber">{formatNumber(price, 2)}</span></div>)}</div>
        </Section>
      </div>
      <Section title="Three-minute price path" icon={<Activity size={16}/>} provenance={profileProvenance}><PriceChart result={selectedFull}/></Section>
    </div> : null}

    {data && activeTab === "orderflow" ? <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2"><OfSourceBadge source={selectedFull?.cvd?.source}/><StatusBadge label={selectedFull?.setup_state ?? "WATCHING"} variant={selectedFull?.setup_state === "CONFIRMED" ? "success" : selectedFull?.setup_state === "MISSED_NO_CHASE" ? "warn" : "neutral"}/><ProvenanceChip provenance={flowProvenance}/><LastUpdated timestamp={latest?.generated_at} label="scan"/></div>
      <ProvenanceChip provenance={flowProvenance} density="caption"/>
      <LiveOrderFlowTape symbol={(selectedFull?.futures_contract?.includes(":") || selectedFull?.futures_contract?.includes("|")) ? selectedFull.futures_contract : underlyingToTapeSymbol(selectedFull?.symbol) ?? selectedFull?.symbol} title={`${selectedFull?.symbol ?? market} · quote tape (sides inferred)`} />
      <section className="grid grid-cols-2 gap-3 md:grid-cols-6"><MetricTile label="Stage" value={selectedFull?.setup_state ?? "LEGACY"} detail={selectedFull?.preferred_direction ? `${selectedFull.preferred_direction} candidate` : "awaiting next staged scan"}/><MetricTile label="Confirmations" value={selectedFull?.confirmation_count != null ? `${selectedFull.confirmation_count}/${selectedFull.confirmation_required ?? 2}` : "—"} detail={selectedFull?.quality ?? "legacy snapshot"}/><MetricTile label="CVD" value={selectedFull?.cvd?.divergence?.kind ?? "impulse/none"} detail={selectedFull?.cvd?.source ?? "—"}/><MetricTile label="Ticks" value={String(selectedFull?.footprint?.tick_count ?? 0)}/><FootprintImbalanceTiles footprint={selectedFull?.footprint}/></section>
      <Section title="Price versus cumulative volume delta" icon={<Waves size={16}/>} provenance={flowProvenance}><CvdPanel series={selectedFull?.cvd?.series} source={selectedFull?.cvd?.source} divergence={selectedFull?.cvd?.divergence} dataMode={flowProvenance.dataMode} height={300}/></Section>
      <Section title="Aggression, initiative & absorption" icon={<Activity size={16}/>} description="Signed buy/sell volume by three-minute bar, inferred from the quote stream; pressure beyond ±60% marks initiative, while amber points flag high-volume absorption."><OrderFlowPulse bars={selectedFull?.footprint?.bars} source={selectedFull?.footprint?.source ?? selectedFull?.cvd?.source} asOf={selectedFull?.footprint?.bars?.at(-1)?.time ?? latest?.generated_at} dataMode={flowProvenance.dataMode}/></Section>
      <div className="grid gap-4 xl:grid-cols-[1.15fr_.85fr]"><Section title="Recent 3-minute footprints" icon={<BookOpen size={16}/>} description="Traded volume bucketed at price with bid/ask sides inferred from quotes; ≥3× imbalance is highlighted."><FootprintGrid bars={selectedFull?.footprint?.bars} source={selectedFull?.footprint?.source} maxBars={4} dataMode={flowProvenance.dataMode}/></Section><Section title="Option OI walls" icon={<Layers3 size={16}/>}><WallPanel result={selectedFull}/></Section></div>
      <div className="grid gap-4 lg:grid-cols-2">
        <Section title="Long sequence" icon={<ListChecks size={16}/>}><div className="space-y-3"><GateChips title="Entry and safety" gates={selectedFull?.long_gates} blockedReasons={selectedFull?.preferred_direction !== "SHORT" ? selectedFull?.blocked_reasons : undefined}/><GateChips title="Evidence · need any 2" gates={selectedFull?.long_confirmations}/></div></Section>
        <Section title="Short sequence" icon={<ListChecks size={16}/>}><div className="space-y-3"><GateChips title="Entry and safety" gates={selectedFull?.short_gates} blockedReasons={selectedFull?.preferred_direction === "SHORT" ? selectedFull?.blocked_reasons : undefined}/><GateChips title="Evidence · need any 2" gates={selectedFull?.short_confirmations}/></div></Section>
      </div>
    </div> : null}

    {data && activeTab === "risk" ? <div className="space-y-4">
      <section className="grid grid-cols-2 gap-3 md:grid-cols-6"><MetricTile label="Entry" value={formatNumber(selected?.risk?.entry, 2)}/><MetricTile label="Stop" value={formatNumber(selected?.risk?.stop, 2)} detail="setup extreme + 0.25 ATR"/><MetricTile label="Target 1" value={formatNumber(selected?.risk?.target1, 2)} detail="50%, then BE"/><MetricTile label="Reward/risk" value={rrStrip.value} detail={rrStrip.detail} color={rrStrip.ok ? undefined : "text-text-muted"}/><MetricTile label="Target 2" value={formatNumber(selected?.action === "SHORT" ? selected?.risk?.target2_short : selected?.risk?.target2_long, 2)} detail="nearest structural target"/><MetricTile label="Risk" value={`${formatNumber((selected?.risk?.risk_fraction ?? 0) * 100, 2)}%`} detail={`${selected?.confirmation_count === 3 ? "full" : "reduced"} · lot ${selected?.risk?.lot_size ?? "—"}`}/></section>
      {!rr.ok ? <Section title="Trade plan incomplete" icon={<ShieldCheck size={16}/>} description="The lane reports a reward/risk figure, but the plan it would be measured against is not in the payload. A ratio without a stop and a target is not a measurement, so it is suppressed rather than displayed.">
        <div className="flex flex-wrap items-center gap-2">
          <SufficiencyBadge sufficiency="insufficient" reasons={[rr.reason]}/>
          {rr.missing.map((field) => <StatusBadge key={field} label={`${field} missing`} variant="warn"/>)}
          {selected?.gates?.reward_risk_1_5 ? <StatusBadge label="gate reward_risk_1_5 computed off an incomplete plan" variant="warn"/> : null}
          <span className="text-[11.5px] text-text-muted">Reported reward_risk: {formatNumber(selected?.risk?.reward_risk, 2)} — not rendered as R.</span>
        </div>
      </Section> : null}
      <Section title="Daily circuit breaker" icon={<ShieldCheck size={16}/>}><div className="flex flex-wrap gap-2"><StatusBadge label={circuit?.locked ? "LOCKED" : "ARMED"} variant={circuit?.locked ? "error" : "success"}/><StatusBadge label={`${circuit?.consecutive_losses ?? 0}/2 consecutive losses`} variant="neutral"/><StatusBadge label={`day ${formatMoney(circuit?.day_pnl)}`} variant="neutral"/><StatusBadge label={`limit ${formatMoney(circuit?.loss_limit)}`} variant="neutral"/></div></Section>
      <ExecutionSections market={market} paper={paper} asOf={latest?.generated_at}/>
    </div> : null}
    {data && activeTab === "gates" ? <SignalQualityTab laneKeys={[market === "MCX" ? "institutional_convergence_commodity" : "institutional_convergence"]} title={`${market} Institutional Convergence validation`}/> : null}
  </DeskShell>;
}

function ResultMatrix({ rows, detail, selected, onSelect, generatedAt }: { rows: Result[]; detail?: Result; selected?: string; onSelect: (s: string) => void; generatedAt?: string }) {
  const [expanded, setExpanded] = useState<string | null>(null);
  return <div className="overflow-x-auto"><table className="w-full min-w-[900px] text-left text-xs"><thead className="text-text-muted"><tr><th className="w-6"/><th>Symbol</th><th>Contract</th><th>Stage</th><th>Action</th><th>Evidence</th><th>Score</th><th>Blocked</th></tr></thead><tbody className="divide-y divide-border">{rows.map((row) => {
    // The compact matrix row omits heavy detail (profile / footprint bars /
    // CVD series). When a row is expanded it is also selected, so the parent's
    // single detail fetch (`detail`) supplies the full payload for it.
    const full = detail && detail.symbol === row.symbol ? { ...row, ...detail } : row;
    return <Fragment key={row.symbol}>
    <tr onClick={() => onSelect(row.symbol)} className={`cursor-pointer ${selected === row.symbol ? "bg-accent-blue/5" : ""}`}>
      <td className="py-3 pr-1"><button type="button" aria-label={`${expanded === row.symbol ? "Collapse" : "Expand"} ${row.symbol}`} onClick={(e) => { e.stopPropagation(); onSelect(row.symbol); setExpanded((cur) => cur === row.symbol ? null : row.symbol); }} className="rounded p-0.5 text-text-muted hover:text-text-primary">{expanded === row.symbol ? <ChevronDown size={14}/> : <ChevronRight size={14}/>}</button></td>
      <td className="py-3 font-semibold">{row.symbol}</td><td className="font-mono text-[10px]">{row.futures_contract}</td><td><StatusBadge label={row.setup_state ?? "LEGACY"} variant={row.setup_state === "CONFIRMED" ? "success" : row.setup_state === "MISSED_NO_CHASE" ? "warn" : "neutral"}/></td>
      <td><StatusBadge label={row.action} variant={row.action === "LONG" ? "success" : row.action === "SHORT" ? "error" : "neutral"}/></td>
      <td>{row.confirmation_count != null ? `${row.confirmation_count}/${row.confirmation_required ?? 2}` : "—"}</td><td>{formatNumber(row.score, 1)}</td>
      <td>{(row.blocked_reasons ?? []).slice(0, 3).join(" · ")}</td>
    </tr>
    {expanded === row.symbol ? <tr className="bg-bg-secondary/20"><td colSpan={8} className="p-3">
      <div className="mb-2 flex flex-wrap items-center gap-2"><OfSourceBadge source={full.cvd?.source} size="sm"/><LastUpdated timestamp={full.footprint?.bars?.at(-1)?.time ?? full.footprint?.last_bar_time ?? generatedAt} label="last bar"/></div>
      <div className="grid gap-3 xl:grid-cols-[250px_minmax(0,1fr)]">
        <ProfileLadder spot={full.spot} vah={full.profile?.vah} val={full.profile?.val} poc={full.profile?.poc} ibHigh={full.profile?.initial_balance_high} ibLow={full.profile?.initial_balance_low} prior={full.profile?.prior} hvnPrices={full.profile?.hvn_prices} singlePrints={full.profile?.single_prints} tpoCounts={full.profile?.tpo_counts} tpoLetters={full.profile?.tpo_letters} expandTitle={`${row.symbol} · level ladder`} height={230} digits={2}/>
        <div className="space-y-3">
          <CvdPanel series={full.cvd?.series} source={full.cvd?.source} divergence={full.cvd?.divergence} height={160} hideHeader showDelta={false}/>
          <div className="grid gap-3 md:grid-cols-2">
            <div className="space-y-2"><GateChips title="Long entry" gates={row.long_gates} blockedReasons={row.preferred_direction !== "SHORT" ? row.blocked_reasons : undefined}/><GateChips title="Long evidence · any 2" gates={row.long_confirmations}/></div>
            <div className="space-y-2"><GateChips title="Short entry" gates={row.short_gates} blockedReasons={row.preferred_direction === "SHORT" ? row.blocked_reasons : undefined}/><GateChips title="Short evidence · any 2" gates={row.short_confirmations}/></div>
          </div>
        </div>
      </div>
    </td></tr> : null}
  </Fragment>;
  })}</tbody></table></div>;
}
/**
 * Lane-level aggression imbalance — BOUNDED.
 *
 * The payload's `long_ratio` / `short_ratio` were rendered as "6630.00×" and
 * "4355.00×", which reads as a 6,630-fold imbalance. They are aggregate
 * magnitudes, and the honest reading of two magnitudes is the share one takes
 * of the total plus both raw numbers. Zero opposing volume renders "one-sided",
 * never a giant multiple; no volume at all renders as MISSING (—), not 0%.
 */
function FootprintImbalanceTiles({ footprint }: { footprint?: Result["footprint"] }) {
  const buy = footprint?.long_ratio ?? null;
  const sell = footprint?.short_ratio ?? null;
  const imb = imbalanceOf(buy, sell);
  return <>
    <MetricTile
      label="Flow imbalance"
      value={imb.pct == null ? "—" : imb.oneSided ? "one-sided" : `${imb.pct.toFixed(0)}% buy`}
      detail={imb.pct == null ? "not reported" : describeImbalance(imb, "buy", "sell")}
      color={imb.pct == null ? "text-text-muted" : imb.pct >= 60 ? "text-accent-green" : imb.pct <= 40 ? "text-accent-red" : undefined}
    />
    <MetricTile
      label="Buy / sell units"
      value={buy == null && sell == null ? "—" : `${formatNumber(buy, 0)} / ${formatNumber(sell, 0)}`}
      detail="reported aggression volume"
      color={buy == null && sell == null ? "text-text-muted" : undefined}
    />
  </>;
}

function PriceChart({ result }: { result?: Result }) { const rows = result?.bars ?? []; return <div className="h-[280px]"><ResponsiveContainer><LineChart data={rows}><CartesianGrid strokeDasharray="3 3" opacity={.2}/><XAxis dataKey="time" tickFormatter={(v) => formatISTTime(v)} minTickGap={25}/><YAxis domain={["auto","auto"]}/><Tooltip labelFormatter={(v) => `${formatIST(v)} IST`}/><Line dataKey="close" stroke="#60a5fa" dot={false}/>{result?.profile?.prior?.vah ? <ReferenceLine y={result.profile.prior.vah} stroke="#ef4444" strokeDasharray="4 3" label="VAH"/> : null}{result?.profile?.prior?.val ? <ReferenceLine y={result.profile.prior.val} stroke="#22c55e" strokeDasharray="4 3" label="VAL"/> : null}</LineChart></ResponsiveContainer></div>; }
function WallPanel({ result }: { result?: Result }) { return <div className="space-y-4"><div><div className="mb-2 text-xs font-semibold text-accent-red">Call resistance</div>{(result?.options?.top_call_walls ?? []).map((w) => <div key={w.strike} className="mb-1 flex justify-between rounded border border-border px-3 py-2"><span>{formatNumber(w.strike,0)}</span><span>{formatNumber(w.oi,0)} OI</span></div>)}</div><div><div className="mb-2 text-xs font-semibold text-accent-green">Put support</div>{(result?.options?.top_put_walls ?? []).map((w) => <div key={w.strike} className="mb-1 flex justify-between rounded border border-border px-3 py-2"><span>{formatNumber(w.strike,0)}</span><span>{formatNumber(w.oi,0)} OI</span></div>)}</div></div>; }
/**
 * Execution sections for the Risk & Trades tab — statistics cards, open
 * positions, sortable trade book and the order-log timeline. Fed from the new
 * /trades /orders /statistics endpoints with graceful fallback to the paper
 * snapshot (closed_positions) while those routes are pending deployment.
 */
function ExecutionSections({ market, paper, asOf }: { market: "NSE" | "MCX"; paper?: Payload["paper"]; asOf?: string }) {
  const execution = useConvergenceExecution(market);
  const exec = execution.data;
  const equityBase = paper?.initial_capital ?? null;

  const snapshotTrades = useMemo<ConvergenceTrade[]>(() => (paper?.closed_positions ?? []).map(normalizeTrade), [paper?.closed_positions]);

  // /trades → else paper-snapshot closed positions → else pending skeleton.
  const trades: ConvergenceTrade[] | null = exec?.trades ?? (snapshotTrades.length ? snapshotTrades : null);
  const tradesFallback = exec?.trades == null && snapshotTrades.length > 0;
  const tradesMissing = exec ? exec.trades == null : false;

  const stats = exec?.statistics ?? deriveStatistics(trades ?? [], equityBase);
  const statsMissing = exec ? exec.statistics == null && exec.missing.includes("statistics") : false;

  const derivedOrders = useMemo(() => deriveOrders(trades ?? []), [trades]);
  const serverOrders = exec?.orders && exec.orders.length ? exec.orders : null;
  const orders = serverOrders ?? (derivedOrders.length ? derivedOrders : exec?.orders ?? null);
  const ordersMissing = exec ? exec.orders == null : false;

  return <>
    <StatisticsCards stats={stats} endpointMissing={statsMissing && !tradesFallback}/>
    <OpenPositionsPanel rows={paper?.open_positions ?? []} asOf={asOf}/>
    <TradeBook trades={trades} endpointMissing={tradesMissing} fallbackUsed={tradesFallback} equityBase={equityBase}/>
    <OrderLogTimeline orders={orders} endpointMissing={ordersMissing && !derivedOrders.length} derivedFromTrades={!serverOrders && derivedOrders.length > 0}/>
  </>;
}

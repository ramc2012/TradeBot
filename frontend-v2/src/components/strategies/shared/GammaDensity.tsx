"use client";

/**
 * Gamma-density (dealer GEX) panel — a peculiar institutional options view.
 *
 * Net gamma exposure per strike = spot² · 0.01 · (CE_gamma·CE_oi − PE_gamma·PE_oi)
 * (dealers short calls = +gamma, short puts = −gamma). Rendered as a diverging
 * horizontal profile by strike with the spot, the zero-gamma FLIP level
 * (where cumulative GEX crosses zero — the volatility-regime boundary), and the
 * max-pain strike. Context tiles (PCR, expected move, ATM straddle, net GEX)
 * come from /fno-analytics and populate even off-hours; the per-strike profile
 * needs the live option chain (market hours).
 */
import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { Sigma } from "lucide-react";

import { MetricTile, REFRESH_MS, Section, formatNumber, formatPct, tone } from "@/components/desk-ui";
import { api } from "@/lib/api";
import { CHART } from "./chartTheme";

type StrikeGex = { strike: number; gex: number; ceOi: number; peOi: number };

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function normalizeChain(payload: any, spot: number): StrikeGex[] {
  const entries: any[] = payload?.entries ?? payload?.rows ?? payload?.chain ?? [];
  if (!Array.isArray(entries) || !entries.length) return [];
  const byStrike = new Map<number, { ceG: number; ceOi: number; peG: number; peOi: number }>();
  const num = (v: unknown) => (v == null ? 0 : Number(v) || 0);
  for (const e of entries) {
    const strike = num(e.strike ?? e.strike_price);
    if (!strike) continue;
    const slot = byStrike.get(strike) ?? { ceG: 0, ceOi: 0, peG: 0, peOi: 0 };
    // grouped {strike, ce:{...}, pe:{...}} OR flat {strike, option_type, gamma, oi} OR {ce_gamma, ce_oi, ...}
    const ce = e.ce ?? e.call ?? (String(e.option_type).toUpperCase() === "CE" ? e : null);
    const pe = e.pe ?? e.put ?? (String(e.option_type).toUpperCase() === "PE" ? e : null);
    if (ce) { slot.ceG = num(ce.gamma ?? e.ce_gamma); slot.ceOi = num(ce.oi ?? e.ce_oi); }
    if (pe) { slot.peG = num(pe.gamma ?? e.pe_gamma); slot.peOi = num(pe.oi ?? e.pe_oi); }
    if (e.ce_gamma != null || e.ce_oi != null) { slot.ceG = num(e.ce_gamma); slot.ceOi = num(e.ce_oi); }
    if (e.pe_gamma != null || e.pe_oi != null) { slot.peG = num(e.pe_gamma); slot.peOi = num(e.pe_oi); }
    byStrike.set(strike, slot);
  }
  const k = spot > 0 ? spot * spot * 0.01 : 1;
  return Array.from(byStrike.entries())
    .map(([strike, s]) => ({ strike, gex: k * (s.ceG * s.ceOi - s.peG * s.peOi), ceOi: s.ceOi, peOi: s.peOi }))
    .sort((a, b) => b.strike - a.strike);
}

export function GammaDensity({ symbol = "NIFTY" }: { symbol?: string }) {
  const chainQ = useQuery({
    queryKey: ["gamma", "chain", symbol],
    queryFn: async () => (await api.get(`/api/market/option-chain/${encodeURIComponent(symbol)}`)).data,
    refetchInterval: REFRESH_MS.snapshot,
    refetchOnWindowFocus: false,
  });
  const fnoQ = useQuery({
    queryKey: ["gamma", "fno", symbol],
    queryFn: async () => (await api.get("/api/market/fno-analytics", { params: { symbol } })).data,
    refetchInterval: REFRESH_MS.summary,
    refetchOnWindowFocus: false,
  });

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const nse: any = fnoQ.data?.nse ?? {};
  const straddle = (nse.straddle_summary ?? []).find(
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (r: any) => String(r.underlying).toUpperCase() === symbol.toUpperCase(),
  );
  const maxPain = (nse.max_pain ?? []).find(
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (r: any) => String(r.underlying).toUpperCase() === symbol.toUpperCase(),
  );
  const spot = Number(straddle?.spot_price ?? chainQ.data?.spot ?? 0);

  const strikes = useMemo(() => normalizeChain(chainQ.data, spot), [chainQ.data, spot]);

  const { netGex, flip } = useMemo(() => {
    const total = strikes.reduce((s, r) => s + r.gex, 0);
    // cumulative-from-bottom crossing zero → flip strike
    let cum = 0;
    let flipStrike: number | null = null;
    const asc = [...strikes].sort((a, b) => a.strike - b.strike);
    for (let i = 0; i < asc.length; i++) {
      const prev = cum;
      cum += asc[i].gex;
      if (i > 0 && Math.sign(prev) !== Math.sign(cum) && prev !== 0) {
        flipStrike = asc[i].strike;
        break;
      }
    }
    return { netGex: total, flip: flipStrike };
  }, [strikes]);

  const maxAbs = Math.max(1, ...strikes.map((s) => Math.abs(s.gex)));

  return (
    <Section
      title="Gamma density (dealer GEX)"
      icon={<Sigma size={16} />}
      description="Net dealer gamma exposure per strike — positive (long-gamma) suppresses vol, negative amplifies it"
    >
      <div className="grid grid-cols-2 gap-2.5 sm:grid-cols-3 lg:grid-cols-6">
        <MetricTile size="sm" label="Net GEX" value={fmtBn(netGex)} detail={netGex >= 0 ? "long-gamma (pinned)" : "short-gamma (volatile)"} color={tone(netGex)} />
        <MetricTile size="sm" label="Flip level" value={flip ? formatNumber(flip, 0) : "—"} detail="zero-gamma" />
        <MetricTile size="sm" label="Spot" value={spot ? formatNumber(spot, 1) : "—"} detail={maxPain ? `max-pain ${formatNumber(maxPain.max_pain_strike, 0)}` : ""} />
        <MetricTile size="sm" label="Expected move" value={straddle ? formatPct((straddle.expected_move_pct ?? 0) / 100) : "—"} detail={straddle ? `±${formatNumber(straddle.expected_move, 0)} pts` : ""} />
        <MetricTile size="sm" label="ATM straddle" value={straddle ? formatNumber(straddle.atm_straddle, 1) : "—"} detail={straddle ? `IV ${formatNumber(straddle.avg_iv, 1)}%` : ""} />
        <MetricTile size="sm" label="PCR (OI)" value={maxPain ? formatNumber(maxPain.chain_pcr_oi, 2) : formatNumber(nse.option_chain?.summary?.pcr_oi, 2)} detail="put/call" />
      </div>

      <div className="mt-4">
        {strikes.length ? (
          <GexProfile strikes={strikes} spot={spot} flip={flip} maxPain={maxPain?.max_pain_strike} maxAbs={maxAbs} />
        ) : (
          <div className="rounded-xl border border-dashed border-bg-border/60 px-4 py-10 text-center text-sm text-text-muted">
            Per-strike gamma profile streams from the live option chain during market hours (09:15–15:30 IST). Context
            metrics above update from the analytics feed.
          </div>
        )}
      </div>
    </Section>
  );
}

function GexProfile({
  strikes,
  spot,
  flip,
  maxPain,
  maxAbs,
}: {
  strikes: StrikeGex[];
  spot: number;
  flip: number | null;
  maxPain?: number;
  maxAbs: number;
}) {
  const rowH = 16;
  const H = strikes.length * rowH + 8;
  const W = 520;
  const cx = W / 2;
  const half = W / 2 - 56;
  const sLow = strikes[strikes.length - 1].strike;
  const sHigh = strikes[0].strike;
  const yOf = (s: number) => 4 + (1 - (s - sLow) / (sHigh - sLow || 1)) * (H - 8);

  return (
    <div className="w-full overflow-x-auto">
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full" style={{ height: "auto", aspectRatio: `${W} / ${H}` }}>
        <line x1={cx} y1={0} x2={cx} y2={H} stroke="rgba(255,255,255,0.18)" strokeWidth={0.8} />
        {strikes.map((r) => {
          const w = (Math.abs(r.gex) / maxAbs) * half;
          const y = yOf(r.strike) - rowH / 2 + 1;
          const pos = r.gex >= 0;
          return (
            <g key={r.strike}>
              <rect x={pos ? cx : cx - w} y={y} width={Math.max(0.5, w)} height={rowH - 2} fill={pos ? CHART.green : CHART.red} opacity={0.8} rx={1} />
              <text x={pos ? cx - 4 : cx + 4} y={y + rowH - 4} fontSize={8.5} fill={CHART.muted} textAnchor={pos ? "end" : "start"}>
                {r.strike}
              </text>
            </g>
          );
        })}
        {spot ? (
          <g>
            <line x1={0} x2={W} y1={yOf(spot)} y2={yOf(spot)} stroke="#e6edf3" strokeWidth={0.8} strokeDasharray="4 2" />
            <text x={W - 2} y={yOf(spot) - 2} fontSize={8.5} fill="#e6edf3" textAnchor="end">spot {spot.toFixed(0)}</text>
          </g>
        ) : null}
        {flip ? (
          <g>
            <line x1={0} x2={W} y1={yOf(flip)} y2={yOf(flip)} stroke={CHART.amber} strokeWidth={1} />
            <text x={2} y={yOf(flip) - 2} fontSize={8.5} fill={CHART.amber}>γ-flip {flip.toFixed(0)}</text>
          </g>
        ) : null}
        {maxPain ? (
          <g>
            <line x1={0} x2={W} y1={yOf(maxPain)} y2={yOf(maxPain)} stroke={CHART.violet} strokeWidth={0.7} strokeDasharray="2 3" />
            <text x={W - 2} y={yOf(maxPain) + 9} fontSize={8} fill={CHART.violet} textAnchor="end">max-pain</text>
          </g>
        ) : null}
      </svg>
      <div className="mt-1 flex items-center justify-center gap-4 text-[10px] text-text-muted">
        <span className="inline-flex items-center gap-1.5"><span className="h-2 w-2 rounded-sm" style={{ background: CHART.red }} /> short-gamma (left)</span>
        <span className="inline-flex items-center gap-1.5"><span className="h-2 w-2 rounded-sm" style={{ background: CHART.green }} /> long-gamma (right)</span>
      </div>
    </div>
  );
}

function fmtBn(v: number): string {
  const a = Math.abs(v);
  const sign = v < 0 ? "-" : "";
  if (a >= 1e9) return `${sign}${(a / 1e9).toFixed(2)}B`;
  if (a >= 1e6) return `${sign}${(a / 1e6).toFixed(1)}M`;
  if (a >= 1e3) return `${sign}${(a / 1e3).toFixed(1)}k`;
  return `${sign}${a.toFixed(0)}`;
}

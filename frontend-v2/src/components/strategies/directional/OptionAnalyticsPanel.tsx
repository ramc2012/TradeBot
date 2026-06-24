"use client";

/**
 * Option-chain analytics panel — the new rich-data view the RL policy
 * is now consuming.
 *
 * Shows everything the bandit sees as chain context: PCR, ATM IV,
 * IV skew (25-delta), max pain, DEX, GEX, gamma curve around ATM, top
 * OI strikes on each side, OI-build classification. All metrics
 * tolerate nulls and degrade to "—" when the chain isn't cached.
 */
import { clsx } from "clsx";
import { useRef } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { Activity, Layers, RefreshCw, TrendingDown, TrendingUp } from "lucide-react";

import {
  MetricTile,
  REFRESH_MS,
  Section,
  StatusBadge,
  formatNumber,
  formatPct,
  tone,
} from "@/components/desk-ui";
import { api as apiClient } from "@/lib/api";

type ChainAnalytics = {
  available?: boolean;
  underlying?: string;
  expiry?: string | null;
  spot?: number | null;
  atm_strike?: number | null;
  atm_iv?: number | null;
  pcr_oi?: number | null;
  pcr_volume?: number | null;
  pcr_oi_change?: number | null;
  iv_skew_25d?: number | null;
  iv_skew_25d_norm?: number | null;
  risk_reversal_25d?: number | null;
  iv_25d_call?: number | null;
  iv_25d_put?: number | null;
  gex_total?: number | null;
  dealer_gex_total?: number | null;
  dex_calls?: number | null;
  dex_puts?: number | null;
  dex_net?: number | null;
  total_ce_oi?: number | null;
  total_pe_oi?: number | null;
  total_ce_oi_change?: number | null;
  total_pe_oi_change?: number | null;
  atm_call_oi_change?: number | null;
  atm_put_oi_change?: number | null;
  atm_call_ltp_change_pct?: number | null;
  atm_put_ltp_change_pct?: number | null;
  max_pain?: number | null;
  top_ce_oi?: Array<{ strike: number; oi: number; moneyness_pct: number }>;
  top_pe_oi?: Array<{ strike: number; oi: number; moneyness_pct: number }>;
  oi_build_ce?: Record<string, number>;
  oi_build_pe?: Record<string, number>;
  gamma_curve?: Array<{
    strike: number;
    moneyness_pct: number;
    ce_gamma_oi: number;
    pe_gamma_oi: number;
    total_gamma_oi: number;
  }>;
  key_levels?: {
    call_wall?: { strike: number; distance_pct: number; net_gamma_exposure: number } | null;
    put_wall?: { strike: number; distance_pct: number; net_gamma_exposure: number } | null;
    abs_gamma?: { strike: number; distance_pct: number; net_gamma_exposure: number } | null;
    zero_gamma?: number | null;
    vol_trigger?: number | null;
    dealer_gex_total?: number | null;
    gamma_regime?: string | null;
  };
  gamma_profile?: Array<{ spot: number; gex: number }>;
  trace_exposures?: Array<{
    strike: number;
    moneyness_pct: number;
    ce_oi: number;
    pe_oi: number;
    net_delta_exposure: number;
    net_gamma_exposure: number;
    net_vanna_exposure: number;
    net_charm_exposure: number;
    net_volga_exposure: number;
    magnitude_score: number;
  }>;
  unusual_activity?: Array<{
    strike: number;
    option_type: string;
    moneyness_pct: number;
    ltp?: number | null;
    volume: number;
    oi: number;
    oi_change: number;
    oi_change_pct?: number | null;
    ltp_change_pct?: number | null;
    volume_to_oi: number;
    score: number;
    flags: string[];
  }>;
  expiry_state?: {
    days_to_expiry?: number | null;
    is_expiry_day?: boolean;
    expiry_mode?: string;
    theta_clock_pct?: number | null;
  };
  ntm_volx?: {
    ce_volume?: number | null;
    pe_volume?: number | null;
    ce_oi?: number | null;
    pe_oi?: number | null;
    ce_oi_change?: number | null;
    pe_oi_change?: number | null;
    volume_imbalance?: number | null;
    oi_imbalance?: number | null;
    vxr?: number | null;
    control?: string | null;
    pressure?: string | null;
    rows?: Array<{
      strike: number;
      ce_volume: number;
      pe_volume: number;
      ce_oi: number;
      pe_oi: number;
      ce_oi_change: number;
      pe_oi_change: number;
    }>;
  };
  spectrum?: {
    rows?: Array<{
      strike: number;
      moneyness_pct: number;
      ce_oi: number;
      pe_oi: number;
      ce_oi_change: number;
      pe_oi_change: number;
      ce_volume: number;
      pe_volume: number;
      ce_state: string;
      pe_state: string;
      net_oi_change: number;
      wall_balance: number;
    }>;
    pressure_side?: string | null;
    ce_oi_change?: number | null;
    pe_oi_change?: number | null;
  };
  straddle?: {
    atm_strike?: number | null;
    call_ltp?: number | null;
    put_ltp?: number | null;
    atm_straddle?: number | null;
    expected_move?: number | null;
    expected_move_pct?: number | null;
    upper?: number | null;
    lower?: number | null;
    source?: string | null;
  };
  sigma_bands?: {
    one_sigma?: number | null;
    minus_one_sigma?: number | null;
    plus_one_sigma?: number | null;
    two_sigma?: number | null;
    minus_two_sigma?: number | null;
    plus_two_sigma?: number | null;
    source?: string | null;
  };
  gamma_density?: {
    peak_strike?: number | null;
    peak_gamma_exposure?: number | null;
    convexity?: string | null;
    left_tail?: number | null;
    right_tail?: number | null;
    skew?: number | null;
  };
  writer_cash_proxy?: {
    ce_add_cash?: number | null;
    pe_add_cash?: number | null;
    ce_unwind_cash?: number | null;
    pe_unwind_cash?: number | null;
    net_writer_cash?: number | null;
    dominant_side?: string | null;
  };
  options_table_rows?: Array<{
    strike: number;
    moneyness_pct: number;
    ce: OptionSideRow;
    pe: OptionSideRow;
  }>;
  requested_expiry?: string | null;
  cache_status?: ChainCacheStatus;
  refresh_status?: {
    warmed?: boolean;
    reason?: string | null;
    expiry?: string | null;
  } | null;
};

type OptionSideRow = {
  ltp?: number | null;
  ltp_change_pct?: number | null;
  oi?: number | null;
  oi_change?: number | null;
  oi_change_pct?: number | null;
  volume?: number | null;
  iv?: number | null;
  spread_pct?: number | null;
  volume_to_oi?: number | null;
  state?: string | null;
  acceptance?: string | null;
};

type ChainCacheStatus = {
  requested_expiry?: string | null;
  resolved_expiry?: string | null;
  default_expiry?: string | null;
  used_fallback_expiry?: boolean;
  known_expiries?: string[];
  cached_or_tracked_expiries?: string[];
  catalog_expiries?: string[];
  poll_running?: boolean;
};

function compact(n: number | null | undefined): string {
  if (n == null || Number.isNaN(n)) return "—";
  const abs = Math.abs(n);
  if (abs >= 1e10) return `${(n / 1e10).toFixed(2)} kCr`;
  if (abs >= 1e7) return `${(n / 1e7).toFixed(2)} Cr`;
  if (abs >= 1e5) return `${(n / 1e5).toFixed(2)} L`;
  if (abs >= 1e3) return `${(n / 1e3).toFixed(1)} K`;
  return n.toFixed(0);
}

function pcrTone(pcr: number | null | undefined): string {
  // PCR > 1.2 = bullish (more puts, contrarian); PCR < 0.8 = bearish.
  if (pcr == null) return "text-text-muted";
  if (pcr > 1.2) return "text-accent-green";
  if (pcr < 0.8) return "text-accent-red";
  return "text-text-secondary";
}

function formatStrike(value?: number | null): string {
  return value == null || Number.isNaN(value) ? "—" : formatNumber(value, 0);
}

function levelDetail(level?: { distance_pct?: number | null; net_gamma_exposure?: number | null } | null): string {
  if (!level) return "";
  return `${formatPct(level.distance_pct, 2)} · ${compact(level.net_gamma_exposure)}`;
}

function regimeLabel(value?: string | null): string {
  return String(value || "unknown").replaceAll("_", " ");
}

function stateLabel(value?: string | null): string {
  return String(value || "—").replaceAll("_", " ");
}

function stateTone(value?: string | null): string {
  const normalized = String(value || "");
  if (normalized.includes("collapse") || normalized.includes("unwind")) return "text-accent-red";
  if (normalized.includes("buildup") || normalized.includes("building") || normalized.includes("cover")) return "text-accent-green";
  return "text-text-muted";
}

function signedCompact(n: number | null | undefined): string {
  if (n == null || Number.isNaN(n)) return "—";
  return `${n > 0 ? "+" : ""}${compact(n)}`;
}

function ladderMarkerClass(toneClass?: string): string {
  if (toneClass === "text-accent-red") return "bg-accent-red";
  if (toneClass === "text-accent-green") return "bg-accent-green";
  if (toneClass === "text-accent-blue") return "bg-accent-blue";
  if (toneClass === "text-accent-amber") return "bg-accent-amber";
  return "bg-text-muted";
}

function LevelLadder({
  spot,
  levels,
}: {
  spot?: number | null;
  levels: Array<{ key: string; label: string; value?: number | null; tone?: string }>;
}) {
  const points = [
    ...levels,
    spot != null ? { key: "spot", label: "Spot", value: spot, tone: "text-accent-blue" } : null,
  ].filter((item): item is { key: string; label: string; value: number; tone?: string } => (
    item != null && item.value != null && Number.isFinite(item.value)
  ));
  if (points.length < 2) return null;
  const values = points.map((point) => point.value);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const pad = Math.max((max - min) * 0.08, Math.abs(spot || max || 1) * 0.002);
  const lo = min - pad;
  const hi = max + pad;
  const span = Math.max(hi - lo, 1);
  const sorted = [...points].sort((a, b) => a.value - b.value);

  return (
    <div className="mt-4 rounded-xl border border-bg-border bg-bg-primary/12 p-3">
      <div className="mb-3 flex items-center justify-between gap-2 text-[10.5px] uppercase tracking-[0.16em] text-text-muted">
        <span>Level ladder</span>
        <span className="font-mono">{formatStrike(lo)} to {formatStrike(hi)}</span>
      </div>
      <div className="relative h-24">
        <div className="absolute left-2 right-2 top-9 h-px bg-bg-border" />
        {sorted.map((point, idx) => {
          const left = ((point.value - lo) / span) * 100;
          const above = idx % 2 === 0;
          return (
            <div
              key={point.key}
              className="absolute top-0 w-24 -translate-x-1/2"
              style={{ left: `${Math.min(98, Math.max(2, left))}%` }}
            >
              <div className={clsx("text-center font-mono text-[10px]", point.tone || "text-text-secondary", above ? "mb-1" : "mt-12")}>
                {formatStrike(point.value)}
              </div>
              <div className={clsx("mx-auto h-5 w-px", ladderMarkerClass(point.tone))} />
              <div className={clsx("mt-1 truncate text-center text-[10px]", point.tone || "text-text-muted")}>
                {point.label}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

export default function OptionAnalyticsPanel({
  underlying,
  expiry,
}: {
  underlying: string;
  expiry?: string | null;
}) {
  const forceRefreshRef = useRef(false);
  const { data, isLoading, isFetching, refetch } = useQuery({
    queryKey: ["directional", "chain-analytics", underlying, expiry || ""],
    queryFn: async () => {
      const refresh = forceRefreshRef.current;
      forceRefreshRef.current = false;
      return (
        await apiClient.get("/api/directional-options/chain-analytics", {
          params: { underlying, expiry: expiry || undefined, refresh: refresh || undefined },
        })
      ).data as ChainAnalytics;
    },
    refetchInterval: REFRESH_MS.live,
    refetchOnWindowFocus: false,
  });
  const refreshButton = (
    <button
      type="button"
      title="Refresh option-chain cache"
      aria-label="Refresh option-chain cache"
      onClick={() => {
        forceRefreshRef.current = true;
        void refetch();
      }}
      disabled={isFetching}
      className="inline-flex h-8 w-8 items-center justify-center rounded-lg border border-bg-border bg-bg-primary/30 text-text-muted transition hover:border-accent-blue/45 hover:text-accent-blue disabled:cursor-wait disabled:opacity-60"
    >
      <RefreshCw size={14} className={clsx(isFetching && "animate-spin")} />
    </button>
  );

  if (isLoading) {
    return (
      <Section title="Option-chain analytics" icon={<Layers size={16} />}>
        <div className="text-sm text-text-muted">Loading chain…</div>
      </Section>
    );
  }

  if (!data?.available) {
    const status = data?.cache_status || {};
    const knownExpiries = status.known_expiries || [];
    const refreshReason = data?.refresh_status?.reason;
    return (
      <Section title="Option-chain analytics" icon={<Layers size={16} />} rightSlot={refreshButton}>
        <div className="rounded-xl border border-bg-border bg-bg-primary/15 p-3 text-sm text-text-muted">
          No chain cached for <span className="font-mono">{underlying}</span>
          {expiry ? ` · ${expiry}` : ""}. Chain analytics show up once the
          broker cache has filled the option chain.
          <div className="mt-2 grid gap-1 text-[11.5px] md:grid-cols-2">
            <span>Default expiry: <span className="font-mono text-text-secondary">{status.default_expiry || "—"}</span></span>
            <span>Poller: <span className="font-mono text-text-secondary">{status.poll_running ? "running" : "idle"}</span></span>
            <span className="md:col-span-2">
              Known expiries: <span className="font-mono text-text-secondary">{knownExpiries.slice(0, 5).join(" · ") || "—"}</span>
            </span>
            {refreshReason ? (
              <span className="md:col-span-2 text-accent-amber">Refresh: {refreshReason}</span>
            ) : null}
          </div>
        </div>
      </Section>
    );
  }

  const pcrOi = data.pcr_oi;
  const pcrOiChg = data.pcr_oi_change;
  const ivSkewNorm = data.iv_skew_25d_norm;
  const dexDenom = Math.max(Math.abs(data.dex_calls ?? 0) + Math.abs(data.dex_puts ?? 0), 1);
  const dexRatio = (data.dex_net ?? 0) / dexDenom;
  const totalCeOiChg = data.total_ce_oi_change ?? 0;
  const totalPeOiChg = data.total_pe_oi_change ?? 0;
  const keyLevels = data.key_levels || {};
  const expiryMode = data.expiry_state?.expiry_mode || "unknown";
  const traceRows = [...(data.trace_exposures || [])]
    .sort((a, b) => Math.abs(a.moneyness_pct) - Math.abs(b.moneyness_pct))
    .slice(0, 9);
  const unusualRows = data.unusual_activity || [];
  const dealerGex = data.dealer_gex_total ?? keyLevels.dealer_gex_total ?? null;
  const ntm = data.ntm_volx || {};
  const spectrumRows = (data.spectrum?.rows || []).map((row) => ({
    ...row,
    pe_oi_plot: -Math.abs(row.pe_oi || 0),
    pe_oi_change_plot: -Math.abs(row.pe_oi_change || 0),
  }));
  const optionTableRows = data.options_table_rows || [];
  const gammaProfile = data.gamma_profile || [];
  const gammaDensity = data.gamma_density || {};
  const writerCash = data.writer_cash_proxy || {};

  return (
    <div className="space-y-4">
      <Section
        title={`Option-chain analytics · ${data.underlying || underlying}`}
        icon={<Layers size={16} />}
        rightSlot={
          <span className="text-[11px] text-text-muted">
            {data.expiry ? `expiry ${data.expiry}` : ""}
            {data.atm_strike ? ` · ATM ${data.atm_strike}` : ""}
          </span>
        }
      >
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-6">
          <MetricTile
            label="PCR (OI)"
            value={formatNumber(pcrOi, 2)}
            color={pcrTone(pcrOi)}
            detail={pcrOiChg != null ? `Δ ${(pcrOiChg >= 0 ? "+" : "") + pcrOiChg.toFixed(3)}` : ""}
          />
          <MetricTile
            label="PCR (Vol)"
            value={formatNumber(data.pcr_volume, 2)}
            color={pcrTone(data.pcr_volume)}
          />
          <MetricTile
            label="ATM IV"
            value={data.atm_iv != null ? formatPct(data.atm_iv, 2) : "—"}
            detail={ivSkewNorm != null ? `skew ${(ivSkewNorm * 100).toFixed(1)}%` : ""}
          />
          <MetricTile
            label="IV skew (25Δ)"
            value={data.iv_skew_25d != null ? (data.iv_skew_25d >= 0 ? "+" : "") + data.iv_skew_25d.toFixed(2) : "—"}
            color={tone(data.iv_skew_25d)}
            detail="put IV − call IV"
          />
          <MetricTile
            label="Max pain"
            value={data.max_pain != null ? formatNumber(data.max_pain, 0) : "—"}
            detail={data.spot != null && data.max_pain != null ? `${(((data.max_pain - data.spot) / data.spot) * 100).toFixed(2)}% from spot` : ""}
          />
          <MetricTile
            label="GEX"
            value={compact(data.gex_total)}
            color={tone(data.gex_total)}
            detail="γ × OI × side"
          />
        </div>
        <div className="mt-3 flex flex-wrap items-center gap-2 text-[11px] text-text-muted">
          {refreshButton}
          {data.cache_status?.used_fallback_expiry ? (
            <StatusBadge label="fallback expiry" variant="warn" />
          ) : null}
          {data.requested_expiry && data.requested_expiry !== data.expiry ? (
            <span>requested <span className="font-mono">{data.requested_expiry}</span>, using <span className="font-mono">{data.expiry}</span></span>
          ) : null}
          {data.refresh_status?.warmed ? <StatusBadge label="cache refreshed" variant="success" /> : null}
        </div>
      </Section>

      <Section
        title="Key levels"
        icon={<TrendingDown size={16} />}
        rightSlot={
          <div className="flex flex-wrap justify-end gap-1.5">
            <StatusBadge
              label={regimeLabel(keyLevels.gamma_regime)}
              variant={String(keyLevels.gamma_regime || "").startsWith("positive") ? "success" : "warn"}
            />
            <StatusBadge label={expiryMode.replaceAll("_", " ")} variant={data.expiry_state?.is_expiry_day ? "warn" : "info"} />
          </div>
        }
      >
        <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
          <MetricTile
            label="Dealer GEX"
            value={compact(dealerGex)}
            color={tone(dealerGex)}
            detail={dealerGex != null && dealerGex >= 0 ? "pinning" : dealerGex != null ? "trend amp" : ""}
          />
          <MetricTile
            label="Gamma flip"
            value={formatStrike(keyLevels.zero_gamma)}
            detail={keyLevels.zero_gamma && data.spot ? `${formatPct((keyLevels.zero_gamma - data.spot) / data.spot, 2)} from spot` : ""}
          />
          <MetricTile
            label="Call wall"
            value={formatStrike(keyLevels.call_wall?.strike)}
            detail={levelDetail(keyLevels.call_wall)}
            color="text-accent-green"
          />
          <MetricTile
            label="Put wall"
            value={formatStrike(keyLevels.put_wall?.strike)}
            detail={levelDetail(keyLevels.put_wall)}
            color="text-accent-red"
          />
          <MetricTile
            label="Abs gamma"
            value={formatStrike(keyLevels.abs_gamma?.strike)}
            detail={levelDetail(keyLevels.abs_gamma)}
          />
          <MetricTile
            label="Theta clock"
            value={data.expiry_state?.theta_clock_pct != null ? formatPct(data.expiry_state.theta_clock_pct, 0) : "—"}
            detail={data.expiry_state?.days_to_expiry != null ? `${data.expiry_state.days_to_expiry} DTE` : ""}
            color={data.expiry_state?.is_expiry_day ? "text-accent-amber" : undefined}
          />
        </div>
        <LevelLadder
          spot={data.spot}
          levels={[
            { key: "put-wall", label: "Put wall", value: keyLevels.put_wall?.strike, tone: "text-accent-red" },
            { key: "zero-gamma", label: "Zero gamma", value: keyLevels.zero_gamma, tone: "text-accent-blue" },
            { key: "vol-trigger", label: "Vol trigger", value: keyLevels.vol_trigger, tone: "text-accent-amber" },
            { key: "max-pain", label: "Max pain", value: data.max_pain, tone: "text-text-secondary" },
            { key: "call-wall", label: "Call wall", value: keyLevels.call_wall?.strike, tone: "text-accent-green" },
          ]}
        />
        <div className="mt-3 grid gap-2 text-[11.5px] text-text-secondary md:grid-cols-2">
          <div className="rounded-lg border border-bg-border bg-bg-primary/10 px-3 py-2">
            Gamma regime: <span className={clsx("font-semibold", dealerGex != null && dealerGex >= 0 ? "text-accent-green" : "text-accent-amber")}>
              {dealerGex != null && dealerGex >= 0 ? "pinning / mean-reverting" : dealerGex != null ? "trend-amplifying" : "unknown"}
            </span>
          </div>
          <div className="rounded-lg border border-bg-border bg-bg-primary/10 px-3 py-2">
            Spot vs flip: <span className={clsx("font-semibold", data.spot != null && keyLevels.zero_gamma != null && data.spot >= keyLevels.zero_gamma ? "text-accent-green" : "text-accent-red")}>
              {data.spot != null && keyLevels.zero_gamma != null ? (data.spot >= keyLevels.zero_gamma ? "above zero gamma" : "below zero gamma") : "waiting for flip"}
            </span>
          </div>
        </div>
      </Section>

      <div className="grid gap-4 xl:grid-cols-2">
        <Section
          title="NTM VolX / VXR"
          icon={<Activity size={16} />}
          rightSlot={<StatusBadge label={stateLabel(ntm.pressure)} variant={(ntm.pressure || "") === "expanding" ? "warn" : "info"} />}
        >
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <MetricTile
              label="Call vol"
              value={compact(ntm.ce_volume)}
              color="text-accent-green"
              detail={`ΔOI ${signedCompact(ntm.ce_oi_change)}`}
            />
            <MetricTile
              label="Put vol"
              value={compact(ntm.pe_volume)}
              color="text-accent-red"
              detail={`ΔOI ${signedCompact(ntm.pe_oi_change)}`}
            />
            <MetricTile
              label="VXR"
              value={ntm.vxr != null ? formatPct(ntm.vxr, 1) : "—"}
              detail={stateLabel(ntm.control)}
              color={tone((ntm.volume_imbalance || 0) * 100)}
            />
            <MetricTile
              label="NTM OI lean"
              value={ntm.oi_imbalance != null ? formatPct(ntm.oi_imbalance, 1) : "—"}
              detail="put wall minus call wall"
              color={tone(ntm.oi_imbalance)}
            />
          </div>
          {ntm.rows?.length ? (
            <div className="mt-3 h-32">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={ntm.rows}>
                  <XAxis dataKey="strike" tick={{ fill: "#94a3b8", fontSize: 10 }} />
                  <YAxis tick={{ fill: "#94a3b8", fontSize: 10 }} tickFormatter={(v) => compact(v)} />
                  <Tooltip
                    formatter={(value: number, name: string) => [compact(value), name === "ce_volume" ? "CE volume" : "PE volume"]}
                    labelFormatter={(strike) => `Strike ${strike}`}
                    contentStyle={{ background: "#0f1724", border: "1px solid #1e2d45" }}
                  />
                  <Bar dataKey="ce_volume" fill="#00d4a3" />
                  <Bar dataKey="pe_volume" fill="#ff4757" />
                </BarChart>
              </ResponsiveContainer>
            </div>
          ) : null}
        </Section>

        <Section title="Straddle range / sigma bands" icon={<TrendingUp size={16} />}>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <MetricTile
              label="ATM straddle"
              value={formatNumber(data.straddle?.atm_straddle, 2)}
              detail={`CE ${formatNumber(data.straddle?.call_ltp, 1)} · PE ${formatNumber(data.straddle?.put_ltp, 1)}`}
            />
            <MetricTile
              label="Expected move"
              value={data.straddle?.expected_move != null ? `±${formatNumber(data.straddle.expected_move, 0)}` : "—"}
              detail={formatPct(data.straddle?.expected_move_pct, 2)}
            />
            <MetricTile
              label="-1σ / +1σ"
              value={`${formatStrike(data.sigma_bands?.minus_one_sigma)} / ${formatStrike(data.sigma_bands?.plus_one_sigma)}`}
              detail={data.sigma_bands?.source || data.straddle?.source || ""}
            />
            <MetricTile
              label="Convexity"
              value={stateLabel(gammaDensity.convexity)}
              detail={gammaDensity.skew != null ? `skew ${formatPct(gammaDensity.skew, 1)}` : ""}
              color={tone(gammaDensity.skew)}
            />
          </div>
          <div className="mt-3 grid grid-cols-2 gap-2 text-[11px] text-text-muted">
            <div className="rounded border border-bg-border bg-bg-primary/10 p-2">
              <div className="uppercase tracking-[0.14em]">Writer cash proxy</div>
              <div className={clsx("mt-1 font-mono", tone(writerCash.net_writer_cash))}>{signedCompact(writerCash.net_writer_cash)}</div>
              <div className="mt-0.5">{stateLabel(writerCash.dominant_side)}</div>
            </div>
            <div className="rounded border border-bg-border bg-bg-primary/10 p-2">
              <div className="uppercase tracking-[0.14em]">Gamma density peak</div>
              <div className="mt-1 font-mono text-text-primary">{formatStrike(gammaDensity.peak_strike)}</div>
              <div className={clsx("mt-0.5 font-mono", tone(gammaDensity.peak_gamma_exposure))}>{compact(gammaDensity.peak_gamma_exposure)}</div>
            </div>
          </div>
        </Section>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Section title="Delta exposure (DEX)" icon={<Activity size={16} />}>
          <div className="grid grid-cols-3 gap-3">
            <MetricTile label="Calls" value={compact(data.dex_calls)} color="text-accent-green" />
            <MetricTile label="Puts" value={compact(data.dex_puts)} color="text-accent-red" />
            <MetricTile
              label="Net (ratio)"
              value={(dexRatio >= 0 ? "+" : "") + dexRatio.toFixed(2)}
              color={tone(dexRatio)}
              detail="(C − P) / (|C| + |P|)"
            />
          </div>
          <div className="mt-3 grid grid-cols-2 gap-2 text-[11px] text-text-muted">
            <div className="rounded border border-bg-border bg-bg-primary/10 p-2">
              <div className="uppercase tracking-[0.14em]">CE OI / Δ</div>
              <div className="mt-1 font-mono text-text-primary">{compact(data.total_ce_oi)}</div>
              <div className={clsx("mt-0.5 font-mono", tone(totalCeOiChg))}>Δ {compact(totalCeOiChg)}</div>
            </div>
            <div className="rounded border border-bg-border bg-bg-primary/10 p-2">
              <div className="uppercase tracking-[0.14em]">PE OI / Δ</div>
              <div className="mt-1 font-mono text-text-primary">{compact(data.total_pe_oi)}</div>
              <div className={clsx("mt-0.5 font-mono", tone(totalPeOiChg))}>Δ {compact(totalPeOiChg)}</div>
            </div>
          </div>
        </Section>

        <Section title="ATM activity (this bar)" icon={<TrendingUp size={16} />}>
          <div className="grid grid-cols-2 gap-3">
            <MetricTile
              label="ATM CE  Δ LTP%"
              value={data.atm_call_ltp_change_pct != null ? (data.atm_call_ltp_change_pct >= 0 ? "+" : "") + data.atm_call_ltp_change_pct.toFixed(2) + "%" : "—"}
              color={tone(data.atm_call_ltp_change_pct)}
              detail={`Δ OI ${compact(data.atm_call_oi_change)}`}
            />
            <MetricTile
              label="ATM PE  Δ LTP%"
              value={data.atm_put_ltp_change_pct != null ? (data.atm_put_ltp_change_pct >= 0 ? "+" : "") + data.atm_put_ltp_change_pct.toFixed(2) + "%" : "—"}
              color={tone(data.atm_put_ltp_change_pct)}
              detail={`Δ OI ${compact(data.atm_put_oi_change)}`}
            />
          </div>
          {data.oi_build_ce || data.oi_build_pe ? (
            <div className="mt-3">
              <div className="mb-1 text-[10.5px] uppercase tracking-[0.16em] text-text-muted">
                OI build classification (chain-wide)
              </div>
              <table className="w-full text-[11px]">
                <thead className="text-[10px] uppercase tracking-wide text-text-muted">
                  <tr><th className="text-left pb-1">Side</th><th>Long Bld</th><th>Short Bld</th><th>Long Unw</th><th>Short Cov</th></tr>
                </thead>
                <tbody>
                  <tr className="border-t border-bg-border/30">
                    <td className="py-1 text-accent-green font-semibold">CE</td>
                    <td className="text-center font-mono">{data.oi_build_ce?.long_buildup ?? 0}</td>
                    <td className="text-center font-mono">{data.oi_build_ce?.short_buildup ?? 0}</td>
                    <td className="text-center font-mono">{data.oi_build_ce?.long_unwind ?? 0}</td>
                    <td className="text-center font-mono">{data.oi_build_ce?.short_cover ?? 0}</td>
                  </tr>
                  <tr className="border-t border-bg-border/30">
                    <td className="py-1 text-accent-red font-semibold">PE</td>
                    <td className="text-center font-mono">{data.oi_build_pe?.long_buildup ?? 0}</td>
                    <td className="text-center font-mono">{data.oi_build_pe?.short_buildup ?? 0}</td>
                    <td className="text-center font-mono">{data.oi_build_pe?.long_unwind ?? 0}</td>
                    <td className="text-center font-mono">{data.oi_build_pe?.short_cover ?? 0}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          ) : null}
        </Section>
      </div>

      {spectrumRows.length ? (
        <Section
          title="Spectrum walls · OI and ΔOI by strike"
          icon={<Layers size={16} />}
          rightSlot={<StatusBadge label={stateLabel(data.spectrum?.pressure_side)} variant="info" />}
        >
          <div className="h-60">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={spectrumRows}>
                <CartesianGrid stroke="#1e2d45" vertical={false} />
                <XAxis dataKey="strike" tick={{ fill: "#94a3b8", fontSize: 10 }} />
                <YAxis tick={{ fill: "#94a3b8", fontSize: 10 }} tickFormatter={(v) => compact(Math.abs(Number(v)))} />
                <Tooltip
                  formatter={(value: number, name: string) => {
                    const label =
                      name === "ce_oi" ? "CE OI" :
                      name === "pe_oi_plot" ? "PE OI" :
                      name === "ce_oi_change" ? "CE ΔOI" : "PE ΔOI";
                    return [compact(Math.abs(value)), label];
                  }}
                  labelFormatter={(strike) => `Strike ${strike}`}
                  contentStyle={{ background: "#0f1724", border: "1px solid #1e2d45" }}
                />
                <ReferenceLine y={0} stroke="#334155" />
                {data.atm_strike ? <ReferenceLine x={data.atm_strike} stroke="#3b82f6" strokeDasharray="3 3" /> : null}
                <Bar dataKey="ce_oi" fill="#00d4a3" />
                <Bar dataKey="pe_oi_plot" fill="#ff4757" />
              </BarChart>
            </ResponsiveContainer>
          </div>
          <div className="mt-2 grid gap-2 md:grid-cols-2">
            {spectrumRows
              .filter((row) => row.ce_state !== "holding" || row.pe_state !== "holding")
              .slice(0, 6)
              .map((row) => (
                <div key={`wall-state-${row.strike}`} className="flex items-center justify-between rounded border border-bg-border bg-bg-primary/10 px-2.5 py-2 text-[11px]">
                  <span className="font-mono text-text-primary">{formatStrike(row.strike)}</span>
                  <span className={clsx("truncate px-2", stateTone(row.ce_state))}>CE {stateLabel(row.ce_state)}</span>
                  <span className={clsx("truncate text-right", stateTone(row.pe_state))}>PE {stateLabel(row.pe_state)}</span>
                </div>
              ))}
          </div>
        </Section>
      ) : null}

      {data.gamma_curve && data.gamma_curve.length > 0 ? (
        <Section title="Gamma curve · γ × OI around ATM" icon={<TrendingDown size={16} />}>
          <div className="h-56">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={data.gamma_curve}>
                <XAxis dataKey="strike" tick={{ fill: "#94a3b8", fontSize: 11 }} />
                <YAxis tick={{ fill: "#94a3b8", fontSize: 11 }} tickFormatter={(v) => compact(v)} />
                <Tooltip
                  formatter={(value: number, name: string) => [compact(value), name === "ce_gamma_oi" ? "CE γ·OI" : "PE γ·OI"]}
                  labelFormatter={(strike) => `Strike ${strike}`}
                  contentStyle={{ background: "#0f1724", border: "1px solid #1e2d45" }}
                />
                {data.spot ? <ReferenceLine x={data.atm_strike ?? undefined} stroke="#3b82f6" strokeDasharray="3 3" label={{ value: "ATM", fill: "#94a3b8", fontSize: 10 }} /> : null}
                <Bar dataKey="ce_gamma_oi" stackId="g" fill="#00d4a3" />
                <Bar dataKey="pe_gamma_oi" stackId="g" fill="#ff4757" />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </Section>
      ) : null}

      {gammaProfile.length ? (
        <Section title="Gamma exposure profile · repriced spot grid" icon={<TrendingDown size={16} />}>
          <div className="h-56">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={gammaProfile}>
                <CartesianGrid stroke="#1e2d45" vertical={false} />
                <XAxis dataKey="spot" type="number" domain={["dataMin", "dataMax"]} tick={{ fill: "#94a3b8", fontSize: 10 }} />
                <YAxis tick={{ fill: "#94a3b8", fontSize: 10 }} tickFormatter={(v) => compact(v)} />
                <Tooltip
                  formatter={(value: number) => [compact(value), "Dealer GEX"]}
                  labelFormatter={(spotLevel) => `Spot ${spotLevel}`}
                  contentStyle={{ background: "#0f1724", border: "1px solid #1e2d45" }}
                />
                <ReferenceLine y={0} stroke="#64748b" strokeDasharray="3 3" />
                {data.spot ? <ReferenceLine x={Number(data.spot.toFixed(2))} stroke="#f59e0b" strokeDasharray="3 3" /> : null}
                {keyLevels.zero_gamma ? <ReferenceLine x={keyLevels.zero_gamma} stroke="#38bdf8" strokeDasharray="3 3" /> : null}
                <Area type="monotone" dataKey="gex" stroke="#38bdf8" fill="#0ea5e9" fillOpacity={0.18} strokeWidth={1.6} />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        </Section>
      ) : null}

      {traceRows.length ? (
        <Section title="TRACE exposures" icon={<Activity size={16} />}>
          <div className="overflow-x-auto">
            <table className="min-w-full text-[11.5px]">
              <thead className="text-[10px] uppercase tracking-wide text-text-muted">
                <tr className="border-b border-bg-border/40">
                  <th className="px-2 py-1.5 text-left">Strike</th>
                  <th className="px-2 py-1.5 text-right">Δ exp</th>
                  <th className="px-2 py-1.5 text-right">Γ exp</th>
                  <th className="px-2 py-1.5 text-right">Vanna</th>
                  <th className="px-2 py-1.5 text-right">Charm</th>
                  <th className="px-2 py-1.5 text-right">Volga</th>
                  <th className="px-2 py-1.5 text-right">% spot</th>
                </tr>
              </thead>
              <tbody>
                {traceRows.map((r) => (
                  <tr key={`trace-${r.strike}`} className="border-b border-bg-border/20">
                    <td className="px-2 py-1.5 font-mono text-text-primary">{formatStrike(r.strike)}</td>
                    <td className={clsx("px-2 py-1.5 text-right font-mono", tone(r.net_delta_exposure))}>{compact(r.net_delta_exposure)}</td>
                    <td className={clsx("px-2 py-1.5 text-right font-mono", tone(r.net_gamma_exposure))}>{compact(r.net_gamma_exposure)}</td>
                    <td className={clsx("px-2 py-1.5 text-right font-mono", tone(r.net_vanna_exposure))}>{compact(r.net_vanna_exposure)}</td>
                    <td className={clsx("px-2 py-1.5 text-right font-mono", tone(r.net_charm_exposure))}>{compact(r.net_charm_exposure)}</td>
                    <td className={clsx("px-2 py-1.5 text-right font-mono", tone(r.net_volga_exposure))}>{compact(r.net_volga_exposure)}</td>
                    <td className={clsx("px-2 py-1.5 text-right font-mono", tone(r.moneyness_pct))}>{formatPct(r.moneyness_pct, 2)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Section>
      ) : null}

      {optionTableRows.length ? (
        <Section title="Options table · price, volume, OI and acceptance" icon={<Layers size={16} />}>
          <div className="overflow-x-auto">
            <table className="min-w-full text-[11px]">
              <thead className="text-[9.5px] uppercase tracking-wide text-text-muted">
                <tr className="border-b border-bg-border/40">
                  <th className="px-2 py-1.5 text-right">CE LTP</th>
                  <th className="px-2 py-1.5 text-right">CE OI</th>
                  <th className="px-2 py-1.5 text-right">CE ΔOI</th>
                  <th className="px-2 py-1.5 text-left">CE state</th>
                  <th className="px-2 py-1.5 text-center text-text-primary">Strike</th>
                  <th className="px-2 py-1.5 text-left">PE state</th>
                  <th className="px-2 py-1.5 text-right">PE ΔOI</th>
                  <th className="px-2 py-1.5 text-right">PE OI</th>
                  <th className="px-2 py-1.5 text-right">PE LTP</th>
                </tr>
              </thead>
              <tbody>
                {optionTableRows.map((row) => (
                  <tr key={`opt-row-${row.strike}`} className={clsx("border-b border-bg-border/20", row.strike === data.atm_strike ? "bg-blue-500/10" : "")}>
                    <td className={clsx("px-2 py-1.5 text-right font-mono", tone(row.ce.ltp_change_pct))}>
                      {formatNumber(row.ce.ltp, 2)}
                      <span className="ml-1 text-[9.5px] text-text-muted">{row.ce.ltp_change_pct != null ? formatPct(row.ce.ltp_change_pct, 1, { asPercent: true }) : ""}</span>
                    </td>
                    <td className="px-2 py-1.5 text-right font-mono">{compact(row.ce.oi)}</td>
                    <td className={clsx("px-2 py-1.5 text-right font-mono", tone(row.ce.oi_change))}>{signedCompact(row.ce.oi_change)}</td>
                    <td className={clsx("max-w-[120px] truncate px-2 py-1.5", stateTone(row.ce.acceptance || row.ce.state))}>{stateLabel(row.ce.acceptance || row.ce.state)}</td>
                    <td className="px-2 py-1.5 text-center font-mono font-semibold text-text-primary">{formatStrike(row.strike)}</td>
                    <td className={clsx("max-w-[120px] truncate px-2 py-1.5", stateTone(row.pe.acceptance || row.pe.state))}>{stateLabel(row.pe.acceptance || row.pe.state)}</td>
                    <td className={clsx("px-2 py-1.5 text-right font-mono", tone(row.pe.oi_change))}>{signedCompact(row.pe.oi_change)}</td>
                    <td className="px-2 py-1.5 text-right font-mono">{compact(row.pe.oi)}</td>
                    <td className={clsx("px-2 py-1.5 text-right font-mono", tone(row.pe.ltp_change_pct))}>
                      {formatNumber(row.pe.ltp, 2)}
                      <span className="ml-1 text-[9.5px] text-text-muted">{row.pe.ltp_change_pct != null ? formatPct(row.pe.ltp_change_pct, 1, { asPercent: true }) : ""}</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Section>
      ) : null}

      {unusualRows.length ? (
        <Section title="Unusual activity" icon={<TrendingUp size={16} />}>
          <div className="overflow-x-auto">
            <table className="min-w-full text-[11.5px]">
              <thead className="text-[10px] uppercase tracking-wide text-text-muted">
                <tr className="border-b border-bg-border/40">
                  <th className="px-2 py-1.5 text-left">Strike</th>
                  <th className="px-2 py-1.5 text-left">Side</th>
                  <th className="px-2 py-1.5 text-right">Vol/OI</th>
                  <th className="px-2 py-1.5 text-right">ΔOI</th>
                  <th className="px-2 py-1.5 text-right">ΔLTP</th>
                  <th className="px-2 py-1.5 text-left">Flags</th>
                </tr>
              </thead>
              <tbody>
                {unusualRows.map((r) => (
                  <tr key={`ua-${r.option_type}-${r.strike}`} className="border-b border-bg-border/20">
                    <td className="px-2 py-1.5 font-mono text-text-primary">{formatStrike(r.strike)}</td>
                    <td className={clsx("px-2 py-1.5 font-semibold", r.option_type === "CE" ? "text-accent-green" : "text-accent-red")}>{r.option_type}</td>
                    <td className="px-2 py-1.5 text-right font-mono">{formatNumber(r.volume_to_oi, 2)}</td>
                    <td className={clsx("px-2 py-1.5 text-right font-mono", tone(r.oi_change))}>
                      {compact(r.oi_change)}
                      {r.oi_change_pct != null ? ` (${formatPct(r.oi_change_pct, 1, { asPercent: true })})` : ""}
                    </td>
                    <td className={clsx("px-2 py-1.5 text-right font-mono", tone(r.ltp_change_pct))}>
                      {r.ltp_change_pct != null ? formatPct(r.ltp_change_pct, 1, { asPercent: true }) : "—"}
                    </td>
                    <td className="px-2 py-1.5 text-text-muted">{r.flags.join(" · ") || formatNumber(r.score, 1)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Section>
      ) : null}

      {data.top_ce_oi && data.top_pe_oi ? (
        <div className="grid gap-4 lg:grid-cols-2">
          <Section title="Top CE strikes by OI">
            <table className="w-full text-[12px]">
              <thead className="text-[10.5px] uppercase tracking-wide text-text-muted">
                <tr className="border-b border-bg-border/40">
                  <th className="px-2 py-1.5 text-left">Strike</th>
                  <th className="px-2 py-1.5 text-right">OI</th>
                  <th className="px-2 py-1.5 text-right">% from spot</th>
                </tr>
              </thead>
              <tbody>
                {data.top_ce_oi.map((r) => (
                  <tr key={`ce-${r.strike}`} className="border-b border-bg-border/20">
                    <td className="px-2 py-1.5 font-mono">{r.strike}</td>
                    <td className="px-2 py-1.5 text-right font-mono">{compact(r.oi)}</td>
                    <td className={clsx("px-2 py-1.5 text-right font-mono", tone(r.moneyness_pct))}>
                      {((r.moneyness_pct ?? 0) * 100).toFixed(2)}%
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Section>
          <Section title="Top PE strikes by OI">
            <table className="w-full text-[12px]">
              <thead className="text-[10.5px] uppercase tracking-wide text-text-muted">
                <tr className="border-b border-bg-border/40">
                  <th className="px-2 py-1.5 text-left">Strike</th>
                  <th className="px-2 py-1.5 text-right">OI</th>
                  <th className="px-2 py-1.5 text-right">% from spot</th>
                </tr>
              </thead>
              <tbody>
                {data.top_pe_oi.map((r) => (
                  <tr key={`pe-${r.strike}`} className="border-b border-bg-border/20">
                    <td className="px-2 py-1.5 font-mono">{r.strike}</td>
                    <td className="px-2 py-1.5 text-right font-mono">{compact(r.oi)}</td>
                    <td className={clsx("px-2 py-1.5 text-right font-mono", tone(-(r.moneyness_pct ?? 0)))}>
                      {((r.moneyness_pct ?? 0) * 100).toFixed(2)}%
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Section>
        </div>
      ) : null}
    </div>
  );
}

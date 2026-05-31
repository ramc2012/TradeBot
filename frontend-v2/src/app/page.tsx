"use client";

/**
 * Overview — the v2 landing page.
 *
 * Two purposes:
 *   1. Cross-lane status at a glance (equity / open positions / risk).
 *   2. Quick-navigation chips into every strategy desk.
 *
 * Until the full SystemOverview API/component is ported, the page
 * surfaces a thin slice (paper-summary across desks) and links into
 * the deeper v1 surfaces for anything not yet rebuilt here.
 */
import Link from "next/link";
import { useQueries } from "@tanstack/react-query";
import { ArrowUpRight, Banknote, Brain, Compass, Crosshair, Fingerprint, Layers3, Radar, Target, Waves } from "lucide-react";

import { MetricTile, REFRESH_MS, Section, formatSignedMoney, tone } from "@/components/desk-ui";
import { api as apiClient } from "@/lib/api";

const STRATEGY_CARDS = [
  { href: "/strategies/nse",         label: "NSE Index",     icon: Crosshair,   summaryEp: "/api/strategy/paper-summary"                  },
  { href: "/strategies/cbe",         label: "CBE Scanner",   icon: Radar,       summaryEp: "/api/cbe/paper-summary"                       },
  { href: "/strategies/directional", label: "Long Premium",  icon: Target,      summaryEp: "/api/directional-options/paper-summary"      },
  { href: "/strategies/gann",        label: "Gann TP Delta", icon: Compass,     summaryEp: "/api/gann-tp-delta/paper-summary"            },
  { href: "/strategies/commodity",   label: "Commodity",     icon: Waves,       summaryEp: "/api/commodity-strategy/paper-summary"       },
  { href: "/strategies/auction",     label: "Auction IQ",    icon: Layers3,     summaryEp: "/api/auction-intelligence/paper-summary"     },
  { href: "/strategies/fractal",     label: "Fractal MP",    icon: Fingerprint, summaryEp: "/api/fractal-market-profile/paper-summary"   },
  { href: "/strategies/mp",          label: "MP Live",       icon: Brain,       summaryEp: "/api/mp-intelligence/paper-summary"          },
];

type PaperSummary = {
  total_equity?: number;
  realized_pnl?: number;
  unrealized_pnl?: number;
  open_positions?: number;
  total_trades?: number;
  win_rate?: number;
};

export default function OverviewPage() {
  const queries = useQueries({
    queries: STRATEGY_CARDS.map(({ href, summaryEp }) => ({
      queryKey: ["overview", "paper-summary", href],
      queryFn: async () => {
        try {
          return (await apiClient.get(summaryEp)).data as PaperSummary;
        } catch {
          return null;
        }
      },
      refetchInterval: REFRESH_MS.summary,
      refetchOnWindowFocus: false,
    })),
  });

  const totalRealized = queries.reduce((acc, q) => acc + Number(q.data?.realized_pnl || 0), 0);
  const totalUnrealized = queries.reduce((acc, q) => acc + Number(q.data?.unrealized_pnl || 0), 0);
  const totalOpen = queries.reduce((acc, q) => acc + Number(q.data?.open_positions || 0), 0);
  const totalEquity = queries.reduce((acc, q) => acc + Number(q.data?.total_equity || 0), 0);

  return (
    <div className="space-y-4">
      <header>
        <h1 className="text-2xl font-semibold text-text-primary">Overview</h1>
        <p className="mt-1 text-sm text-text-muted">
          Cross-lane snapshot. Click a strategy below to jump into its desk.
        </p>
      </header>

      <Section title="Cross-lane book" icon={<Banknote size={16} />}>
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <MetricTile label="Total equity" value={`₹${totalEquity.toLocaleString("en-IN", { maximumFractionDigits: 0 })}`} />
          <MetricTile label="Realized P&L" value={formatSignedMoney(totalRealized)} color={tone(totalRealized)} />
          <MetricTile label="Unrealized P&L" value={formatSignedMoney(totalUnrealized)} color={tone(totalUnrealized)} />
          <MetricTile label="Open positions" value={String(totalOpen)} detail={`${queries.filter((q) => q.data).length} of ${queries.length} desks reporting`} />
        </div>
      </Section>

      <Section title="Strategy desks" icon={<Target size={16} />}>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {STRATEGY_CARDS.map(({ href, label, icon: Icon }, i) => {
            const data = queries[i].data;
            const realized = data?.realized_pnl || 0;
            const unreal = data?.unrealized_pnl || 0;
            const total = realized + unreal;
            return (
              <Link
                key={href}
                href={href}
                className="group rounded-2xl border border-bg-border bg-bg-primary/16 p-4 transition-colors hover:border-accent-blue/40"
              >
                <div className="flex items-start justify-between gap-2">
                  <div className="flex items-center gap-2">
                    <div className="rounded-lg border border-bg-border bg-bg-secondary/35 p-1.5 text-text-secondary group-hover:border-accent-blue/30 group-hover:text-accent-blue">
                      <Icon size={14} />
                    </div>
                    <div className="font-semibold text-text-primary">{label}</div>
                  </div>
                  <ArrowUpRight size={14} className="text-text-muted group-hover:text-accent-blue" />
                </div>
                <div className="mt-3 grid grid-cols-2 gap-2 text-[11.5px]">
                  <div>
                    <div className="text-text-muted">Total P&L</div>
                    <div className={`mt-0.5 font-mono font-semibold ${tone(total)}`}>{data ? formatSignedMoney(total) : "—"}</div>
                  </div>
                  <div>
                    <div className="text-text-muted">Open</div>
                    <div className="mt-0.5 font-mono font-semibold text-text-primary">{data?.open_positions ?? "—"}</div>
                  </div>
                </div>
              </Link>
            );
          })}
        </div>
      </Section>

      <Section title="Quick links">
        <div className="grid grid-cols-2 gap-2 text-sm md:grid-cols-4">
          {[
            { href: "/trading", label: "Execution" },
            { href: "/positions", label: "Positions" },
            { href: "/market", label: "Market" },
            { href: "/charts", label: "Charts" },
            { href: "/orderflow", label: "Orderflow" },
            { href: "/research", label: "Research" },
            { href: "/system", label: "System health" },
            { href: "/settings", label: "Settings" },
          ].map(({ href, label }) => (
            <Link
              key={href}
              href={href}
              className="rounded-lg border border-bg-border bg-bg-primary/15 px-3 py-2 text-text-secondary transition-colors hover:border-accent-blue/30 hover:text-text-primary"
            >
              {label}
            </Link>
          ))}
        </div>
      </Section>
    </div>
  );
}

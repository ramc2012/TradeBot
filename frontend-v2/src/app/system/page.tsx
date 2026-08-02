"use client";

/**
 * /system — the single system surface. The old /health and /lane-health
 * routes redirect here (?tab=services / ?tab=lanes).
 *
 * Tabs:
 *   - services: risk/blocker overview (/api/system/overview) + the full
 *               ServiceHealthBoard (/api/system/health). The board is the
 *               ONE services table — the previous second table rendered
 *               from /api/system/overview was dropped as a duplicate.
 *   - lanes:    lane-audit invariants (LaneHealthBoard)
 *   - brokers:  managed under /settings; this tab links there
 *   - budgets:  Upstox API budget card
 */
import { useQuery } from "@tanstack/react-query";
import { Link as LinkIcon, ServerCog, ShieldAlert, TrendingUp, Wallet } from "lucide-react";
import Link from "next/link";
import { clsx } from "clsx";

import { MetricTile, REFRESH_MS, Section, formatIST, useUrlTab } from "@/components/desk-ui";
import { api as apiClient } from "@/lib/api";
import ServiceHealthBoard from "@/components/system/ServiceHealthBoard";
import LaneHealthBoard from "@/components/system/LaneHealthBoard";
import UpstoxBudgetCard from "@/components/system/UpstoxBudgetCard";

const TABS = [
  { key: "services", label: "Services", icon: ServerCog },
  { key: "lanes",    label: "Lane invariants", icon: ShieldAlert },
  { key: "brokers",  label: "Brokers",   icon: LinkIcon },
  { key: "budgets",  label: "API budgets", icon: Wallet },
];

type SystemOverview = {
  generated_at?: string;
  health?: { services?: Array<{ key?: string; label?: string; status?: string; detail?: string; meta?: Record<string, unknown> }> };
  books?: { combined?: { equity?: number; realized_pnl?: number; open_pnl?: number; open_positions?: number } };
  risk?: { trading_allowed?: boolean; open_positions?: number; max_positions?: number; daily_loss?: number; max_daily_loss?: number };
  blockers?: Array<{ key?: string; label?: string; status?: string; detail?: string }>;
};

export default function SystemPage() {
  const [tab, setTab] = useUrlTab("services");

  const overviewQuery = useQuery({
    queryKey: ["system-overview"],
    queryFn: async () => {
      try {
        return (await apiClient.get("/api/system/overview")).data as SystemOverview;
      } catch {
        return null;
      }
    },
    refetchInterval: REFRESH_MS.snapshot,
    refetchOnWindowFocus: false,
    enabled: tab === "services",
  });

  const overview = overviewQuery.data;
  const blockers = overview?.blockers || [];

  return (
    <div className="space-y-4">
      <header>
        <h1 className="text-2xl font-semibold text-text-primary">System</h1>
        <p className="mt-1 text-sm text-text-muted">
          Health, lane invariants, broker connectivity, and API budgets.
          <code>/health</code> and <code>/lane-health</code> both redirect into this page.
        </p>
      </header>

      <nav className="flex flex-wrap items-center gap-1 border-b border-bg-border/40">
        {TABS.map(({ key, label, icon: Icon }) => (
          <button
            key={key}
            type="button"
            onClick={() => setTab(key)}
            className={clsx(
              "inline-flex items-center gap-1.5 rounded-t-lg border-b-2 px-3 py-2 text-[12.5px] font-semibold transition-colors",
              tab === key ? "border-accent-blue text-text-primary" : "border-transparent text-text-muted hover:text-text-secondary",
            )}
          >
            <Icon size={14} />
            {label}
          </button>
        ))}
      </nav>

      {tab === "services" ? (
        <div className="space-y-4">
          <Section title="Overview" icon={<TrendingUp size={16} />} rightSlot={<span className="text-[11px] text-text-muted">{overview?.generated_at ? `Generated ${formatIST(overview.generated_at)}` : "—"}</span>}>
            <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
              <MetricTile label="Trading allowed" value={overview?.risk?.trading_allowed ? "YES" : "NO"} color={overview?.risk?.trading_allowed ? "text-accent-green" : "text-accent-red"} />
              <MetricTile label="Open positions" value={`${overview?.risk?.open_positions ?? "—"} / ${overview?.risk?.max_positions ?? "—"}`} />
              <MetricTile label="Daily loss" value={overview?.risk?.daily_loss != null ? `₹${overview.risk.daily_loss.toLocaleString("en-IN")}` : "—"} detail={overview?.risk?.max_daily_loss != null ? `max ₹${overview.risk.max_daily_loss.toLocaleString("en-IN")}` : ""} />
              <MetricTile label="Blockers" value={String(blockers.length)} detail={blockers.length === 0 ? "All clear" : "See list"} color={blockers.length === 0 ? "text-accent-green" : "text-accent-amber"} />
            </div>
          </Section>

          {blockers.length > 0 ? (
            <Section title="Blockers" icon={<ShieldAlert size={16} />}>
              <ul className="space-y-1.5">
                {blockers.map((b, i) => (
                  <li key={i} className="rounded-lg border border-accent-amber/30 bg-accent-amber/8 p-2 text-[12px]">
                    <div className="font-semibold text-accent-amber">{b.label || b.key}</div>
                    <div className="mt-0.5 text-text-secondary">{b.detail}</div>
                  </li>
                ))}
              </ul>
            </Section>
          ) : null}

          <ServiceHealthBoard showBudget={false} />
        </div>
      ) : null}

      {tab === "lanes" ? <LaneHealthBoard /> : null}
      {tab === "brokers" ? (
        <Section title="Broker connectivity">
          <p className="text-sm text-text-secondary">Brokers are managed under Settings.</p>
          <Link href="/settings" className="mt-2 inline-block rounded-lg border border-accent-blue/35 bg-accent-blue/10 px-3 py-2 text-sm font-semibold text-accent-blue hover:border-accent-blue/55">
            Open Settings →
          </Link>
        </Section>
      ) : null}
      {tab === "budgets" ? <UpstoxBudgetCard /> : null}
    </div>
  );
}

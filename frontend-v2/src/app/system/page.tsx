"use client";

/**
 * /system — consolidates v1's /health and /lane-health into one
 * tabbed surface. The v1 redirect map sends both old URLs here.
 *
 * Tabs:
 *   - services: live system services health (broker, db, redis, paper engines)
 *   - lanes:    invariants table (data integrity, replay parity, gate attribution)
 *   - brokers:  broker connectivity status (per-broker tokens, rate limits)
 *   - budgets:  Upstox API budget card
 *
 * Each tab here renders a thin client that calls the same backend
 * endpoints v1 used (/api/system/overview, /api/lane-health, etc.).
 * Until those clients are written we stub with links into v1.
 */
import { useQuery } from "@tanstack/react-query";
import { Link as LinkIcon, ServerCog, ShieldAlert, TrendingUp, Wallet } from "lucide-react";
import Link from "next/link";
import { clsx } from "clsx";

import { MetricTile, REFRESH_MS, Section, formatIST, serviceStateTone, useUrlTab } from "@/components/desk-ui";
import { api as apiClient } from "@/lib/api";
import LaneHealthEmbed from "@/app/lane-health/page";
import HealthEmbed from "@/app/health/page";

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
  });

  const overview = overviewQuery.data;
  const services = overview?.health?.services || [];
  const blockers = overview?.blockers || [];

  return (
    <div className="space-y-4">
      <header>
        <h1 className="text-2xl font-semibold text-text-primary">System</h1>
        <p className="mt-1 text-sm text-text-muted">
          Health, lane invariants, broker connectivity, and API budgets.
          v1's <code>/health</code> and <code>/lane-health</code> both redirect into this page.
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

          <Section title="Services">
            {services.length === 0 ? (
              <div className="rounded-lg border border-bg-border/40 bg-bg-primary/15 p-3 text-sm text-text-muted">
                No service detail available — backend `/api/system/overview` returned no health block.
              </div>
            ) : (
              <table className="w-full text-[12px]">
                <thead className="text-[10.5px] uppercase tracking-wide text-text-muted">
                  <tr className="border-b border-bg-border/60">
                    <th className="px-2 py-2 text-left">Service</th>
                    <th className="px-2 py-2 text-left">Status</th>
                    <th className="px-2 py-2 text-left">Detail</th>
                  </tr>
                </thead>
                <tbody>
                  {services.map((s, i) => (
                    <tr key={s.key ?? i} className="border-b border-bg-border/30">
                      <td className="px-2 py-2 font-semibold text-text-primary">{s.label ?? s.key ?? "—"}</td>
                      <td className="px-2 py-2">
                        <span className={clsx("rounded-full border px-2 py-0.5 text-[10.5px] font-semibold uppercase", serviceStateTone(s.status))}>{s.status ?? "—"}</span>
                      </td>
                      <td className="px-2 py-2 text-[11px] text-text-secondary">{s.detail ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
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
        </div>
      ) : null}

      {tab === "lanes" ? <LaneHealthEmbed /> : null}
      {tab === "brokers" ? (
        <Section title="Broker connectivity">
          <p className="text-sm text-text-secondary">Brokers are managed under Settings.</p>
          <Link href="/settings" className="mt-2 inline-block rounded-lg border border-accent-blue/35 bg-accent-blue/10 px-3 py-2 text-sm font-semibold text-accent-blue hover:border-accent-blue/55">
            Open Settings →
          </Link>
        </Section>
      ) : null}
      {tab === "budgets" ? <HealthEmbed /> : null}
    </div>
  );
}

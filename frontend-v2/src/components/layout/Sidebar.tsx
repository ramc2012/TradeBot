"use client";

/**
 * v2 sidebar — reorganised around a verb-first mental model.
 *
 * v1 had three groups (Operate / Validate / System) with 18 items that
 * mixed execution, market data, strategy desks, research, and LLM
 * agents. v2 splits those into five groups by what the user is DOING:
 * trading, browsing a strategy desk, watching the market, doing research,
 * or operating the platform.
 *
 * Every desk (NSE / CBE / Directional / Gann / Commodity / Auction /
 * Fractal / MP) lives under /strategies/<name> so the URL prefix tells
 * the trader they're in "the same kind of surface" as they navigate.
 */
import Link from "next/link";
import { usePathname } from "next/navigation";
import { clsx } from "clsx";
import { useEffect, useState } from "react";
import {
  Activity,
  Banknote,
  BarChart3,
  Bot,
  BriefcaseBusiness,
  CandlestickChart,
  ChevronLeft,
  ChevronRight,
  Compass,
  Crosshair,
  FlaskConical,
  Fingerprint,
  Globe,
  Heart,
  LayoutDashboard,
  Layers3,
  Network,
  Radar,
  Settings,
  Target,
  Waves,
  Workflow,
  Brain,
  FileText,
} from "lucide-react";

const SIDEBAR_STORAGE_KEY = "nomad-curie.sidebar.collapsed.v2";

type NavItem = {
  href: string;
  label: string;
  icon: typeof Target;
  matchers?: string[];
};

const NAV_GROUPS: { title: string; subtitle?: string; items: NavItem[] }[] = [
  {
    title: "Trade",
    subtitle: "Active surfaces during market hours",
    items: [
      { href: "/", label: "Overview", icon: LayoutDashboard },
      { href: "/trading", label: "Execution", icon: Activity },
      { href: "/positions", label: "Positions", icon: Banknote },
      { href: "/analytics", label: "Portfolio", icon: BriefcaseBusiness },
    ],
  },
  {
    title: "Strategies",
    subtitle: "One shell per strategy desk",
    items: [
      { href: "/strategies/nse/live",    label: "NSE Index",     icon: Crosshair, matchers: ["/strategies/nse"] },
      { href: "/strategies/cbe",         label: "CBE Scanner",   icon: Radar },
      { href: "/strategies/directional", label: "Long Premium",  icon: Target },
      { href: "/strategies/gann",        label: "Gann TP Delta", icon: Compass },
      { href: "/strategies/commodity",   label: "Commodity",     icon: Waves },
      { href: "/strategies/auction",     label: "Auction IQ",    icon: Layers3 },
      { href: "/strategies/fractal",     label: "Fractal MP",    icon: Fingerprint },
      { href: "/strategies/mp",          label: "MP Live",       icon: Brain },
    ],
  },
  {
    title: "Market",
    subtitle: "Raw market data",
    items: [
      { href: "/market",             label: "Market",         icon: Globe },
      { href: "/charts",             label: "Charts",         icon: CandlestickChart },
      { href: "/orderflow",          label: "Orderflow",      icon: Workflow },
      { href: "/sector-interaction", label: "Sector Network", icon: Network },
    ],
  },
  {
    title: "Research",
    subtitle: "Non-live — backtests, LLM",
    items: [
      { href: "/research",       label: "Research",   icon: FlaskConical, matchers: ["/research", "/analysis", "/backtester", "/data"] },
      { href: "/analysis",       label: "Validation", icon: FlaskConical },
      { href: "/backtester",     label: "Backtester", icon: FlaskConical },
      { href: "/data",           label: "Data ingest",icon: FlaskConical },
      { href: "/macro-research", label: "Macro",      icon: BarChart3 },
      { href: "/agent",          label: "Agent",      icon: Bot },
    ],
  },
  {
    title: "System",
    items: [
      { href: "/reports",     label: "Reports",        icon: FileText },
      { href: "/system",      label: "System hub",     icon: Heart, matchers: ["/system", "/health", "/lane-health"] },
      { href: "/health",      label: "Service health", icon: Heart },
      { href: "/lane-health", label: "Lane invariants",icon: Heart },
      { href: "/settings",    label: "Settings",       icon: Settings },
    ],
  },
];

export default function Sidebar() {
  const pathname = usePathname();
  const [collapsed, setCollapsed] = useState(false);

  useEffect(() => {
    try {
      setCollapsed(window.localStorage.getItem(SIDEBAR_STORAGE_KEY) === "1");
    } catch {}
  }, []);

  const toggleCollapsed = () => {
    setCollapsed((current) => {
      const next = !current;
      try {
        window.localStorage.setItem(SIDEBAR_STORAGE_KEY, next ? "1" : "0");
      } catch {}
      return next;
    });
  };

  return (
    <nav
      className={clsx(
        "relative z-20 h-full shrink-0 overflow-y-auto border-r border-bg-border bg-bg-secondary/70 px-2 py-2.5 backdrop-blur-sm transition-[width] duration-200",
        collapsed ? "w-[58px]" : "w-[212px]",
      )}
    >
      <div className={clsx("mb-2 flex items-start gap-2", collapsed ? "flex-col items-center" : "justify-between")}>
        <div className={collapsed ? "text-center" : undefined}>
          <div className="text-[10px] uppercase tracking-[0.18em] text-text-muted">
            {collapsed ? "NC" : "Nomad Curie"}
          </div>
          {!collapsed ? (
            <div className="mt-0.5 flex items-center gap-1.5">
              <div className="text-xs font-semibold text-text-primary">Trader Workspace</div>
              <span className="v2-badge rounded px-1 py-0 text-[9px] font-bold uppercase tracking-[0.14em]">v2</span>
            </div>
          ) : null}
        </div>
        <button
          type="button"
          onClick={toggleCollapsed}
          className="rounded-lg border border-bg-border bg-bg-primary/30 p-1.5 text-text-secondary transition-colors hover:border-bg-active hover:text-text-primary"
          aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          title={collapsed ? "Expand sidebar" : "Collapse sidebar"}
        >
          {collapsed ? <ChevronRight size={14} /> : <ChevronLeft size={14} />}
        </button>
      </div>

      <div className="space-y-3">
        {NAV_GROUPS.map((group) => (
          <div key={group.title}>
            {collapsed ? (
              <div className="mb-1 border-t border-bg-border/70" />
            ) : (
              <div className="px-2">
                <div className="text-[10px] font-semibold uppercase tracking-[0.12em] text-text-muted">
                  {group.title}
                </div>
                {group.subtitle ? (
                  <div className="text-[10px] text-text-muted/70">{group.subtitle}</div>
                ) : null}
              </div>
            )}
            <div className={clsx("space-y-0.5", collapsed ? "mt-0" : "mt-1")}>
              {group.items.map(({ href, label, icon: Icon, matchers }) => {
                const activeMatchers = matchers || [href];
                const active = activeMatchers.some((matcher) =>
                  matcher === "/" ? pathname === "/" : pathname === matcher || pathname.startsWith(`${matcher}/`),
                );
                return (
                  <Link
                    key={href}
                    href={href}
                    title={label}
                    className={clsx(
                      "group relative flex rounded-lg text-xs transition-colors",
                      collapsed ? "justify-center px-0 py-1.5" : "items-center gap-2 px-2 py-1.5",
                      active
                        ? "bg-accent-blue/14 text-accent-blue"
                        : "text-text-secondary hover:bg-bg-hover hover:text-text-primary",
                    )}
                  >
                    <div
                      className={clsx(
                        "flex h-7 w-7 items-center justify-center rounded-md border transition-colors",
                        active
                          ? "border-accent-blue/35 bg-accent-blue/12"
                          : "border-transparent bg-bg-primary/25 group-hover:border-bg-active",
                      )}
                    >
                      <Icon size={15} />
                    </div>
                    {!collapsed ? (
                      <>
                        <span className="truncate">{label}</span>
                        {active ? <span className="ml-auto h-1.5 w-1.5 rounded-full bg-accent-blue" /> : null}
                      </>
                    ) : (
                      <span className="pointer-events-none absolute left-full top-1/2 z-20 ml-2 -translate-y-1/2 whitespace-nowrap rounded-lg border border-bg-border bg-bg-card px-2 py-1 text-[11px] text-text-primary opacity-0 shadow-lg transition-opacity group-hover:opacity-100">
                        {label}
                      </span>
                    )}
                  </Link>
                );
              })}
            </div>
          </div>
        ))}
      </div>
    </nav>
  );
}

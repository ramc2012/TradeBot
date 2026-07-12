"use client";

/**
 * v2 sidebar — consolidated, function-first, collapsible groups.
 *
 * Reduces the left rail two ways:
 *  1. Removes entries that are already TABS inside a hub page (Validation /
 *     Backtester / Data-ingest live in /research; Service-health /
 *     Lane-invariants live in /system) — the hub is the single menu entry.
 *  2. Collapsible groups: only the group containing the current route is
 *     expanded by default, so the visible menu is short. Manual toggles
 *     persist. Width-collapse (icon rail) still works on top.
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
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Compass,
  Crosshair,
  FileText,
  // Fingerprint — only used by the parked Fractal MP nav entry (see below).
  FlaskConical,
  Globe,
  Inbox,
  LayoutDashboard,
  Layers3,
  Network,
  Radar,
  Server,
  Settings,
  Sigma,
  Target,
  Waves,
  Workflow,
} from "lucide-react";

const SIDEBAR_STORAGE_KEY = "nomad-curie.sidebar.collapsed.v2";
const GROUPS_STORAGE_KEY = "nomad-curie.sidebar.groups.v2";

type NavItem = { href: string; label: string; icon: typeof Target; matchers?: string[] };
type NavGroup = { title: string; icon: typeof Target; items: NavItem[] };

const NAV_GROUPS: NavGroup[] = [
  {
    title: "Trade",
    icon: Banknote,
    items: [
      { href: "/", label: "Overview", icon: LayoutDashboard },
      { href: "/trading", label: "Execution", icon: Activity },
      { href: "/positions", label: "Positions", icon: Banknote },
      { href: "/proposals", label: "Proposals", icon: Inbox },
      { href: "/analytics", label: "Portfolio", icon: BriefcaseBusiness },
    ],
  },
  {
    title: "Strategy desks",
    icon: Target,
    items: [
      { href: "/strategies/overview", label: "Overview", icon: LayoutDashboard },
      { href: "/strategies/nse/live", label: "MACD Strategy", icon: Crosshair, matchers: ["/strategies/nse"] },
      { href: "/strategies/macd-refined", label: "MACD Refined", icon: BarChart3, matchers: ["/strategies/macd-refined"] },
      { href: "/strategies/us-macd-refined", label: "US MACD Refined", icon: Globe, matchers: ["/strategies/us-macd-refined"] },
      { href: "/strategies/directional", label: "Long Premium", icon: Target },
      { href: "/strategies/auction", label: "Auction IQ", icon: Layers3 },
      // Fractal MP (FMP) parked out of production 2026-07-07 — revisit later.
      // Page + component preserved on disk; only the nav link is hidden.
      // { href: "/strategies/fractal", label: "Fractal MP", icon: Fingerprint },
      { href: "/strategies/gann", label: "Gann TP Delta", icon: Compass },
      { href: "/strategies/mp", label: "MP Live", icon: Sigma },
      { href: "/strategies/cbe", label: "CBE Scanner", icon: Radar },
      { href: "/strategies/institutional-convergence", label: "Convergence", icon: Network },
      { href: "/strategies/commodity", label: "Commodity", icon: Waves },
      // Sniper parked out of production 2026-07-07 — revisit later.
      // Page + component preserved on disk; only the nav link is hidden.
      // { href: "/strategies/sniper", label: "Sniper", icon: Crosshair },
    ],
  },
  {
    title: "Markets",
    icon: Globe,
    items: [
      { href: "/market", label: "Option chain", icon: Globe },
      { href: "/charts", label: "Charts", icon: CandlestickChart },
      { href: "/orderflow", label: "Orderflow", icon: Workflow },
      { href: "/sector-interaction", label: "Sector network", icon: Network },
      { href: "/macro-research", label: "Macro", icon: BarChart3 },
    ],
  },
  {
    title: "Research",
    icon: FlaskConical,
    items: [
      { href: "/research", label: "Research lab", icon: FlaskConical, matchers: ["/research", "/analysis", "/backtester", "/data"] },
      { href: "/agent", label: "AI agent", icon: Bot },
    ],
  },
  {
    title: "Platform",
    icon: Server,
    items: [
      { href: "/system", label: "System hub", icon: Server, matchers: ["/system", "/health", "/lane-health"] },
      { href: "/reports", label: "Reports", icon: FileText },
      { href: "/settings", label: "Settings", icon: Settings },
    ],
  },
];

function matchesItem(item: NavItem, pathname: string): boolean {
  const ms = item.matchers || [item.href];
  return ms.some((m) => (m === "/" ? pathname === "/" : pathname === m || pathname.startsWith(`${m}/`)));
}

export default function Sidebar() {
  const pathname = usePathname();
  const [collapsed, setCollapsed] = useState(false);
  const [overrides, setOverrides] = useState<Record<string, boolean>>({});

  useEffect(() => {
    try {
      setCollapsed(window.localStorage.getItem(SIDEBAR_STORAGE_KEY) === "1");
      setOverrides(JSON.parse(window.localStorage.getItem(GROUPS_STORAGE_KEY) || "{}"));
    } catch {
      /* ignore */
    }
  }, []);

  const activeGroup = NAV_GROUPS.find((g) => g.items.some((it) => matchesItem(it, pathname)))?.title ?? NAV_GROUPS[0].title;
  const isGroupOpen = (title: string) => (collapsed ? true : title in overrides ? overrides[title] : title === activeGroup);

  const toggleCollapsed = () => {
    setCollapsed((c) => {
      const next = !c;
      try {
        window.localStorage.setItem(SIDEBAR_STORAGE_KEY, next ? "1" : "0");
      } catch {
        /* ignore */
      }
      return next;
    });
  };

  const toggleGroup = (title: string) => {
    setOverrides((prev) => {
      const next = { ...prev, [title]: !isGroupOpen(title) };
      try {
        window.localStorage.setItem(GROUPS_STORAGE_KEY, JSON.stringify(next));
      } catch {
        /* ignore */
      }
      return next;
    });
  };

  return (
    <nav
      className={clsx(
        "relative z-20 h-full shrink-0 overflow-y-auto border-r border-bg-border bg-bg-secondary/70 px-2 py-2.5 backdrop-blur-sm transition-[width] duration-200",
        collapsed ? "w-[58px]" : "w-[208px]",
      )}
    >
      <div className={clsx("mb-2 flex items-start gap-2", collapsed ? "flex-col items-center" : "justify-between")}>
        <div className={collapsed ? "text-center" : undefined}>
          <div className="text-[10px] uppercase tracking-[0.18em] text-text-muted">{collapsed ? "NC" : "Nomad Curie"}</div>
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

      <div className="space-y-1.5">
        {NAV_GROUPS.map((group) => {
          const open = isGroupOpen(group.title);
          return (
            <div key={group.title}>
              {collapsed ? (
                <div className="my-1 border-t border-bg-border/70" />
              ) : (
                <button
                  type="button"
                  onClick={() => toggleGroup(group.title)}
                  className="flex w-full items-center justify-between rounded-md px-2 py-1 text-left text-text-muted transition-colors hover:text-text-secondary"
                >
                  <span className="text-[10px] font-semibold uppercase tracking-[0.12em]">{group.title}</span>
                  <ChevronDown size={12} className={clsx("transition-transform", open ? "" : "-rotate-90")} />
                </button>
              )}
              {open ? (
                <div className={clsx("space-y-0.5", collapsed ? "mt-0" : "mt-0.5")}>
                  {group.items.map(({ href, label, icon: Icon, matchers }) => {
                    const active = matchesItem({ href, label, icon: Icon, matchers }, pathname);
                    return (
                      <Link
                        key={href}
                        href={href}
                        title={label}
                        className={clsx(
                          "group relative flex rounded-lg text-xs transition-colors",
                          collapsed ? "justify-center px-0 py-1.5" : "items-center gap-2 px-2 py-1.5",
                          active ? "bg-accent-blue/14 text-accent-blue" : "text-text-secondary hover:bg-bg-hover hover:text-text-primary",
                        )}
                      >
                        <div
                          className={clsx(
                            "flex h-7 w-7 items-center justify-center rounded-md border transition-colors",
                            active ? "border-accent-blue/35 bg-accent-blue/12" : "border-transparent bg-bg-primary/25 group-hover:border-bg-active",
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
              ) : null}
            </div>
          );
        })}
      </div>
    </nav>
  );
}

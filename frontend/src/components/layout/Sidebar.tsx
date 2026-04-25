"use client";

import { usePathname } from "next/navigation";
import { clsx } from "clsx";
import { useEffect, useState } from "react";
import {
  Activity,
  BarChart3,
  Bot,
  Brain,
  BriefcaseBusiness,
  ChevronLeft,
  ChevronRight,
  Crosshair,
  FlaskConical,
  Fingerprint,
  Globe,
  LayoutDashboard,
  Layers3,
  Settings,
  Target,
  Waves,
} from "lucide-react";

const SIDEBAR_STORAGE_KEY = "nomad-curie.sidebar.collapsed";

const NAV_GROUPS = [
  {
    title: "Operate",
    items: [
      { href: "/", label: "Overview", icon: LayoutDashboard },
      { href: "/analytics", label: "Portfolio", icon: BriefcaseBusiness },
      { href: "/trading", label: "Execution", icon: Activity },
      { href: "/market", label: "Market", icon: Globe },
      { href: "/strategy", label: "NSE Desk", icon: Crosshair },
      { href: "/directional-options", label: "Long Premium", icon: Target },
      { href: "/commodity", label: "Commodity", icon: Waves },
    ],
  },
  {
    title: "Validate",
    items: [
      { href: "/auction-intelligence", label: "Auction IQ", icon: Layers3 },
      { href: "/fractal-market-profile", label: "Fractal MP", icon: Fingerprint },
      { href: "/mp-intelligence", label: "MP Live", icon: Brain },
      { href: "/macro-research", label: "Macro Research", icon: BarChart3 },
      { href: "/analysis", label: "Research", icon: FlaskConical, matchers: ["/analysis", "/backtester", "/data"] },
      { href: "/agent", label: "Agent", icon: Bot },
    ],
  },
  {
    title: "System",
    items: [
      { href: "/settings", label: "Settings", icon: Settings, matchers: ["/settings", "/health"] },
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
        "relative z-20 h-full shrink-0 overflow-y-auto border-r border-bg-border bg-bg-secondary/70 px-3 py-4 backdrop-blur-sm transition-[width] duration-200",
        collapsed ? "w-[78px]" : "w-[236px]",
      )}
    >
      <div className={clsx("mb-5 flex items-start gap-2", collapsed ? "flex-col items-center" : "justify-between")}>
        <div className={clsx(collapsed ? "text-center" : "")}>
          <div className="text-[10px] uppercase tracking-[0.24em] text-text-muted">{collapsed ? "NC" : "Nomad Curie"}</div>
          {!collapsed ? <div className="mt-1 text-sm font-semibold text-text-primary">Trader Workspace</div> : null}
        </div>
        <button
          type="button"
          onClick={toggleCollapsed}
          className="rounded-xl border border-bg-border bg-bg-primary/30 p-2 text-text-secondary transition-colors hover:border-bg-active hover:text-text-primary"
          aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          title={collapsed ? "Expand sidebar" : "Collapse sidebar"}
        >
          {collapsed ? <ChevronRight size={14} /> : <ChevronLeft size={14} />}
        </button>
      </div>

      <div className="space-y-5">
        {NAV_GROUPS.map((group) => (
          <div key={group.title}>
            {collapsed ? (
              <div className="mb-2 border-t border-bg-border/70" />
            ) : (
              <div className="px-2 text-[11px] font-semibold uppercase tracking-[0.16em] text-text-muted">
                {group.title}
              </div>
            )}
            <div className={clsx("mt-2 space-y-1", collapsed ? "mt-0" : "")}>
              {group.items.map(({ href, label, icon: Icon, matchers }) => {
                const activeMatchers = matchers || [href];
                const active = activeMatchers.some((matcher) =>
                  matcher === "/"
                    ? pathname === "/"
                    : pathname === matcher || pathname.startsWith(`${matcher}/`) || pathname.startsWith(matcher),
                );
                return (
                  <a
                    key={href}
                    href={href}
                    title={label}
                    data-shell-nav="true"
                    className={clsx(
                      "group relative flex rounded-xl text-sm transition-colors",
                      collapsed ? "justify-center px-0 py-2.5" : "items-center gap-3 px-3 py-2.5",
                      active
                        ? "bg-accent-blue/14 text-accent-blue"
                        : "text-text-secondary hover:bg-bg-hover hover:text-text-primary",
                    )}
                  >
                    <div
                      className={clsx(
                        "flex h-8 w-8 items-center justify-center rounded-lg border transition-colors",
                        active
                          ? "border-accent-blue/35 bg-accent-blue/12"
                          : "border-transparent bg-bg-primary/25 group-hover:border-bg-active",
                      )}
                    >
                      <Icon size={16} />
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
                  </a>
                );
              })}
            </div>
          </div>
        ))}
      </div>
    </nav>
  );
}

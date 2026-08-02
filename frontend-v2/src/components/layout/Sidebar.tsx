"use client";

/**
 * v2 sidebar — SEVEN functional groups (owner-requested 2026-08-02):
 * Overview · Market data · Technical lanes · Auction/MP lanes · Research ·
 * Future lanes · Settings.
 *
 * The grouping is declared in lib/nav-model.ts (SIDEBAR_GROUPS) as a VIEW
 * over the same desk model the landing page reads — the landing page keeps
 * the horizon × policy taxonomy (LANE_SECTIONS) untouched. Desk rows still
 * carry their policy chip, the registry-served KIND caption, and the books
 * child link; the Market Structure workspace keeps its four view sub-links
 * as the first entry of "Market data".
 *
 * "Future lanes" holds Gann, Fractal and Sniper — linked and inspectable,
 * not part of today's production loop. Parked desks stay visibly PARKED.
 *
 * Manual group toggles persist; width-collapse (icon rail) still works.
 */
import Link from "next/link";
import { usePathname } from "next/navigation";
import { clsx } from "clsx";
import { useEffect, useMemo, useState } from "react";
import {
  Activity,
  Banknote,
  BarChart3,
  Bot,
  BriefcaseBusiness,
  CalendarClock,
  CandlestickChart,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Compass,
  Crosshair,
  Fingerprint,
  FlaskConical,
  Globe,
  Grid3x3,
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
  Zap,
} from "lucide-react";

import { useLaneRegistry } from "@/hooks/useLaneRegistry";
import {
  SIDEBAR_GROUPS,
  WORKSPACE_ROUTE,
  WORKSPACE_VIEWS,
  allDesks,
  deskKinds,
  deskPolicyLabel,
  kindShort,
  type NavDesk,
} from "@/lib/nav-model";

const SIDEBAR_STORAGE_KEY = "nomad-curie.sidebar.collapsed.v2";
// v4 key on purpose: v3 overrides refer to the horizon-section ids that no
// longer exist as sidebar groups, and a stale override would hide new groups.
const GROUPS_STORAGE_KEY = "nomad-curie.sidebar.groups.v4";

/** Route → rail icon. Purely decorative; the model carries no icons. */
const ROUTE_ICON: Record<string, typeof Target> = {
  "/": LayoutDashboard,
  "/trading": Activity,
  "/positions": Banknote,
  "/proposals": Inbox,
  "/analytics": BriefcaseBusiness,
  "/market": Globe,
  "/charts": CandlestickChart,
  "/orderflow": Workflow,
  "/sector-interaction": Network,
  "/macro-research": BarChart3,
  "/research": FlaskConical,
  "/agent": Bot,
  "/system": Server,
  "/settings": Settings,
  "/strategies/auction": Layers3,
  "/strategies/mp": Sigma,
  "/strategies/commodity": Waves,
  "/strategies/institutional-convergence": Network,
  // The dual-horizon Long Premium desk: lightning for the weekly-DTE
  // intraday horizon, calendar-clock for the monthly-DTE positional one.
  "/strategies/directional": Zap,
  "/strategies/directional?horizon=positional": CalendarClock,
  "/strategies/nse/live": Crosshair,
  "/strategies/macd-refined": BarChart3,
  "/strategies/gann": Compass,
  "/strategies/cbe": Radar,
  "/strategies/overview": LayoutDashboard,
  "/strategies/fractal": Fingerprint,
  "/strategies/sniper": Crosshair,
};

function matches(href: string, matchers: string[] | undefined, pathname: string): boolean {
  const ms = matchers || [href.split("?")[0]];
  return ms.some((m) => (m === "/" ? pathname === "/" : pathname === m || pathname.startsWith(`${m}/`)));
}

export default function Sidebar() {
  const pathname = usePathname();
  const [collapsed, setCollapsed] = useState(false);
  const [overrides, setOverrides] = useState<Record<string, boolean>>({});

  // The KIND axis is SERVED, never declared here. Shared react-query key, so
  // this dedupes with every other lane-registry consumer rather than adding a
  // poll of its own.
  const registry = useLaneRegistry();
  const kindByLaneKey = useMemo(() => {
    const out: Record<string, string> = {};
    for (const l of registry.data?.lanes ?? []) out[l.key] = String(l.kind || "");
    return out;
  }, [registry.data]);

  const deskByHref = useMemo(() => {
    const out: Record<string, NavDesk> = {};
    for (const d of allDesks()) out[d.href] = d;
    return out;
  }, []);

  useEffect(() => {
    try {
      setCollapsed(window.localStorage.getItem(SIDEBAR_STORAGE_KEY) === "1");
      setOverrides(JSON.parse(window.localStorage.getItem(GROUPS_STORAGE_KEY) || "{}"));
    } catch {
      /* ignore */
    }
  }, []);

  const groupActive = (id: string) => {
    const g = SIDEBAR_GROUPS.find((s) => s.id === id);
    if (!g) return false;
    return g.entries.some((e) => {
      if (e.kind === "workspace") return matches(WORKSPACE_ROUTE, undefined, pathname);
      if (e.kind === "desk") {
        const d = deskByHref[e.href];
        return d ? matches(d.href, d.matchers, pathname) : false;
      }
      return matches(e.href, e.matchers, pathname);
    });
  };

  const isOpen = (key: string, fallback: boolean) =>
    collapsed ? true : key in overrides ? overrides[key] : fallback;

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

  const toggleGroup = (key: string, current: boolean) => {
    setOverrides((prev) => {
      const next = { ...prev, [key]: !current };
      try {
        window.localStorage.setItem(GROUPS_STORAGE_KEY, JSON.stringify(next));
      } catch {
        /* ignore */
      }
      return next;
    });
  };

  const GroupHeader = ({ label, open, onToggle, title }: { label: string; open: boolean; onToggle: () => void; title?: string }) =>
    collapsed ? (
      <div className="my-1 border-t border-bg-border/70" />
    ) : (
      <button
        type="button"
        onClick={onToggle}
        title={title}
        className="flex w-full items-center justify-between rounded-md px-2 py-1 text-left text-text-muted transition-colors hover:text-text-secondary"
      >
        <span className="text-[10px] font-semibold uppercase tracking-[0.12em]">{label}</span>
        <ChevronDown size={12} className={clsx("transition-transform", open ? "" : "-rotate-90")} />
      </button>
    );

  const RailLink = ({
    href,
    label,
    icon: Icon,
    active,
    title,
    caption,
    chip,
    muted,
  }: {
    href: string;
    label: string;
    icon: typeof Target;
    active: boolean;
    title?: string;
    caption?: string | null;
    chip?: string | null;
    muted?: boolean;
  }) => (
    <Link
      href={href}
      title={title ?? label}
      className={clsx(
        "group relative flex rounded-lg text-xs transition-colors",
        collapsed ? "justify-center px-0 py-1.5" : "items-start gap-2 px-2 py-1.5",
        active
          ? "bg-accent-blue/14 text-accent-blue"
          : muted
            ? "text-text-muted hover:bg-bg-hover hover:text-text-secondary"
            : "text-text-secondary hover:bg-bg-hover hover:text-text-primary",
      )}
    >
      <div
        className={clsx(
          "mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-md border transition-colors",
          active ? "border-accent-blue/35 bg-accent-blue/12" : "border-transparent bg-bg-primary/25 group-hover:border-bg-active",
        )}
      >
        <Icon size={15} />
      </div>
      {!collapsed ? (
        <span className="min-w-0 flex-1">
          <span className="flex items-center gap-1">
            <span className="truncate">{label}</span>
            {active ? <span className="ml-auto h-1.5 w-1.5 shrink-0 rounded-full bg-accent-blue" /> : null}
          </span>
          {chip ? (
            <span className="mt-0.5 block truncate text-[9px] font-semibold uppercase tracking-[0.1em] text-accent-blue/70">{chip}</span>
          ) : null}
          {caption ? <span className="mt-0.5 block truncate text-[9px] text-text-muted">{caption}</span> : null}
        </span>
      ) : (
        <span className="pointer-events-none absolute left-full top-1/2 z-20 ml-2 -translate-y-1/2 whitespace-nowrap rounded-lg border border-bg-border bg-bg-card px-2 py-1 text-[11px] text-text-primary opacity-0 shadow-lg transition-opacity group-hover:opacity-100">
          {label}
        </span>
      )}
    </Link>
  );

  const deskCaption = (desk: NavDesk): string => {
    const k = deskKinds(desk, kindByLaneKey);
    if (desk.laneKeys.length === 0) return "no registry lane";
    if (k.registryUnavailable) return "kind unavailable";
    if (k.kinds.length === 0) return `kind unserved (${k.unresolved.length})`;
    return k.kinds.map(kindShort).join(" · ");
  };

  const DeskRow = ({ desk }: { desk: NavDesk }) => (
    <div>
      <RailLink
        href={desk.href}
        label={desk.label}
        icon={ROUTE_ICON[desk.href] ?? Target}
        active={matches(desk.href, desk.matchers, pathname)}
        muted={desk.status === "parked"}
        title={
          desk.status === "parked"
            ? `${desk.parkedReason}\n\n${desk.note}`
            : `${desk.note}\n\nLanes: ${desk.laneKeys.join(", ") || "none in the registry"}`
        }
        chip={deskPolicyLabel(desk)}
        caption={desk.status === "parked" ? `PARKED · ${deskCaption(desk)}` : deskCaption(desk)}
      />
      {/* The BOOKS page as an indented child so the route stays findable. */}
      {desk.books && !collapsed ? (
        <div className="ml-9 border-l border-bg-border/60 pl-2">
          <Link
            href={desk.books.href}
            title={desk.books.blurb}
            className={clsx(
              "block truncate rounded px-1.5 py-1 text-[11px] transition-colors hover:bg-bg-hover hover:text-text-primary",
              pathname.startsWith(desk.books.href) ? "text-accent-blue" : "text-text-muted",
            )}
          >
            {desk.books.label}
            <span className="ml-1 text-[9px] text-text-muted/70">{desk.books.views.join(" · ")}</span>
          </Link>
        </div>
      ) : null}
    </div>
  );

  // Workspace: the primary destination — first entry of "Market data", with
  // its BUILT views linked directly underneath.
  const WorkspaceRow = () => (
    <div>
      <RailLink
        href={WORKSPACE_ROUTE}
        label="Market Structure"
        icon={Grid3x3}
        active={matches(WORKSPACE_ROUTE, undefined, pathname)}
        title="Market Structure workspace — the cross-lane command surface. One pinned instrument, every lane's read on it."
        caption="cross-lane · one pin"
      />
      {!collapsed ? (
        <div className="ml-9 space-y-0.5 border-l border-bg-border/60 pl-2">
          {WORKSPACE_VIEWS.map((v) => (
            <Link
              key={v.view}
              href={v.href}
              title={v.blurb}
              className="block truncate rounded px-1.5 py-1 text-[11px] text-text-muted transition-colors hover:bg-bg-hover hover:text-text-primary"
            >
              {v.label}
            </Link>
          ))}
        </div>
      ) : null}
    </div>
  );

  return (
    <nav
      className={clsx(
        "relative z-20 h-full shrink-0 overflow-y-auto border-r border-bg-border bg-bg-secondary/70 px-2 py-2.5 backdrop-blur-sm transition-[width] duration-200",
        collapsed ? "w-[58px]" : "w-[212px]",
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
        {SIDEBAR_GROUPS.map((group) => {
          const open = isOpen(group.id, group.defaultOpen ?? groupActive(group.id));
          return (
            <div key={group.id}>
              <GroupHeader label={group.title} open={open} onToggle={() => toggleGroup(group.id, open)} />
              {open ? (
                <div className={clsx("space-y-0.5", collapsed ? "mt-0" : "mt-0.5")}>
                  {group.entries.map((entry) => {
                    if (entry.kind === "workspace") return <WorkspaceRow key="workspace" />;
                    if (entry.kind === "desk") {
                      const desk = deskByHref[entry.href];
                      if (!desk) return null;
                      return <DeskRow key={desk.href} desk={desk} />;
                    }
                    return (
                      <RailLink
                        key={entry.href}
                        href={entry.href}
                        label={entry.label}
                        icon={ROUTE_ICON[entry.href] ?? Target}
                        active={matches(entry.href, entry.matchers, pathname)}
                      />
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

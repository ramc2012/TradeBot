"use client";

/**
 * v2 sidebar — grouped by the LANE LOGIC, not by a hand-kept flat list.
 *
 * ─── What changed (2026-07-20) ──────────────────────────────────────────────
 *
 * 1. WORKSPACE FIRST, AND ALWAYS OPEN. The cross-lane workspace used to be the
 *    first item inside "Strategy desks", a group that is COLLAPSED on the
 *    landing route — so the owner opened the terminal and saw none of the new
 *    work. It now has its own section at the top that is NOT collapsible, with
 *    its four BUILT views linked directly.
 *
 * 2. DESKS GROUPED BY HORIZON × KIND × POLICY, from lib/nav-model.ts, which
 *    reads lib/lane-taxonomy.ts and lib/policy-state.ts. Sections are declared
 *    horizons; policy terminals sort first inside a section and carry their
 *    policy-column chip; the KIND chip is resolved from the SERVED
 *    /api/system/lanes registry and says so when the registry has not loaded.
 *
 * 3. SCALP is rendered as a permanently-unavailable row carrying its reason,
 *    never as an empty group.
 *
 * 4. Nothing was deleted. Every previously-linked route is still linked, and
 *    the two routes whose nav entries had been commented out (Fractal MP,
 *    Sniper) are linked again under "Parked lanes" — labelled PARKED, which is
 *    a decision, not the "idle" a missing link implied.
 *
 * Manual group toggles still persist; width-collapse (icon rail) still works.
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
  CandlestickChart,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Compass,
  Crosshair,
  FileText,
  Fingerprint,
  FlaskConical,
  Globe,
  Grid3x3,
  Inbox,
  LayoutDashboard,
  Layers3,
  Lock,
  Network,
  Radar,
  Server,
  Settings,
  Sigma,
  Target,
  Waves,
  Workflow,
} from "lucide-react";

import { useLaneRegistry } from "@/hooks/useLaneRegistry";
import {
  LANE_SECTIONS,
  WORKSPACE_ROUTE,
  WORKSPACE_VIEWS,
  deskKinds,
  deskPolicyLabel,
  kindShort,
  policyRank,
  type NavDesk,
} from "@/lib/nav-model";

const SIDEBAR_STORAGE_KEY = "nomad-curie.sidebar.collapsed.v2";
// v3 key on purpose: the v2 overrides refer to group titles that no longer
// exist, and a stale `{"Strategy desks": false}` would re-hide the new model.
const GROUPS_STORAGE_KEY = "nomad-curie.sidebar.groups.v3";

type NavItem = { href: string; label: string; icon: typeof Target; matchers?: string[] };
type NavGroup = { title: string; items: NavItem[]; defaultOpen?: boolean };

/** Desk route → rail icon. Purely decorative; the model carries no icons. */
const DESK_ICON: Record<string, typeof Target> = {
  "/strategies/auction": Layers3,
  "/strategies/mp": Sigma,
  "/strategies/commodity": Waves,
  "/strategies/institutional-convergence": Network,
  "/strategies/directional": Target,
  "/strategies/directional?horizon=positional": Target,
  "/strategies/nse/live": Crosshair,
  "/strategies/macd-refined": BarChart3,
  "/strategies/gann": Compass,
  "/strategies/cbe": Radar,
  "/strategies/overview": LayoutDashboard,
  "/strategies/us-macd-refined": Globe,
  "/strategies/fractal": Fingerprint,
  "/strategies/sniper": Crosshair,
};

/** The non-lane groups. These are functions, not lanes, so they have no axes. */
const OPS_GROUPS: NavGroup[] = [
  {
    title: "Trade",
    items: [
      { href: "/", label: "Landing", icon: LayoutDashboard },
      { href: "/trading", label: "Execution", icon: Activity },
      { href: "/positions", label: "Positions", icon: Banknote },
      { href: "/proposals", label: "Proposals", icon: Inbox },
      { href: "/analytics", label: "Portfolio", icon: BriefcaseBusiness },
    ],
  },
  {
    title: "Markets",
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
    items: [
      { href: "/research", label: "Research lab", icon: FlaskConical, matchers: ["/research", "/analysis", "/backtester", "/data"] },
      { href: "/agent", label: "AI agent", icon: Bot },
    ],
  },
  {
    title: "Platform",
    items: [
      { href: "/system", label: "System hub", icon: Server, matchers: ["/system", "/health", "/lane-health"] },
      { href: "/reports", label: "Reports", icon: FileText },
      { href: "/settings", label: "Settings", icon: Settings },
    ],
  },
];

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

  useEffect(() => {
    try {
      setCollapsed(window.localStorage.getItem(SIDEBAR_STORAGE_KEY) === "1");
      setOverrides(JSON.parse(window.localStorage.getItem(GROUPS_STORAGE_KEY) || "{}"));
    } catch {
      /* ignore */
    }
  }, []);

  const laneSectionActive = (id: string) =>
    LANE_SECTIONS.find((s) => s.id === id)?.desks.some((d) => matches(d.href, d.matchers, pathname)) ?? false;
  const opsGroupActive = (title: string) =>
    OPS_GROUPS.find((g) => g.title === title)?.items.some((i) => matches(i.href, i.matchers, pathname)) ?? false;

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
        {/* ── Workspace: the primary destination. Never collapsed. ── */}
        <div>
          {collapsed ? (
            <div className="my-1 border-t border-bg-border/70" />
          ) : (
            <div className="flex items-center gap-1 px-2 py-1 text-text-muted">
              <span className="text-[10px] font-semibold uppercase tracking-[0.12em]">Workspace</span>
              <span className="rounded bg-accent-blue/15 px-1 text-[8.5px] font-bold uppercase tracking-[0.12em] text-accent-blue">
                primary
              </span>
            </div>
          )}
          <div className="space-y-0.5">
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
                <div className="px-1.5 py-1 text-[9px] leading-tight text-text-muted/70">
                  Risk &amp; Research views are scaffolds — they deep-link back to the legacy desks.
                </div>
              </div>
            ) : null}
          </div>
        </div>

        {/* ── Desks, grouped by declared HORIZON; policy terminals first. ── */}
        {LANE_SECTIONS.map((section) => {
          if (section.unavailable) {
            // Permanently unavailable horizon. One row, its reason on hover.
            const u = section.unavailable;
            return (
              <div key={section.id}>
                {collapsed ? <div className="my-1 border-t border-bg-border/70" /> : null}
                <div
                  title={`${u.reason}\n\nMissing capabilities: ${u.missingCapabilities.join(", ")}\nCitation: ${u.citation}`}
                  className={clsx(
                    "flex cursor-help items-center gap-2 rounded-lg border border-dashed border-bg-border/70 px-2 py-1.5 text-text-muted",
                    collapsed ? "justify-center" : "",
                  )}
                >
                  <Lock size={13} className="shrink-0" />
                  {!collapsed ? (
                    <span className="min-w-0">
                      <span className="block truncate text-[11px] font-semibold uppercase tracking-[0.1em]">{section.title}</span>
                      <span className="block text-[9px] leading-tight">
                        needs {u.missingCapabilities.join(" + ")} — structurally absent, not unbuilt
                      </span>
                    </span>
                  ) : null}
                </div>
              </div>
            );
          }
          const open = isOpen(section.id, section.defaultOpen || laneSectionActive(section.id));
          const desks = [...section.desks].sort((a, b) => policyRank(a) - policyRank(b));
          return (
            <div key={section.id}>
              <GroupHeader
                label={section.title}
                title={section.blurb}
                open={open}
                onToggle={() => toggleGroup(section.id, open)}
              />
              {open ? (
                <div className={clsx("space-y-0.5", collapsed ? "mt-0" : "mt-0.5")}>
                  {desks.map((desk) => (
                    <RailLink
                      key={desk.href}
                      href={desk.href}
                      label={desk.label}
                      icon={DESK_ICON[desk.href] ?? Target}
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
                  ))}
                </div>
              ) : null}
            </div>
          );
        })}

        {/* ── Non-lane function groups, unchanged in content. ── */}
        {OPS_GROUPS.map((group) => {
          const open = isOpen(group.title, group.defaultOpen ?? opsGroupActive(group.title));
          return (
            <div key={group.title}>
              <GroupHeader label={group.title} open={open} onToggle={() => toggleGroup(group.title, open)} />
              {open ? (
                <div className={clsx("space-y-0.5", collapsed ? "mt-0" : "mt-0.5")}>
                  {group.items.map(({ href, label, icon: Icon, matchers }) => (
                    <RailLink
                      key={href}
                      href={href}
                      label={label}
                      icon={Icon}
                      active={matches(href, matchers, pathname)}
                    />
                  ))}
                </div>
              ) : null}
            </div>
          );
        })}
      </div>
    </nav>
  );
}

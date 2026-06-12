"use client";

/**
 * Universal strategy-desk shell.
 *
 * Every /strategies/<name> route renders inside this so the trader sees
 * the same chrome (header, status chips, tab bar, data-freshness pill)
 * regardless of which desk they're looking at. The desk's actual content
 * (live overview, paper trading, etc.) goes into the `children` slot.
 *
 * v1 desks each rolled their own header / status row / tab bar with
 * subtly different padding, font sizes, badge styles, and tab keying.
 * One shell removes that divergence in one stroke.
 */
import { clsx } from "clsx";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useCallback } from "react";
import { Activity } from "lucide-react";

import { IndexTickerStrip } from "./IndexTickerStrip";
import { StatusBadge } from "./StatusBadge";
import { formatIST } from "./formatters";

export type DeskTab = {
  key: string;
  label: string;
  /** Any component that accepts a `size` prop — lucide-react icons fit. */
  icon?: React.ElementType;
};

export function DeskShell({
  title,
  description,
  asOf,
  isLive,
  isFetching,
  paperMode,
  tabs,
  activeTab,
  onTabChange,
  v1Href,
  rightSlot,
  children,
}: {
  title: string;
  description?: string;
  asOf?: string | Date | null;
  isLive?: boolean;
  isFetching?: boolean;
  paperMode?: boolean;
  tabs: DeskTab[];
  activeTab: string;
  onTabChange: (key: string) => void;
  /** v1 equivalent URL for quick-jump while v2 is being perfected. */
  v1Href?: string;
  rightSlot?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div className="space-y-4">
      <header className="rounded-2xl border border-bg-border bg-bg-secondary/22 px-5 py-4">
        <IndexTickerStrip className="mb-2.5 border-b border-bg-border/40 pb-2" />
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <h1 className="text-xl font-semibold text-text-primary">{title}</h1>
              {paperMode != null ? (
                <StatusBadge
                  label={paperMode ? "paper" : "live"}
                  variant={paperMode ? "success" : "warn"}
                />
              ) : null}
              {isLive ? (
                <StatusBadge label="armed" variant="success" icon={<span className="h-1.5 w-1.5 rounded-full bg-accent-green" />} />
              ) : null}
            </div>
            {description ? (
              <p className="mt-1 text-sm text-text-muted max-w-2xl">{description}</p>
            ) : null}
            <div className="mt-2 flex flex-wrap items-center gap-3 text-[11.5px] text-text-muted">
              <span className="inline-flex items-center gap-1.5">
                <Activity size={12} className={isFetching ? "animate-pulse text-accent-blue" : undefined} />
                {isFetching ? "Refreshing" : "Idle"}
              </span>
              {asOf ? (
                (() => {
                  const ageSec = Math.max(0, (Date.now() - new Date(asOf).getTime()) / 1000);
                  const stale = ageSec > 90;
                  return (
                    <span className={clsx("inline-flex items-center gap-1.5", stale && "text-accent-amber")}>
                      {stale ? <span className="h-1.5 w-1.5 rounded-full bg-accent-amber" /> : null}
                      As of {formatIST(asOf)}
                      {stale ? ` · ${Math.round(ageSec)}s stale` : ""}
                    </span>
                  );
                })()
              ) : null}
            </div>
          </div>
          <div className="flex items-center gap-2">
            {rightSlot}
            {/* v1 deprecated — the "v1 view" link is intentionally not rendered.
                The v1Href prop is kept for back-compat but no longer surfaces a
                link (v1 frontend is being retired; v2 is the primary UI). */}
          </div>
        </div>
      </header>

      <DeskTabBar tabs={tabs} activeTab={activeTab} onTabChange={onTabChange} />

      {children}
    </div>
  );
}

/**
 * URL-synced tab bar. Lifting the active tab into a search param means
 * /strategies/directional?tab=paper is a shareable link, and the
 * browser back button moves between tabs naturally.
 */
export function useUrlTab(defaultTab: string): [string, (key: string) => void] {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const active = searchParams.get("tab") || defaultTab;
  const set = useCallback(
    (key: string) => {
      const next = new URLSearchParams(searchParams.toString());
      if (key === defaultTab) next.delete("tab");
      else next.set("tab", key);
      router.replace(`${pathname}${next.size ? `?${next.toString()}` : ""}`, { scroll: false });
    },
    [router, pathname, searchParams, defaultTab],
  );
  return [active, set];
}

function DeskTabBar({
  tabs,
  activeTab,
  onTabChange,
}: {
  tabs: DeskTab[];
  activeTab: string;
  onTabChange: (key: string) => void;
}) {
  return (
    <nav className="flex flex-wrap items-center gap-1 border-b border-bg-border/40 pb-0">
      {tabs.map(({ key, label, icon: Icon }) => (
        <button
          key={key}
          type="button"
          onClick={() => onTabChange(key)}
          className={clsx(
            "inline-flex items-center gap-1.5 rounded-t-lg border-b-2 px-3 py-2 text-[12.5px] font-semibold transition-colors",
            activeTab === key
              ? "border-accent-blue text-text-primary"
              : "border-transparent text-text-muted hover:text-text-secondary",
          )}
        >
          {Icon ? <Icon size={14} /> : null}
          {label}
        </button>
      ))}
    </nav>
  );
}

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
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useCallback } from "react";
import { Activity, ExternalLink } from "lucide-react";

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
                <Activity size={12} />
                {isFetching ? "Refreshing" : "Idle"}
              </span>
              {asOf ? <span>As of {formatIST(asOf)}</span> : null}
            </div>
          </div>
          <div className="flex items-center gap-2">
            {rightSlot}
            {v1Href ? (
              <Link
                href={v1Href}
                className="inline-flex items-center gap-1.5 rounded-lg border border-bg-border bg-bg-primary/30 px-2.5 py-1.5 text-[11px] text-text-secondary hover:border-bg-active hover:text-text-primary"
                title="Open the equivalent v1 page in a new tab"
                target="_blank"
              >
                v1 view
                <ExternalLink size={11} />
              </Link>
            ) : null}
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

"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { clsx } from "clsx";

type PageTab = {
  href: string;
  label: string;
};

export default function PageTabs({
  tabs,
  className,
}: {
  tabs: PageTab[];
  className?: string;
}) {
  const pathname = usePathname();

  return (
    <div className={clsx("flex min-w-0 gap-2 overflow-x-auto pb-1", className)}>
      {tabs.map((tab) => {
        const active = pathname === tab.href;
        return (
          <Link
            key={tab.href}
            href={tab.href}
            className={clsx(
              "relative flex min-w-[170px] items-center justify-between rounded-t-[18px] rounded-b-xl border px-4 py-2.5 text-left transition-all",
              active
                ? "border-accent-blue/45 bg-[#111a2c] text-text-primary shadow-[0_10px_24px_rgba(6,12,24,0.24)]"
                : "border-bg-border bg-bg-secondary/35 text-text-muted hover:border-bg-active hover:text-text-primary",
            )}
          >
            <div className="text-[11px] font-semibold uppercase tracking-[0.18em]">{tab.label}</div>
            <div
              className={clsx(
                "h-2 w-2 rounded-full border transition-colors",
                active ? "border-accent-blue bg-accent-blue" : "border-bg-border bg-bg-primary/30",
              )}
            />
          </Link>
        );
      })}
    </div>
  );
}

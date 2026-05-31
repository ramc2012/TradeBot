"use client";

/**
 * Placeholder for strategy desks that haven't been ported to the v2
 * shell yet. Renders the new DeskShell with a single tab and a card
 * that links into the v1 equivalent. As each desk is ported, its stub
 * gets replaced with the real Desk component.
 */
import Link from "next/link";
import { ArrowUpRight, Construction } from "lucide-react";

import { DeskShell, Section, useUrlTab } from "@/components/desk-ui";

export default function DeskStub({
  title,
  description,
  v1Href,
  v1Label,
}: {
  title: string;
  description: string;
  v1Href: string;
  v1Label: string;
}) {
  const [activeTab, setActiveTab] = useUrlTab("live");
  return (
    <DeskShell
      title={title}
      description={description}
      tabs={[{ key: "live", label: "Live overview" }]}
      activeTab={activeTab}
      onTabChange={setActiveTab}
      v1Href={v1Href}
    >
      <Section title="Not yet ported to v2" icon={<Construction size={16} />}>
        <p className="text-sm text-text-secondary">
          This desk is still served by the v1 frontend. The v2 desk shell is
          ready — when the desk gets ported, the trade-off / paper /
          backtest tabs will appear here. Until then, open the v1 surface:
        </p>
        <Link
          href={v1Href}
          target="_blank"
          rel="noreferrer"
          className="mt-3 inline-flex items-center gap-2 rounded-lg border border-accent-blue/35 bg-accent-blue/10 px-3 py-2 text-sm font-semibold text-accent-blue hover:border-accent-blue/55"
        >
          {v1Label}
          <ArrowUpRight size={14} />
        </Link>
      </Section>
    </DeskShell>
  );
}

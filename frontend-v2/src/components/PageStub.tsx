"use client";

/**
 * Generic placeholder page — links to a v1 equivalent for everything
 * that hasn't been ported to v2 yet. Keeps the v2 app navigable while
 * we incrementally port surfaces.
 */
import Link from "next/link";
import { ArrowUpRight, Construction } from "lucide-react";

import { Section } from "@/components/desk-ui";

export default function PageStub({
  title,
  description,
  v1Href,
  v1Label,
  notes,
}: {
  title: string;
  description: string;
  v1Href: string;
  v1Label: string;
  notes?: string[];
}) {
  return (
    <div className="space-y-4">
      <header>
        <h1 className="text-2xl font-semibold text-text-primary">{title}</h1>
        <p className="mt-1 text-sm text-text-muted">{description}</p>
      </header>
      <Section title="Not yet ported to v2" icon={<Construction size={16} />}>
        <p className="text-sm text-text-secondary">
          The v2 reorganisation hasn't reached this surface yet. The v1
          page works unchanged at the link below — bookmarks targeting
          the v1 URL still resolve.
        </p>
        {notes && notes.length > 0 ? (
          <ul className="mt-3 list-disc space-y-1 pl-5 text-[12px] text-text-muted">
            {notes.map((n, i) => <li key={i}>{n}</li>)}
          </ul>
        ) : null}
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
    </div>
  );
}

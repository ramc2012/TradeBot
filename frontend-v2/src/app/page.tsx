"use client";

/**
 * Landing — the first thing the owner sees, and now the first place the new
 * lane logic is legible.
 *
 * ─── What changed (2026-07-20) ──────────────────────────────────────────────
 *
 * 1. The Market Structure WORKSPACE leads the page. It is the cross-lane
 *    command surface; the desk cards below are a desk index, not the product.
 *
 * 2. The desk cards are no longer an ad-hoc list. They are grouped by the same
 *    declared HORIZON sections the sidebar uses (lib/nav-model.ts), policy
 *    terminals first, each carrying its policy column and its SERVED registry
 *    kind. Scalp is present, empty and permanently unavailable WITH its reason.
 *
 * 3. Every card says WHY it has no number. The old grid rendered an em-dash for
 *    five of seven desks, and that single glyph was hiding four different
 *    facts:
 *      · the endpoint it polled does not exist (/api/strategy/paper-summary,
 *        /api/gann-tp-delta/paper-summary, /api/commodity-strategy/paper-summary,
 *        /api/auction-intelligence/paper-summary and
 *        /api/mp-intelligence/paper-summary all 404 — verified 2026-07-20);
 *      · the endpoint exists but did not answer;
 *      · it answered and the book is MEASURED-flat;
 *      · the lane is PARKED and will not trade at all.
 *    Those are now four distinct states with four distinct labels, and the
 *    endpoints have been repointed at the ones the backend actually serves.
 *
 * 4. The roll-up tiles sum ONLY desks that answered, over UNIQUE endpoints
 *    (Long Premium appears at two horizons and must not be counted twice), and
 *    a missing component is never summed as zero — it is named.
 */
import Link from "next/link";
import { useQueries } from "@tanstack/react-query";
import { ArrowUpRight, Banknote, BookOpen, Grid3x3, Lock, Target } from "lucide-react";

import { MetricTile, REFRESH_MS, Section, StatusBadge, formatSignedMoney, tone } from "@/components/desk-ui";
import { LastUpdated } from "@/components/common/LastUpdated";
import { api as apiClient } from "@/lib/api";
import {
  CURRENCY_SYMBOL,
  DESK_CARD_STATE_LABEL,
  DESK_CARD_STATE_VARIANT,
  LANE_SECTIONS,
  WORKSPACE_ROUTE,
  WORKSPACE_VIEWS,
  allDesks,
  deskCardState,
  deskCurrency,
  deskKinds,
  deskPolicyLabel,
  deskTotalPnl,
  kindShort,
  normalizeBook,
  policyRank,
  reportingTally,
  type DeskFetch,
} from "@/lib/nav-model";
import { useLaneRegistry } from "@/hooks/useLaneRegistry";

/** One query per DISTINCT endpoint — two desks may legitimately share one. */
const BOOK_ENDPOINTS = Array.from(
  new Set(allDesks().filter((d) => d.book).map((d) => d.book!.endpoint)),
);

/** Path to walk for a given endpoint (identical across desks that share it). */
const PATH_BY_ENDPOINT: Record<string, string[]> = Object.fromEntries(
  allDesks().filter((d) => d.book).map((d) => [d.book!.endpoint, d.book!.path]),
);

/**
 * Endpoints denominated in INR. The US MACD Refined book is USD paper capital
 * (macd_refined/config.py: MACD_REFINED_US_INITIAL_CAPITAL = 100_000.0 "USD"),
 * so summing it into the ₹ roll-up asserted $1 = ₹1 — a number the data does
 * not support. Non-INR books are excluded from the totals and named below.
 */
const INR_ENDPOINTS = BOOK_ENDPOINTS.filter((e) =>
  allDesks().some((d) => d.book?.endpoint === e && deskCurrency(d) === "INR"),
);
const NON_INR_DESKS = allDesks().filter((d) => d.book && deskCurrency(d) !== "INR");

export default function LandingPage() {
  const results = useQueries({
    queries: BOOK_ENDPOINTS.map((endpoint) => ({
      queryKey: ["landing", "book", endpoint],
      // Errors are NOT swallowed into null here: "the endpoint failed" and
      // "the book is empty" must stay distinguishable downstream.
      queryFn: async () => (await apiClient.get(endpoint)).data,
      refetchInterval: REFRESH_MS.snapshot,
      refetchOnWindowFocus: false,
      retry: false,
    })),
  });

  const registry = useLaneRegistry();
  const kindByLaneKey: Record<string, string> = {};
  for (const l of registry.data?.lanes ?? []) kindByLaneKey[l.key] = String(l.kind || "");

  const fetchByEndpoint: Record<string, DeskFetch> = {};
  BOOK_ENDPOINTS.forEach((endpoint, i) => {
    const q = results[i];
    if (q.isError) {
      const err = q.error as { response?: { status?: number }; message?: string } | undefined;
      const status = err?.response?.status;
      fetchByEndpoint[endpoint] = {
        status: "error",
        detail: status ? `HTTP ${status}` : err?.message || "request failed",
      };
    } else if (q.data === undefined) {
      fetchByEndpoint[endpoint] = { status: "pending" };
    } else {
      fetchByEndpoint[endpoint] = { status: "ok", payload: q.data };
    }
  });

  const cards = allDesks().map((desk) =>
    deskCardState(desk, desk.book ? fetchByEndpoint[desk.book.endpoint] ?? null : null),
  );
  const tally = reportingTally(cards);

  // Roll-up over UNIQUE endpoints that answered. Nulls are counted as missing,
  // never as zero.
  let realized = 0;
  let unrealized = 0;
  let open = 0;
  let equity = 0;
  const missing = { realized: 0, unrealized: 0, open: 0, equity: 0 };
  for (const endpoint of INR_ENDPOINTS) {
    const f = fetchByEndpoint[endpoint];
    if (!f || f.status !== "ok") continue;
    const b = normalizeBook(f.payload, PATH_BY_ENDPOINT[endpoint] ?? []);
    if (b.realizedPnl === null) missing.realized++;
    else realized += b.realizedPnl;
    if (b.unrealizedPnl === null) missing.unrealized++;
    else unrealized += b.unrealizedPnl;
    if (b.openPositions === null) missing.open++;
    else open += b.openPositions;
    if (b.totalEquity === null) missing.equity++;
    else equity += b.totalEquity;
  }
  const answered = INR_ENDPOINTS.filter((e) => fetchByEndpoint[e]?.status === "ok").length;
  const fetchedAt = results.reduce((acc, q) => Math.max(acc, q.dataUpdatedAt || 0), 0);
  const detail = (miss: number) =>
    `${answered - miss} of ${INR_ENDPOINTS.length} INR books carry this field`;

  return (
    <div className="space-y-4">
      <header>
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="text-2xl font-semibold text-text-primary">Terminal</h1>
          <LastUpdated label="Fetched" timestamp={fetchedAt > 0 ? fetchedAt : null} />
        </div>
        <p className="mt-1 text-sm text-text-muted">
          The workspace below is the cross-lane command surface. The desks are grouped by their DECLARED horizon and, where
          one applies, by the policy column they are a terminal for.
        </p>
      </header>

      {/* ── The primary destination ── */}
      <Link
        href={WORKSPACE_ROUTE}
        className="group block rounded-2xl border border-accent-blue/35 bg-accent-blue/[0.07] p-4 transition-colors hover:border-accent-blue/60"
      >
        <div className="flex items-start justify-between gap-3">
          <div className="flex items-center gap-2.5">
            <div className="rounded-lg border border-accent-blue/35 bg-accent-blue/12 p-2 text-accent-blue">
              <Grid3x3 size={18} />
            </div>
            <div>
              <div className="flex items-center gap-2">
                <div className="text-base font-semibold text-text-primary">Market Structure workspace</div>
                <span className="rounded bg-accent-blue/15 px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-[0.14em] text-accent-blue">
                  primary
                </span>
              </div>
              <div className="mt-0.5 text-xs text-text-muted">
                One pinned instrument, every lane&apos;s read on it — command matrix, structure, flow and the four policy
                columns in one shell.
              </div>
            </div>
          </div>
          <ArrowUpRight size={16} className="mt-1 shrink-0 text-accent-blue" />
        </div>
        <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-4">
          {WORKSPACE_VIEWS.map((v) => (
            <Link
              key={v.view}
              href={v.href}
              className="rounded-xl border border-bg-border bg-bg-primary/25 px-3 py-2 transition-colors hover:border-accent-blue/40"
            >
              <div className="text-[12.5px] font-semibold text-text-primary">{v.label}</div>
              <div className="mt-0.5 text-[10.5px] leading-tight text-text-muted">{v.blurb}</div>
            </Link>
          ))}
        </div>
        <div className="mt-2 text-[10.5px] text-text-muted/80">
          Risk &amp; Execution and Research are scaffolds inside the workspace — they deep-link back to /trading,
          /positions, /research and /analytics, which remain the only home of those functions.
        </div>
      </Link>

      <Section title="Cross-lane paper book" icon={<Banknote size={16} />}>
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <MetricTile
            label="Total equity"
            value={
              answered - missing.equity > 0
                ? `₹${equity.toLocaleString("en-IN", { maximumFractionDigits: 0 })}`
                : "UNAVAILABLE"
            }
            detail={detail(missing.equity)}
          />
          <MetricTile
            label="Realized P&L"
            value={answered - missing.realized > 0 ? formatSignedMoney(realized) : "UNAVAILABLE"}
            color={tone(realized)}
            detail={detail(missing.realized)}
          />
          <MetricTile
            label="Unrealized P&L"
            value={answered - missing.unrealized > 0 ? formatSignedMoney(unrealized) : "UNAVAILABLE"}
            color={tone(unrealized)}
            detail={detail(missing.unrealized)}
          />
          <MetricTile
            label="Open positions"
            value={answered - missing.open > 0 ? String(open) : "UNAVAILABLE"}
            detail={`${tally.reporting} of ${tally.askable} askable desks answered with a book · ${tally.partial} partial · ${tally.notReporting} not reporting · ${tally.noBook} with no book endpoint · ${tally.parked} parked`}
          />
        </div>
        <p className="mt-2 text-[11px] text-text-muted">
          Summed over DISTINCT ₹-denominated book endpoints, so Long Premium — which is listed at two horizons — is
          counted once. A field a payload does not carry is counted as missing above, never summed as zero.
          {NON_INR_DESKS.length > 0 ? (
            <>
              {" "}
              Excluded from these totals because it is not ₹:{" "}
              {NON_INR_DESKS.map((d) => `${d.label} (${deskCurrency(d)})`).join(", ")} — its book is shown on its own
              card in its own currency.
            </>
          ) : null}
        </p>
      </Section>

      {LANE_SECTIONS.map((section) => {
        if (section.unavailable) {
          const u = section.unavailable;
          return (
            <Section key={section.id} title={section.title} icon={<Lock size={16} />}>
              <div className="rounded-2xl border border-dashed border-bg-border bg-bg-primary/10 p-4">
                <div className="flex flex-wrap items-center gap-2">
                  <StatusBadge label="Permanently unavailable" variant="neutral" />
                  {u.missingCapabilities.map((c) => (
                    <StatusBadge key={c} label={`missing: ${c}`} variant="neutral" />
                  ))}
                </div>
                <p className="mt-2 text-[12px] leading-relaxed text-text-secondary">{u.reason}</p>
                <p className="mt-1.5 text-[10.5px] text-text-muted">Citation: {u.citation}</p>
              </div>
            </Section>
          );
        }
        const sectionCards = cards
          .filter((c) => section.desks.includes(c.desk))
          .sort((a, b) => policyRank(a.desk) - policyRank(b.desk));
        if (sectionCards.length === 0) return null;
        return (
          <Section key={section.id} title={section.title} icon={<Target size={16} />}>
            <p className="-mt-1 mb-2 text-[11px] text-text-muted">{section.blurb}</p>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3">
              {sectionCards.map((card) => {
                const { desk } = card;
                const pnl = deskTotalPnl(card);
                const k = deskKinds(desk, kindByLaneKey);
                const policy = deskPolicyLabel(desk);
                // Render each book in ITS OWN currency — the US lane is USD.
                const ccy = deskCurrency(desk);
                const money = (v: number) =>
                  `${v > 0 ? "+" : ""}${CURRENCY_SYMBOL[ccy]}${v.toLocaleString("en-IN", {
                    maximumFractionDigits: 0,
                  })}`;
                return (
                  <div
                    key={desk.href}
                    className="group flex flex-col rounded-2xl border border-bg-border bg-bg-primary/16 transition-colors hover:border-accent-blue/40"
                  >
                  <Link href={desk.href} className="flex flex-1 flex-col p-4">
                    <div className="flex items-start justify-between gap-2">
                      <div className="min-w-0">
                        <div className="truncate font-semibold text-text-primary">{desk.label}</div>
                        <div className="mt-1 flex flex-wrap items-center gap-1">
                          {policy ? <StatusBadge label={policy} variant="info" /> : null}
                          {k.registryUnavailable ? (
                            <StatusBadge label="kind unavailable" variant="neutral" />
                          ) : (
                            k.kinds.map((kind) => <StatusBadge key={kind} label={kindShort(kind)} variant="neutral" />)
                          )}
                        </div>
                      </div>
                      <ArrowUpRight size={14} className="shrink-0 text-text-muted group-hover:text-accent-blue" />
                    </div>

                    <div className="mt-3 flex items-center gap-2">
                      <StatusBadge
                        label={DESK_CARD_STATE_LABEL[card.state]}
                        variant={DESK_CARD_STATE_VARIANT[card.state]}
                      />
                      {pnl.value !== null ? (
                        <span className={`font-mono text-sm font-semibold ${tone(pnl.value)}`}>
                          {money(pnl.value)}
                        </span>
                      ) : (
                        <span className="font-mono text-sm font-semibold text-text-muted">P&amp;L UNAVAILABLE</span>
                      )}
                      {card.fields?.openPositions != null ? (
                        <span className="ml-auto text-[11px] text-text-muted">
                          {card.fields.openPositions} open
                        </span>
                      ) : (
                        <span className="ml-auto text-[11px] text-text-muted">open UNAVAILABLE</span>
                      )}
                    </div>

                    {pnl.value !== null && !pnl.complete ? (
                      <div className="mt-1 text-[10.5px] text-text-muted">
                        partial: {pnl.missing.join(" + ")} not carried by this payload
                      </div>
                    ) : null}

                    <div className="mt-2 text-[10.5px] leading-tight text-text-muted">{card.reason}</div>
                  </Link>
                  {/* BOOKS affordance — order / trade / position / portfolio
                      over the lane's AUTHORITATIVE book. Rendered on the card
                      itself so the pages are not reachable only from the rail. */}
                  {desk.books ? (
                    <Link
                      href={desk.books.href}
                      title={desk.books.blurb}
                      className="flex items-center gap-1.5 border-t border-bg-border/70 px-4 py-2 text-[11px] font-semibold text-text-muted transition-colors hover:bg-bg-hover hover:text-accent-blue"
                    >
                      <BookOpen size={12} />
                      {desk.books.label}
                      <span className="font-normal text-text-muted/70">{desk.books.views.join(" · ")}</span>
                      <ArrowUpRight size={12} className="ml-auto" />
                    </Link>
                  ) : null}
                  </div>
                );
              })}
            </div>
          </Section>
        );
      })}

      <Section title="Quick links">
        <div className="grid grid-cols-2 gap-2 text-sm md:grid-cols-4">
          {[
            { href: "/trading", label: "Execution" },
            { href: "/positions", label: "Positions" },
            { href: "/market", label: "Market" },
            { href: "/charts", label: "Charts" },
            { href: "/orderflow", label: "Orderflow" },
            { href: "/research", label: "Research" },
            { href: "/system", label: "System health" },
            { href: "/settings", label: "Settings" },
          ].map(({ href, label }) => (
            <Link
              key={href}
              href={href}
              className="rounded-lg border border-bg-border bg-bg-primary/15 px-3 py-2 text-text-secondary transition-colors hover:border-accent-blue/30 hover:text-text-primary"
            >
              {label}
            </Link>
          ))}
        </div>
      </Section>
    </div>
  );
}

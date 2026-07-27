"use client";

/**
 * LaneBooksDesk — the order / trade / position / portfolio surface for one
 * lane's AUTHORITATIVE book (and its market sibling, where one exists).
 *
 * One desk drives all three routes. The differences between lanes are not
 * branches in this file — they are DECLARED in lib/lane-books and read here, so
 * a lane cannot quietly acquire a field it does not have.
 *
 * The source banner is rendered above the tabs, on every view, because "which
 * book did this number come from" must be answerable without scrolling.
 */
import { useMemo } from "react";
import { BarChart3, ListChecks, ScrollText, Target } from "lucide-react";
import Link from "next/link";

import { DeskShell, StatusBadge, useUrlChoice, useUrlTab, type DeskTab } from "@/components/desk-ui";
import { BookSourceBanner } from "./BookPrimitives";
import { OrderBookView, PortfolioView, PositionsView, TradeBookView } from "./BookViews";
import { useBookData } from "./book-data";
import { useLaneRegistry } from "@/hooks/useLaneRegistry";
import {
  BOOK_VIEWS,
  BOOK_VIEW_LABEL,
  BOOK_VIEW_PARAM,
  booksForRoute,
  isBookView,
  type BookView,
  type LaneBook,
} from "@/lib/lane-books";
import { deriveExecutionMode, deriveSchedulerState } from "@/lib/market-semantics";

const VIEW_ICON: Record<BookView, DeskTab["icon"]> = {
  orders: ScrollText,
  trades: ListChecks,
  positions: Target,
  portfolio: BarChart3,
};

const TABS: DeskTab[] = BOOK_VIEWS.map((v) => ({ key: v, label: BOOK_VIEW_LABEL[v], icon: VIEW_ICON[v] }));

export function LaneBooksDesk({
  routeBase,
  title,
  description,
}: {
  routeBase: string;
  title: string;
  description: string;
}) {
  const books = useMemo(() => booksForRoute(routeBase), [routeBase]);
  const markets = useMemo(() => books.map((b) => b.market), [books]);
  const [market, setMarket] = useUrlChoice<"NSE" | "MCX">("market", markets.length ? markets : ["NSE"], markets[0] ?? "NSE");
  const book: LaneBook = books.find((b) => b.market === market) ?? books[0];
  const [rawView, setView] = useUrlTab("orders", BOOK_VIEW_PARAM);
  const view: BookView = isBookView(rawView) ? rawView : "orders";

  const query = useBookData(book.key);
  const data = query.data;
  const registry = useLaneRegistry();
  // RUNNING-vs-ARMED comes from the SERVED registry, never from a local guess.
  // With no registry loaded the badges are omitted rather than defaulted.
  const lane = useMemo(
    () => registry.data?.lanes?.find((l) => l.key === book.laneKey) ?? null,
    [registry.data, book.laneKey],
  );

  const nowMs = Date.now();

  return (
    <DeskShell
      title={title}
      description={description}
      asOf={data?.lastWriteAt ?? null}
      asOfLabel="Book written"
      // A paper book is written by a lane that only runs in session; the
      // green→amber cutoffs of a tick feed would paint every weekend red.
      asOfStaleSeconds={3600}
      asOfCriticalSeconds={86_400 * 4}
      isFetching={query.isFetching}
      paperMode={lane ? deriveExecutionMode({ execution_mode: lane.execution_mode }) === "paper" : undefined}
      schedulerState={lane ? deriveSchedulerState(lane) : undefined}
      tabs={TABS}
      activeTab={view}
      onTabChange={(k) => setView(k)}
      rightSlot={
        books.length > 1 ? (
          <div className="flex items-center gap-1">
            {books.map((b) => (
              <button
                key={b.market}
                type="button"
                onClick={() => setMarket(b.market)}
                title={`${b.label} — ${b.source.path}`}
                className={`rounded-full border px-3 py-1 text-[11px] font-semibold uppercase tracking-[0.12em] transition-colors ${
                  b.market === market
                    ? "border-accent-blue/50 bg-accent-blue/10 text-accent-blue"
                    : "border-bg-border text-text-muted hover:text-text-secondary"
                }`}
              >
                {b.market}
              </button>
            ))}
          </div>
        ) : null
      }
      beforeTabs={
        <div className="space-y-2">
          <BookSourceBanner
            book={book}
            counts={{
              open: data?.facts.openCount ?? null,
              closed: data?.facts.closedCount ?? null,
              ...(data?.orders ? { orderEvents: data.orders.length } : {}),
              ...(data?.decisions ? { decisions: data.decisions.length } : {}),
              ...(data?.intents ? { intents: data.intents.length } : {}),
            }}
            lastWriteAt={data?.lastWriteAt ?? null}
            errors={data?.errors ?? []}
          />
          <div className="flex flex-wrap items-center gap-2 text-[11px] text-text-muted">
            <span>Sibling books:</span>
            {books.map((b) => (
              <Link
                key={b.key}
                href={b.route}
                className={`rounded-full border px-2.5 py-0.5 font-semibold uppercase tracking-[0.1em] transition-colors ${
                  b.key === book.key
                    ? "border-accent-blue/40 text-accent-blue"
                    : "border-bg-border hover:border-accent-blue/30 hover:text-text-secondary"
                }`}
              >
                {b.label}
              </Link>
            ))}
            <Link href={book.deskHref} className="ml-auto underline decoration-dotted hover:text-text-secondary">
              open the {book.label} desk
            </Link>
          </div>
        </div>
      }
    >
      {query.isLoading || !data ? (
        <LoadingBooks book={book} error={query.error} />
      ) : (
        <div className="mt-3">
          {view === "orders" ? <OrderBookView data={data} /> : null}
          {view === "trades" ? <TradeBookView data={data} /> : null}
          {view === "positions" ? <PositionsView data={data} nowMs={nowMs} /> : null}
          {view === "portfolio" ? <PortfolioView data={data} nowMs={nowMs} /> : null}
        </div>
      )}
    </DeskShell>
  );
}

function LoadingBooks({ book, error }: { book: LaneBook; error: unknown }) {
  if (error) {
    return (
      <div className="mt-3 rounded-2xl border border-accent-red/35 bg-accent-red/8 p-5">
        <StatusBadge label="book not read" variant="error" />
        <p className="mt-2 text-sm text-accent-red">
          {book.source.path} could not be read. Every figure on this page is UNAVAILABLE — this is NOT an empty book.
        </p>
        <p className="mt-1 font-mono text-[11px] text-text-muted">
          {(error as { message?: string })?.message ?? String(error)}
        </p>
      </div>
    );
  }
  return (
    <div className="mt-3 space-y-2">
      {[0, 1, 2].map((i) => (
        <div key={i} className="h-16 animate-pulse rounded-xl bg-bg-secondary/40" />
      ))}
      <div className="text-center text-[11px] text-text-muted">Reading {book.source.path}…</div>
    </div>
  );
}

export default LaneBooksDesk;

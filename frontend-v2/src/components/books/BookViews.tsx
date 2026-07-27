"use client";

/**
 * BookViews — the four views (order book / trade book / positions / portfolio)
 * rendered from the ONE normalized book model in lib/book-rows.
 *
 * The views are lane-agnostic on purpose: every honesty decision that differs
 * per lane (is there an order layer? are fees recorded? is there a mark clock?
 * where does the day figure come from?) is DECLARED in lib/lane-books and read
 * here, so no view can quietly assume a field exists for a book that lacks it.
 */
import { useMemo, useState } from "react";
import {
  BarChart3,
  ClipboardList,
  ListChecks,
  ScrollText,
  ShieldQuestion,
  Target,
  Wallet,
} from "lucide-react";

import {
  MetricTile,
  Section,
  StatusBadge,
  formatIST,
  formatMoney,
  formatNumber,
  formatPct,
  formatSignedMoney,
} from "@/components/desk-ui";
import { ExitReasonChip } from "@/components/strategies/institutional-convergence/ExecutionPanels";
import {
  AbsentFieldsNote,
  DayLifetimeNote,
  DayTile,
  FieldValue,
  MarkAgeChip,
  MeasuredEmpty,
  NeverFiredState,
  QtyCell,
  SourceUnavailable,
  Unavailable,
  UnrealizedCell,
  UnrealizedRollupTile,
} from "./BookPrimitives";
import type { BookData } from "./book-data";
import { notionalExposure } from "@/lib/book-rows";
import {
  ORDER_LAYER_LABEL,
  ORDER_LAYER_VARIANT,
  PLAN_BASIS_NOTE,
  dteFromExpiry,
  markVerdict,
  portfolioUnrealized,
  totalPnl,
  unavailableReason,
} from "@/lib/lane-books";
import { rrRender } from "@/lib/market-semantics";

const money0 = (v: number) => formatMoney(v);
const signed0 = (v: number) => formatSignedMoney(v);
const price2 = (v: number) => formatNumber(v, 2);

function pnlTone(v: number | null | undefined): string {
  if (v == null) return "text-text-muted";
  return v > 0 ? "text-accent-green" : v < 0 ? "text-accent-red" : "text-text-secondary";
}

// ─── 1. ORDER BOOK ──────────────────────────────────────────────────────────

/**
 * The honest answer to "show me the order book" for a paper lane.
 *
 * The statement at the top is not boilerplate: it is the per-lane finding about
 * whether an order-level record genuinely exists. Where none does, the view
 * says so and shows what DOES exist (decisions, intents, gate blocks). No order
 * is ever reconstructed from a fill.
 */
export function OrderBookView({ data }: { data: BookData }) {
  const { book } = data;
  return (
    <div className="space-y-3">
      <Section
        title="What this lane records at order level"
        icon={<ShieldQuestion size={16} />}
        rightSlot={<StatusBadge label={ORDER_LAYER_LABEL[book.orderLayer]} variant={ORDER_LAYER_VARIANT[book.orderLayer]} />}
      >
        <p className="max-w-4xl text-[12.5px] leading-relaxed text-text-secondary">{book.orderLayerStatement}</p>
        <div className="mt-2 rounded-lg border border-bg-border bg-bg-primary/12 px-3 py-2 text-[11px] leading-relaxed text-text-muted">
          <span className="font-semibold uppercase tracking-[0.12em] text-text-secondary">Order status · </span>
          {unavailableReason(book.fields.orderStatus)}
        </div>
      </Section>

      {book.orderLayer === "fill_events" ? <FillEventLog data={data} /> : null}
      {book.orderLayer === "decision_log" ? <DecisionLog data={data} /> : null}
      {book.orderLayer === "intent_log" ? <IntentJournal data={data} /> : null}
      {data.gateLadder ? <GateLadder data={data} /> : null}
    </div>
  );
}

function FillEventLog({ data }: { data: BookData }) {
  const { book, orders } = data;
  if (orders == null) {
    return <SourceUnavailable what="The fill-event log" reason="The order endpoint did not answer." />;
  }
  if (!orders.length) {
    return book.neverFired ? (
      <NeverFiredState book={book} initialCapital={data.facts.initialCapital} what="orders" />
    ) : (
      <MeasuredEmpty what="fill events" detail="The log exists and is empty for the loaded window." />
    );
  }
  return (
    <Section
      title="Fill-event log"
      icon={<ScrollText size={16} />}
      description="Every event that actually executed, newest first. open / close / partial_close are the only states this log can hold."
      rightSlot={<StatusBadge label={`${orders.length} events`} variant="neutral" />}
    >
      <div className="overflow-x-auto">
        <table className="w-full min-w-[900px] text-xs">
          <thead className="text-left text-text-muted">
            <tr>
              <th className="py-1">Time (IST)</th>
              <th>Symbol</th>
              <th>Event</th>
              <th>Side</th>
              <th>Price</th>
              <th>Quantity</th>
              <th>Reason</th>
              <th>Booked P&amp;L</th>
              <th>Remaining</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-bg-border/60">
            {orders.map((o) => (
              <tr key={o.id}>
                <td className="py-2 font-mono text-[10.5px] text-text-muted">{formatIST(o.time)}</td>
                <td className="font-semibold">{o.symbol}</td>
                <td>
                  <StatusBadge
                    label={o.action.replace(/_/g, " ")}
                    variant={o.action === "open" ? "info" : o.action === "partial_close" ? "warn" : "neutral"}
                    className="normal-case tracking-normal"
                  />
                </td>
                <td className="font-mono text-[11px]">{o.direction ?? "—"}</td>
                <td className="font-mono">{o.price == null ? <Unavailable reason="No fill price on this event." /> : price2(o.price)}</td>
                <td><QtyCell qty={o.qty} /></td>
                <td className="text-[11px] text-text-muted">{o.reason ?? "—"}</td>
                <td className={`font-mono ${pnlTone(o.pnl)}`}>
                  {o.pnl == null ? (
                    <Unavailable reason="An opening fill books no P&L; only closing and partial-closing events do." />
                  ) : (
                    signed0(o.pnl)
                  )}
                </td>
                <td className="font-mono text-[11px] text-text-muted">
                  {o.lotsRemaining == null ? "—" : `${o.lotsRemaining} lots`}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <AbsentFieldsNote book={book} fields={["orderStatus"]} />
    </Section>
  );
}

function DecisionLog({ data }: { data: BookData }) {
  const { book, decisions } = data;
  const [only, setOnly] = useState<"all" | "approved" | "declined">("all");
  const rows = useMemo(() => {
    const list = decisions ?? [];
    if (only === "approved") return list.filter((d) => d.approved === true);
    if (only === "declined") return list.filter((d) => d.approved === false);
    return list;
  }, [decisions, only]);

  if (decisions == null) {
    return <SourceUnavailable what="The decision log" reason="The paper-journal endpoint did not answer." />;
  }
  const approved = decisions.filter((d) => d.approved === true).length;
  const declined = decisions.filter((d) => d.approved === false).length;
  const unknown = decisions.length - approved - declined;

  return (
    <Section
      title="Decision log"
      icon={<ClipboardList size={16} />}
      description="Per-cycle accept / decline with the lane's own reason. This is a decision record, not an order lifecycle — no order id, no status, no venue."
      rightSlot={
        <div className="flex items-center gap-1">
          {(["all", "approved", "declined"] as const).map((k) => (
            <button
              key={k}
              type="button"
              onClick={() => setOnly(k)}
              className={`rounded-full border px-2.5 py-0.5 text-[10.5px] font-semibold uppercase tracking-[0.1em] transition-colors ${
                only === k
                  ? "border-accent-blue/50 bg-accent-blue/10 text-accent-blue"
                  : "border-bg-border text-text-muted hover:text-text-secondary"
              }`}
            >
              {k}
            </button>
          ))}
        </div>
      }
    >
      <div className="mb-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
        <MetricTile size="sm" label="Approved" value={String(approved)} detail="in the loaded window" />
        <MetricTile size="sm" label="Declined" value={String(declined)} detail="with a stated reason" />
        <MetricTile
          size="sm"
          label="No verdict"
          value={String(unknown)}
          detail={unknown ? "rows carrying no approved flag" : "every row carried a verdict"}
        />
        <MetricTile
          size="sm"
          label="Window"
          value={data.decisionsTotal == null ? String(decisions.length) : `${decisions.length} of ${data.decisionsTotal}`}
          detail="the API caps a journal page"
        />
      </div>
      {!rows.length ? (
        <MeasuredEmpty what="decisions in this filter" detail="Widen the filter to see the rest of the window." />
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[980px] text-xs">
            <thead className="text-left text-text-muted">
              <tr>
                <th className="py-1">Recorded (IST)</th>
                <th>Symbol</th>
                <th>Verdict</th>
                <th>Side</th>
                <th>Confidence</th>
                <th>Reason</th>
                <th>Readiness</th>
                <th>Data age</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-bg-border/60">
              {rows.map((d) => (
                <tr key={d.id}>
                  <td className="py-2 font-mono text-[10.5px] text-text-muted">{formatIST(d.time)}</td>
                  <td className="font-semibold">{d.symbol}</td>
                  <td>
                    {d.approved == null ? (
                      <Unavailable reason="This journal row carries no approved flag." />
                    ) : (
                      <StatusBadge
                        label={d.approved ? "approved" : "declined"}
                        variant={d.approved ? "info" : "neutral"}
                        className="normal-case tracking-normal"
                      />
                    )}
                  </td>
                  <td className="font-mono text-[11px]">{d.direction ?? "—"}</td>
                  <td className="font-mono">
                    {d.confidence == null ? <Unavailable reason="No confidence recorded." /> : formatNumber(d.confidence, 3)}
                  </td>
                  <td className="max-w-[360px] text-[11px] text-text-secondary">
                    {d.reasonIsFault ? (
                      <span className="text-accent-amber" title={d.reason ?? ""}>
                        runtime fault, not a decision — the reason field holds a raw error
                      </span>
                    ) : (
                      <span title={d.reason ?? ""} className="line-clamp-2">
                        {d.reason ?? "—"}
                      </span>
                    )}
                  </td>
                  <td className="text-[11px] text-text-muted">
                    {d.readinessMode ?? "—"}
                    {d.degradedReason ? <span className="block text-accent-amber">{d.degradedReason}</span> : null}
                  </td>
                  <td className="font-mono text-[10.5px] text-text-muted">
                    {d.spotAgeSeconds == null ? "spot —" : `spot ${Math.round(d.spotAgeSeconds)}s`}
                    <br />
                    {d.watchlistAgeSeconds == null ? "wl —" : `wl ${Math.round(d.watchlistAgeSeconds)}s`}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <AbsentFieldsNote book={book} fields={["orderStatus"]} />
    </Section>
  );
}

function IntentJournal({ data }: { data: BookData }) {
  const { book, intents } = data;
  if (intents == null) {
    return <SourceUnavailable what="The intent journal" reason="The paper-journal endpoint did not answer." />;
  }
  return (
    <Section
      title="Intent journal — context only, NOT an order book"
      icon={<ClipboardList size={16} />}
      description="Logged LONG/SHORT intents with their planned entry, stop and target. FLAT decisions and rejections are never written, and many intents collapse into one position, so intent count and position count are not comparable."
      rightSlot={
        <div className="flex items-center gap-2">
          <StatusBadge label="not an order book" variant="warn" />
          <StatusBadge
            label={data.intentsTotal == null ? `${intents.length} rows` : `${intents.length} of ${data.intentsTotal}`}
            variant="neutral"
          />
        </div>
      }
    >
      {!intents.length ? (
        <MeasuredEmpty what="intents" detail="The journal is empty for the loaded window." />
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[900px] text-xs">
            <thead className="text-left text-text-muted">
              <tr>
                <th className="py-1">Recorded (IST)</th>
                <th>Symbol</th>
                <th>Intent</th>
                <th>Agent</th>
                <th>Style</th>
                <th>Confidence</th>
                <th>Planned entry / stop / target</th>
                <th>Planned R/R</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-bg-border/60">
              {intents.map((i) => {
                const rr = rrRender({ entry: i.entry, stop: i.stop, target1: i.target });
                return (
                  <tr key={i.id}>
                    <td className="py-2 font-mono text-[10.5px] text-text-muted">{formatIST(i.time)}</td>
                    <td className="font-semibold">{i.symbol}</td>
                    <td>
                      <StatusBadge
                        label={i.action ?? "—"}
                        variant={i.action === "LONG" ? "info" : i.action === "SHORT" ? "warn" : "neutral"}
                        className="normal-case tracking-normal"
                      />
                    </td>
                    <td className="text-[11px] text-text-muted">{i.agent ?? "—"}</td>
                    <td className="text-[11px] text-text-muted">{i.executionStyle ?? "—"}</td>
                    <td className="font-mono">{i.confidence == null ? "—" : formatNumber(i.confidence, 3)}</td>
                    <td className="font-mono text-[11px]">
                      {[i.entry, i.stop, i.target].map((v, n) => (
                        <span key={n}>
                          {n ? " / " : ""}
                          {v == null ? "—" : formatNumber(v, 2)}
                        </span>
                      ))}
                    </td>
                    <td className={`font-mono text-[11px] ${rr.ok ? "text-text-primary" : "text-text-muted"}`} title={rr.ok ? rr.note : rr.reason}>
                      {rr.text}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      <AbsentFieldsNote book={book} fields={["orderStatus"]} />
    </Section>
  );
}

/** The one real rejection surface in this whole set: the convergence gates. */
function GateLadder({ data }: { data: BookData }) {
  const g = data.gateLadder!;
  const breakdown = Object.entries(g.breakdown ?? {}).sort((a, b) => b[1] - a[1]);
  return (
    <Section
      title="Why nothing fired — the gate ladder"
      icon={<ShieldQuestion size={16} />}
      description="Per-symbol gate results from the latest cycle. This is the lane's own record of what blocked an entry, and it is the closest thing to a rejection log any of these books has."
      rightSlot={g.generatedAt ? <StatusBadge label={`cycle ${formatIST(g.generatedAt)}`} variant="neutral" /> : undefined}
    >
      {!breakdown.length && !g.blocked.length ? (
        <MeasuredEmpty what="gate results" detail="The status endpoint carried no results for the latest cycle." />
      ) : (
        <>
          {breakdown.length ? (
            <div className="mb-3 flex flex-wrap gap-2">
              {breakdown.map(([gate, count]) => (
                <span
                  key={gate}
                  className="rounded-lg border border-bg-border bg-bg-primary/14 px-2.5 py-1 font-mono text-[11px] text-text-secondary"
                  title={`${count} symbol(s) blocked by ${gate} in the latest cycle`}
                >
                  {gate} <span className="text-accent-amber">×{count}</span>
                </span>
              ))}
            </div>
          ) : null}
          {g.blocked.length ? (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[760px] text-xs">
                <thead className="text-left text-text-muted">
                  <tr>
                    <th className="py-1">Symbol</th>
                    <th>Setup</th>
                    <th>Action</th>
                    <th>Blocked by</th>
                    <th>Plan R/R</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-bg-border/60">
                  {g.blocked.map((b) => {
                    const rr = rrRender(b.risk);
                    return (
                      <tr key={b.symbol}>
                        <td className="py-2 font-semibold">{b.symbol}</td>
                        <td>
                          <StatusBadge label={b.quality ?? "—"} variant="neutral" className="normal-case tracking-normal" />
                        </td>
                        <td className="font-mono text-[11px]">{b.action ?? "—"}</td>
                        <td className="text-[11px] text-accent-amber">{b.reasons.length ? b.reasons.join(", ") : "—"}</td>
                        <td className={`font-mono text-[11px] ${rr.ok ? "text-text-primary" : "text-text-muted"}`} title={rr.ok ? rr.note : rr.reason}>
                          {rr.text}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          ) : null}
        </>
      )}
    </Section>
  );
}

// ─── 2. TRADE BOOK ──────────────────────────────────────────────────────────

export function TradeBookView({ data }: { data: BookData }) {
  const { book, trades } = data;
  if (trades == null) {
    return (
      <div className="space-y-3">
        <SourceUnavailable what="The trade book" reason="The book endpoint did not answer." />
      </div>
    );
  }
  if (!trades.length) {
    return (
      <div className="space-y-3">
        {book.neverFired ? (
          <NeverFiredState book={book} initialCapital={data.facts.initialCapital} what="trades" />
        ) : (
          <MeasuredEmpty what="closed trades" detail="The book has closed nothing in the loaded window." />
        )}
      </div>
    );
  }
  const feeField = book.fields.fees;
  const slipField = book.fields.slippage;
  const rField = book.fields.rMultiple;

  return (
    <Section
      title="Trade book"
      icon={<ListChecks size={16} />}
      description="Executed fills — entry and exit legs with quantity in LOTS and UNITS, the recorded cost basis, realized P&L and the lane's own exit reason."
      rightSlot={
        <div className="flex flex-wrap items-center gap-2">
          <StatusBadge label={`${trades.length} closed`} variant="neutral" />
          {data.tradesTruncated ? (
            <StatusBadge
              label={`showing ${data.tradesTruncated.shown} of ${data.tradesTruncated.total ?? "?"}`}
              variant="warn"
            />
          ) : null}
          <StatusBadge
            label={feeField.state === "available" ? "P&L net of cost" : "P&L is GROSS"}
            variant={feeField.state === "available" ? "info" : "warn"}
          />
        </div>
      }
    >
      {data.tradesTruncated ? (
        <p className="mb-2 text-[11px] text-accent-amber">
          The API caps a page at {data.tradesTruncated.shown} rows and this book holds {data.tradesTruncated.total ?? "more"}.
          The rows below are the most recent closes, so today and the recent sessions are complete; older history is not on
          this page.
        </p>
      ) : null}
      <div className="overflow-x-auto">
        <table className="w-full min-w-[1240px] text-xs">
          <thead className="text-left text-text-muted">
            <tr>
              <th className="py-1">Instrument</th>
              <th>Side</th>
              <th>Opened → closed (IST)</th>
              <th>Entry → exit</th>
              <th>Quantity</th>
              <th>Cost</th>
              <th>Slippage</th>
              <th>Realized</th>
              <th>R</th>
              <th>Exit reason</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-bg-border/60">
            {trades.map((t) => (
              <tr key={t.id}>
                <td className="py-2.5">
                  <div className="font-semibold">{t.symbol}</div>
                  <div className="font-mono text-[9.5px] text-text-muted">{t.contract || t.underlying || ""}</div>
                </td>
                <td>
                  <StatusBadge
                    label={t.side}
                    variant={t.side === "SHORT" || t.side === "PE" ? "warn" : "info"}
                    className="normal-case tracking-normal"
                  />
                </td>
                <td className="whitespace-nowrap text-[10.5px] text-text-muted">
                  {formatIST(t.openedAt)}
                  <br />
                  {formatIST(t.closedAt)}
                </td>
                <td className="whitespace-nowrap font-mono text-[11px]">
                  {t.entry == null ? <Unavailable reason="No entry price on this row." /> : price2(t.entry)}
                  {" → "}
                  {t.exit == null ? <Unavailable reason="No exit price on this row." /> : price2(t.exit)}
                  {t.entrySpot != null || t.exitSpot != null ? (
                    <div className="text-[9.5px] text-text-muted">
                      spot {t.entrySpot == null ? "—" : price2(t.entrySpot)} → {t.exitSpot == null ? "—" : price2(t.exitSpot)}
                    </div>
                  ) : null}
                </td>
                <td>
                  <QtyCell qty={t.qty} />
                  {/* A partial close books P&L at more than one price, so the
                      single entry → exit pair times this quantity deliberately
                      does NOT reconcile with Realized. Say so rather than let a
                      reader multiply and conclude the number is wrong. */}
                  {t.partialExit ? (
                    <div
                      className="mt-0.5 cursor-help text-[9px] uppercase tracking-[0.1em] text-accent-amber"
                      title={`Closed in more than one exit${
                        t.lotsAtFinalExit != null ? ` — ${t.lotsAtFinalExit} lot(s) remained for the final exit` : ""
                      }. The quantity shown is the size ENTERED, and Realized spans every exit, so entry → exit × quantity will not equal it.`}
                    >
                      entered size · partial exit
                    </div>
                  ) : null}
                </td>
                <td>
                  <FieldValue value={t.cost} field={feeField} format={money0} />
                  {t.realizedGross != null ? (
                    <div className="text-[9.5px] text-text-muted">gross {signed0(t.realizedGross)}</div>
                  ) : null}
                </td>
                <td><FieldValue value={t.slippage} field={slipField} format={money0} /></td>
                <td className={`font-mono font-semibold ${pnlTone(t.realized)}`}>
                  {t.realized == null ? (
                    <Unavailable reason="This row carries no realized P&L." />
                  ) : (
                    signed0(t.realized)
                  )}
                </td>
                <td>
                  <FieldValue value={t.rMultiple} field={rField} format={(v) => `${formatNumber(v, 2)}R`} />
                </td>
                <td><ExitReasonChip reason={t.exitReason} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <AbsentFieldsNote book={book} fields={["fees", "slippage", "rMultiple"]} />
    </Section>
  );
}

// ─── 3. POSITIONS ───────────────────────────────────────────────────────────

export function PositionsView({ data, nowMs }: { data: BookData; nowMs: number }) {
  const { book, positions } = data;
  if (positions == null) {
    return <SourceUnavailable what="The open book" reason="The book endpoint did not answer." />;
  }
  if (!positions.length) {
    return book.neverFired ? (
      <NeverFiredState book={book} initialCapital={data.facts.initialCapital} what="positions" />
    ) : (
      <MeasuredEmpty what="open positions" detail="The book is measured-flat: nothing is open right now." />
    );
  }
  const dteField = book.fields.dte;
  const exitField = book.fields.exitPlan;

  return (
    <Section
      title="Open positions"
      icon={<Target size={16} />}
      description="Live from the authoritative book. Unrealised P&L is suppressed wherever the mark is too old to act on — a stale mark makes it UNKNOWN, not last-known."
      rightSlot={<StatusBadge label={`${positions.length} open`} variant="info" />}
    >
      <div className="space-y-2">
        {positions.map((p) => {
          const mv = markVerdict(p.markAsOf, p.markClock, nowMs);
          const dte = dteFromExpiry(p.expiry, nowMs);
          const ageMinutes =
            p.openedAt != null ? Math.max(0, (nowMs - new Date(normalizeIso(p.openedAt)).getTime()) / 60000) : null;
          // R/R is measured against the entry on the SAME scale as the stop and
          // target. On an underlying-level plan that is the entry SPOT — feeding
          // the option premium in produces a ratio with no meaning.
          const rr = rrRender({ entry: p.planEntry, stop: p.stop, target1: p.target });
          const planNote = PLAN_BASIS_NOTE[p.planBasis];
          return (
            <div key={p.id} className="rounded-xl border border-bg-border bg-bg-primary/12 p-3">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-semibold text-text-primary">{p.symbol}</span>
                <StatusBadge
                  label={p.side}
                  variant={p.side === "SHORT" || p.side === "PE" ? "warn" : "info"}
                  className="normal-case tracking-normal"
                />
                {p.contract ? <span className="font-mono text-[10px] text-text-muted">{p.contract}</span> : null}
                <MarkAgeChip verdict={mv} />
                {p.markSource ? <StatusBadge label={`mark via ${p.markSource}`} variant="neutral" /> : null}
                <span className="ml-auto text-[11px] text-text-muted">
                  opened {formatIST(p.openedAt)}
                  {ageMinutes != null ? ` · ${formatAge(ageMinutes)} old` : ""}
                </span>
              </div>

              <div className="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
                <Cell label="Entry" value={p.entry == null ? null : price2(p.entry)} />
                <Cell
                  label="Mark"
                  value={p.mark == null ? null : price2(p.mark)}
                  tone={mv.state === "stale" ? "text-accent-amber" : undefined}
                />
                <div>
                  <div className="text-[9.5px] uppercase tracking-[0.14em] text-text-muted">Quantity</div>
                  <div className="mt-0.5"><QtyCell qty={p.qty} /></div>
                </div>
                <div>
                  <div className="text-[9.5px] uppercase tracking-[0.14em] text-text-muted">Unrealised</div>
                  <div className="mt-0.5"><UnrealizedCell value={p.unrealized} verdict={mv} /></div>
                </div>
                <div>
                  <div className="text-[9.5px] uppercase tracking-[0.14em] text-text-muted">Expiry / DTE</div>
                  <div className="mt-0.5 font-mono text-[11px]">
                    {p.expiry == null || dte == null ? (
                      <Unavailable reason={unavailableReason(dteField) ?? "No expiry date is recorded on this row."} />
                    ) : (
                      <>
                        {p.expiry} <span className="text-text-muted">· {dte}d</span>
                      </>
                    )}
                  </div>
                </div>
                <div>
                  <div className="text-[9.5px] uppercase tracking-[0.14em] text-text-muted">R/R</div>
                  <div className="mt-0.5 font-mono text-[11px]" title={`${rr.ok ? rr.note : rr.reason} — ${planNote}`}>
                    {rr.ok ? (
                      <>
                        <span className="text-text-primary">{rr.text}</span>
                        {p.planBasis === "underlying" ? (
                          <span className="ml-1 text-[9px] uppercase tracking-[0.1em] text-text-muted">
                            on the underlying
                          </span>
                        ) : null}
                      </>
                    ) : (
                      <Unavailable
                        reason={`${rr.reason}. ${planNote} ${unavailableReason(exitField) ?? ""}`}
                        label="R/R UNAVAILABLE"
                      />
                    )}
                  </div>
                </div>
              </div>

              <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-[10.5px] text-text-muted">
                <span title={planNote}>
                  stop{p.planBasis === "underlying" ? " (underlying)" : ""}{" "}
                  {p.stop == null ? <Unavailable reason={unavailableReason(exitField) ?? "No stop recorded."} /> : price2(p.stop)}
                </span>
                <span title={planNote}>
                  target{p.planBasis === "underlying" ? " (underlying)" : ""}{" "}
                  {p.target == null ? <Unavailable reason={unavailableReason(exitField) ?? "No target recorded."} /> : price2(p.target)}
                </span>
                {p.entrySpot != null ? <span>entry spot {price2(p.entrySpot)}</span> : null}
                {p.regime ? <span>regime {p.regime}</span> : null}
              </div>
              {p.reason ? <div className="mt-1 text-[10.5px] leading-relaxed text-text-muted">{p.reason}</div> : null}
            </div>
          );
        })}
      </div>
      <AbsentFieldsNote book={book} fields={["exitPlan", "markClock", "dte"]} />
    </Section>
  );
}

function Cell({ label, value, tone }: { label: string; value: string | null; tone?: string }) {
  return (
    <div>
      <div className="text-[9.5px] uppercase tracking-[0.14em] text-text-muted">{label}</div>
      <div className={`mt-0.5 font-mono text-[11px] ${tone ?? "text-text-primary"}`}>
        {value ?? <Unavailable reason="Not recorded on this row." />}
      </div>
    </div>
  );
}

function normalizeIso(v: string): string {
  const hasTz = /[zZ]$|[+-]\d{2}:?\d{2}$/.test(v);
  const isDateTime = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/.test(v);
  return isDateTime && !hasTz ? `${v.replace(" ", "T")}Z` : v;
}

function formatAge(minutes: number): string {
  if (minutes < 60) return `${Math.round(minutes)}m`;
  const h = Math.floor(minutes / 60);
  if (h < 48) return `${h}h`;
  return `${Math.floor(h / 24)}d`;
}

// ─── 4. PORTFOLIO ───────────────────────────────────────────────────────────

export function PortfolioView({ data, nowMs }: { data: BookData; nowMs: number }) {
  const { book, facts, day } = data;
  const exposure = useMemo(() => notionalExposure(data.positions ?? []), [data.positions]);
  // The stale-mark rule applies to the ROLL-UP too. The directional summary
  // reported +9,760 unrealised on 2026-07-27 while all six of its marks were
  // 2-5 days old and the positions view refused every one of them; printing the
  // roll-up regardless just moves the same lie one tab across.
  const unrealized = useMemo(
    () =>
      portfolioUnrealized({
        reported: facts.unrealized,
        openCount: facts.openCount,
        marks: data.positions == null ? null : data.positions.map((p) => markVerdict(p.markAsOf, p.markClock, nowMs)),
      }),
    [facts.unrealized, facts.openCount, data.positions, nowMs],
  );
  const total = useMemo(() => totalPnl(facts.realizedLifetime, unrealized), [facts.realizedLifetime, unrealized]);

  return (
    <div className="space-y-3">
      <Section
        title="Today"
        icon={<Wallet size={16} />}
        description="The day figures, kept structurally apart from lifetime. A lifetime number never appears under this heading."
      >
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
          <DayTile day={day} />
          {/* The lane's own circuit-breaker day counter is only a TODAY figure
              on a day the lane actually ran. It is not reset overnight, so on a
              day with no session it still holds the LAST session's number —
              printing that under a "Today" heading is precisely the defect
              these pages exist to remove. */}
          {data.dayPnlLive != null ? (
            day.state === "served" || day.state === "derived" ? (
              <MetricTile
                label="Circuit-breaker day P&L"
                value={formatSignedMoney(data.dayPnlLive)}
                detail="the lane's own live day counter"
                color={data.dayPnlLive >= 0 ? "text-accent-green" : "text-accent-red"}
              />
            ) : (
              <MetricTile
                label="Circuit-breaker counter"
                value="NOT TODAY'S"
                detail={`the counter still holds ${formatSignedMoney(data.dayPnlLive)} from the last session it ran${
                  day.state === "no_session_today" && day.lastSessionDay ? ` (${day.lastSessionDay})` : ""
                } — it is not a figure for today`}
              />
            )
          ) : null}
          <MetricTile
            label="Opened today"
            value={data.opensToday == null ? "UNAVAILABLE" : String(data.opensToday)}
            detail={data.opensToday == null ? "the book serves no opens-today counter" : "server-side, IST-keyed"}
          />
          <MetricTile
            label="Closed today"
            value={data.closesToday == null ? "UNAVAILABLE" : String(data.closesToday)}
            detail={data.closesToday == null ? "the book serves no closes-today counter" : "server-side, IST-keyed"}
          />
          {data.cooldownSkipsToday != null ? (
            <MetricTile label="Cooldown skips today" value={String(data.cooldownSkipsToday)} detail="entries suppressed by cooldown" />
          ) : null}
        </div>
        <DayLifetimeNote book={book} day={day} />
      </Section>

      <Section
        title="Lifetime"
        icon={<BarChart3 size={16} />}
        description="Since this book's inception. Every tile is read from the book's own summary — nothing is summed across lanes here."
      >
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
          <Tile label="Initial capital" v={facts.initialCapital} fmt={money0} />
          <Tile label="Equity" v={facts.equity} fmt={money0} />
          <Tile label="Realized (lifetime)" v={facts.realizedLifetime} fmt={signed0} signed />
          <UnrealizedRollupTile verdict={unrealized} />
          <Tile label="Open positions" v={facts.openCount} fmt={(v) => String(v)} />
          <Tile label="Closed trades" v={facts.closedCount} fmt={(v) => String(v)} />
        </div>
        <div className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
          <Tile label="Win rate" v={facts.winRate} fmt={(v) => formatPct(v, 1)} />
          <Tile label="Profit factor" v={facts.profitFactor} fmt={(v) => (Number.isFinite(v) ? formatNumber(v, 2) : "∞")} />
          {/* Some books serve the drawdown as a ₹ magnitude, others only as a
              percentage of capital. Show whichever is actually carried and say
              which — converting one into the other needs a peak-equity series
              these summaries do not provide. */}
          {facts.maxDrawdown != null ? (
            <Tile
              label="Max drawdown"
              v={facts.maxDrawdown}
              fmt={(v) => money0(Math.abs(v))}
              detail={facts.maxDrawdownPct != null ? `${formatNumber(facts.maxDrawdownPct, 2)}% of capital` : "₹ magnitude"}
            />
          ) : (
            <Tile
              label="Max drawdown"
              v={facts.maxDrawdownPct}
              fmt={(v) => `${formatNumber(Math.abs(v), 2)}%`}
              detail="of capital — this book serves no ₹ magnitude"
              absentReason="This book's summary carries neither a drawdown magnitude nor a drawdown percentage."
            />
          )}
          <Tile
            label="Reserved margin"
            v={facts.reservedMargin}
            fmt={money0}
            detail="a real capital reservation"
            absentReason={unavailableReason(book.fields.exposure) ?? undefined}
          />
          <div className="rounded-2xl border border-bg-border bg-bg-secondary/28 px-4 py-3">
            <div className="text-[10.5px] uppercase tracking-[0.16em] text-text-muted">Notional exposure</div>
            <div className="mt-1 font-mono text-lg font-semibold text-text-primary">
              {exposure.value == null ? (
                <span className="text-text-muted">UNAVAILABLE</span>
              ) : (
                money0(exposure.value)
              )}
            </div>
            <div className="mt-0.5 text-[11px] text-text-muted">
              {exposure.value == null
                ? "no open row carried both a price and a unit count"
                : `notional, not margin · ${exposure.counted} row${exposure.counted === 1 ? "" : "s"}${
                    exposure.skipped ? ` · ${exposure.skipped} skipped for want of a price or units` : ""
                  }`}
            </div>
          </div>
          {/* Never a partial sum wearing a total's label: a component that is
              UNKNOWN or absent is not zero, so the total is refused. */}
          <Tile label="Total P&L" v={total.value} fmt={signed0} signed detail={total.note} absentReason={total.note} />
        </div>
        {data.perExitReason && Object.keys(data.perExitReason).length ? (
          <div className="mt-4">
            <div className="mb-1 text-[10px] uppercase tracking-[0.16em] text-text-muted">By exit reason</div>
            <div className="overflow-x-auto">
              <table className="w-full min-w-[620px] text-xs">
                <thead className="text-left text-text-muted">
                  <tr><th className="py-1">Reason</th><th>Trades</th><th>Wins</th><th>Win rate</th><th>P&amp;L</th><th>Avg R</th></tr>
                </thead>
                <tbody className="divide-y divide-bg-border/60">
                  {Object.entries(data.perExitReason).map(([reason, s]) => (
                    <tr key={reason}>
                      <td className="py-2"><ExitReasonChip reason={reason} /></td>
                      <td className="font-mono">{s.trades ?? "—"}</td>
                      <td className="font-mono">{s.wins ?? "—"}</td>
                      <td className="font-mono">{s.win_rate == null ? "—" : formatPct(s.win_rate, 1)}</td>
                      <td className={`font-mono ${pnlTone(s.pnl ?? null)}`}>{s.pnl == null ? "—" : signed0(s.pnl)}</td>
                      <td className="font-mono">{s.avg_r == null ? "—" : formatNumber(s.avg_r, 2)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        ) : null}
      </Section>
    </div>
  );
}

function Tile({
  label,
  v,
  fmt,
  signed,
  detail,
  absentReason,
}: {
  label: string;
  v: number | null;
  fmt: (n: number) => string;
  signed?: boolean;
  detail?: string;
  absentReason?: string;
}) {
  if (v == null) {
    return (
      <div className="rounded-2xl border border-bg-border bg-bg-secondary/28 px-4 py-3" title={absentReason}>
        <div className="text-[10.5px] uppercase tracking-[0.16em] text-text-muted">{label}</div>
        <div className="mt-1 font-mono text-lg font-semibold text-text-muted">UNAVAILABLE</div>
        <div className="mt-0.5 text-[11px] text-text-muted">{absentReason ?? "this book does not carry it"}</div>
      </div>
    );
  }
  return (
    <MetricTile
      label={label}
      value={fmt(v)}
      detail={detail}
      color={signed ? (v > 0 ? "text-accent-green" : v < 0 ? "text-accent-red" : undefined) : undefined}
    />
  );
}

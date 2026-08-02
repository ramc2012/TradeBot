# UI Consolidation Plan — 2026-08-02

> **STATUS (2026-08-02 evening): Phases 0–3 SHIPPED** on branch `ui/consolidation-0-3`
> (commits c8837bd4, a6d003a0, c52a98ca, 44c7cec0, 21b338d1), built and deployed to
> nomadcurie_frontend_v2. Deviations from the plan as written:
> - #2 (duplicate directional nav row): NOT changed — the double listing is documented
>   in nav-model.ts as deliberate (dual-horizon lane, distinct labels per section).
> - #11: analytics `SectionTitle` and market `PanelHeader` were left in place — they are
>   section headers, not re-implementations of any desk-ui primitive.
> - #13: market's inline `RrgMap` kept (different idiom: HTML chip map on 100-centered
>   JdK coords); folded into the Phase-4 market split instead. sector+cbe now share
>   `strategies/shared/RrgScatter` via thin domain adapters.
> - #14 became: move `OptionChartModal` + `OptionStudyChart` from nse/ to shared/
>   (3 cross-lane consumers; also fixed the pre-existing shared→nse import).
> - Bonus fix found during verification: directional + fractal paper-position endpoints
>   cap `limit` at 200; the old /reports asked for 500 and silently dropped both desks
>   (480 → 702 rows after the fix).
> Phase 4 remains open (items 15–19 below, owner sign-off per item).

Scope: `frontend-v2`. Constraint from the owner: **per-lane views stay per-lane** — nothing under
`/strategies/<lane>` loses its own route. Everything else is fair game.

Ground truth this plan is based on: 42 routed pages, 169 components. The full duplication survey
(page → purpose → endpoints → overlaps) was done on 2026-08-02; key facts are restated inline so
this doc stands alone.

---

## What is NOT broken (leave alone)

- `components/strategies/shared/*` is a working consolidation point — `SignalQualityTab` has 12
  consumers, `CandleChart` 9, `MarketProfileChart` 7, etc. Lane desks are **not** copy-pasting
  positions tables/PnL cards; they import these.
- The three `*/books` subpages are 15-line wrappers around one `components/books/LaneBooksDesk.tsx`
  driven by the `lib/lane-books.ts` registry. This is the model the rest of the app should follow.
- Redirect-only routes (`strategies/{auction,directional,fractal}/live`, `sector-interaction/[sector]`,
  the 13 legacy redirects in `next.config.js`) are cheap deep-link preservation. Keep.

---

## Phase 0 — Free wins (no behavior change, ~1 sitting)

1. **Delete the entire `components/v1-*` tree.** ~19,000 lines across 23 entry files
   (`v1-charts`, `v1-orderflow`, `v1-layout`, `v1-system`, `v1-auth`, `v1-strategy`,
   `v1-directional-options`, `v1-auction-intelligence`, `v1-fractal-market-profile`,
   `v1-mp-intelligence`, `v1-cbe`, `v1-gann-tp-delta`, `v1-macro-research`,
   `v1-sector-interaction`, `v1-trading`, `v1-live`, `v1-errors`). Zero imports from `app/`,
   `lib/`, `hooks/`, or any live component — verified by path-grep. Git history keeps them.
   This alone removes most "same filename, two versions" confusion (`ChartsWorkbench`,
   `OrderflowWorkbench`, `Sidebar`, the four directional panels).
2. **Fix the duplicate nav row**: `/strategies/directional` appears twice in `LANE_SECTIONS`
   (plain + `?horizon=positional`). Keep one, or label the second as a distinct child entry.
3. **Extract the byte-identical `normalizePositionsOverview`** out of `app/positions/page.tsx:34`
   and `app/analytics/page.tsx:45` into `lib/strategy-position-ledger.ts`. Pure move.

## Phase 1 — Route collapse: make tab shells canonical (~1 day)

`/system` and `/research` already ARE the consolidation — they import other pages' default exports
as tabs. Finish the job:

4. `/health`, `/lane-health` → thin `redirect()` to `/system?tab=budgets` / `/system?tab=lanes`.
   `/backtester`, `/data`, `/analysis` → `redirect()` to `/research?tab=…`. Move the real
   components out of `app/*/page.tsx` into `components/` (a page importing another route's page
   default export is fragile under App Router).
5. While folding `/lane-health` in, port it to `desk-ui` tokens — it is the only page still on raw
   Tailwind greys + emoji status glyphs.
6. **One Services table.** `/system` tab "Services" renders service health from
   `/api/system/overview`; the embedded `ServiceHealthBoard` renders it again from
   `/api/system/health` (which also has the `/ws/system-health` socket). Keep the
   `ServiceHealthBoard` version (socket-fed), fold the overview-only metrics (trading allowed,
   blockers) into its header, and drop the second table.
   Lane state still appears via three endpoint families (`/api/lane-health/*`,
   `system/health.strategy_lanes`, `/api/system/lanes`) — UI shows them in one place after this
   phase; unifying the backend endpoints is a separate, optional backend task.

## Phase 2 — The positions/P&L quadruplication (highest-value, ~2 days)

Today the same paper-book dataset renders on four surfaces with three different fetch stacks:
`/positions` + `/analytics` (shared snapshot + socket + row builders), `/reports`
(second aggregator `lib/reports-ledger.ts`, own normalizer/row type, same six desks), and the
landing roll-up (eight per-lane summary endpoints). Numbers can disagree between them.

7. **Merge `lib/reports-ledger.ts` into `lib/strategy-position-ledger.ts`** — one normalizer, one
   row type; reports become "closed rows + date filter + CSV export" over the shared ledger.
8. **Fold `/reports` into `/positions` as a "Reports" tab** (closed-trade ledger, KPI cards, CSV).
   `/reports` route becomes a redirect.
9. **De-duplicate `/analytics` vs `/positions`**: `/analytics` keeps what is unique — equity
   curves, per-strategy curves, commodity P&L verification/audit — and drops its Open Positions
   table and Recent Strategy Exits (both live on `/positions`); link across instead.
10. Landing tiles: keep the cheap summary endpoints, but add a one-line "as-of" and link to
    `/positions` as the authoritative ledger. (Optional later: feed tiles from the same snapshot.)

End state: **one ledger library, one live-book page (+Reports tab), one analytics page** with
disjoint content.

## Phase 3 — Component dedup (~1 day, mechanical)

11. Promote the five live local re-implementations of `desk-ui` primitives to imports:
    `MetricTile` (`app/positions/page.tsx:153`, `app/market/page.tsx:508`), `StatusBadge` +
    `PanelHeader` (`app/market/page.tsx`), `MetricCard`/`SectionTitle` (`app/analytics/page.tsx`),
    `KpiCard` (`app/reports/page.tsx:50`), `StatBox` (`app/lane-health/page.tsx:179`).
12. **One Sparkline**: merge `strategies/cbe/Sparkline.tsx` and `strategies/macro/Sparkline.tsx`
    into `desk-ui/Sparkline.tsx` (superset props: stroke/fill/gradient/end-dot).
13. **One RRG scatter**: canonicalize `strategies/sector/RrgChart.tsx`; migrate
    `strategies/cbe/RrgScatter.tsx` consumers (note `SectorRotation.tsx` imports `QUADRANT_COLOR`
    from it — move the palette to the shared module) and replace the inline `RrgMap` at
    `app/market/page.tsx:663`.
14. `macd-refined/MacdRefinedDesk.tsx` is the only lane desk not using `strategies/shared` —
    migrate its chart/mark cells to the shared imports (route and layout unchanged; per-lane
    constraint respected).

## Phase 4 — Larger refactors (owner sign-off per item)

15. **`/agent` + `/trading` → one Ops surface.** Both poll `/api/trading/strategy-agent/status`
    and POST `run-once`. Proposal: `/trading` is canonical (kill switch, blotter, risk); fold
    `/agent`'s Runtime/Data-quality/Commentary in as tabs; broker sessions live on `/settings`
    only (the `/system` Brokers tab already just links there). `/agent` becomes a redirect.
16. **`app/market/page.tsx` (2,478 lines) split.** Two unrelated modes in one file: "Live market
    tools" (option chain, ATM watchlist, RRG) vs "NSE+MCX Research" (analytics grids). Split into
    `components/market/LiveTools.tsx` + `components/market/ResearchBoard.tsx`; consider moving the
    Research mode into `/research` as a fourth tab.
17. **`app/strategies/commodity/live/page.tsx` (3,887 lines).** ~40 locally-defined components
    mirror `strategies/shared` + `desk-ui` + `BookPrimitives`; its `PositionsTab`/`OrdersTab`/
    `TradesTab` trio re-implements `LaneBooksDesk` with no shared code. Proposal: register the
    commodity lane in `lib/lane-books.ts` and add `/strategies/commodity/books`, then shrink the
    cockpit to lane-specific panels (TPO chart, trigger badges, audit feed). Do LAST — it is the
    live commodity cockpit; needs visual parity checks. Per-lane route unchanged.
18. **Rotation cluster** (`/sector-interaction`, `/macro-research`, cbe rotation tab, `/market`
    RRG — four surfaces, four endpoint families). UI-side: after #13 they at least share the
    chart. Full merge into one "Rotation" page is as much a backend question (four endpoint
    families) as a UI one — decide separately.
19. `/strategies/commodity/page.tsx` aliasing `./live/page` (identical 3,887-line render at two
    URLs): make one a redirect to match the auction/directional/fractal pattern.

---

## Sequencing and risk

| Phase | Effort | Risk | Removes |
|---|---|---|---|
| 0 | hours | none (dead code + pure moves) | ~19k lines, same-name confusion |
| 1 | ~1 day | low (routes become redirects) | 5 standalone routes, 1 duplicate table |
| 2 | ~2 days | medium (P&L surfaces — verify numbers match before/after) | 1 aggregator lib, 1 route, 2 duplicate tables |
| 3 | ~1 day | low-medium (visual diffs) | ~8 duplicate components |
| 4 | multi-day, itemized | medium-high | 2 mega-files, 1-2 routes |

Suggested order: 0 → 1 → 3 → 2 → 4 (3 before 2 so the merged pages are built from shared
primitives). Phases 0–3 need no backend changes. Nothing in this plan touches
`/strategies/<lane>` routes except through shared-component imports (#14, #17).

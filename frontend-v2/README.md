# Nomad Curie · Frontend v2

Reorganised trader workspace. Runs alongside `frontend/` (the v1 app) on
port **3001** against the same backend (port 8000). v1 stays on port 3000
until v2 reaches feature parity.

## Why v2 exists

The v1 frontend grew into 24 routes and 17 workspace components
totalling ~25 K lines of JSX. Each desk re-implemented its own
`formatPct`, `MetricTile`, `tone()`, tab bar, and refresh cadence.
There were three overlapping system-status pages (`/`, `/health`,
`/lane-health`), three split research routes (`/analysis`,
`/backtester`, `/data`) presented as tabs in `/analysis` but with no
URL sync, and eight strategy desks side-by-side with no consistent
shell.

v2 is the redesign laid out in the codebase audit:

- Five sidebar groups by **what the trader is doing** (Trade /
  Strategies / Market / Research / System) instead of "Operate /
  Validate / System".
- Every strategy desk under `/strategies/<name>` with a shared
  `DeskShell` (header + URL-synced tab bar + status chips) so
  switching between desks doesn't reset the trader's spatial map.
- `/system` consolidates v1's `/health` + `/lane-health`.
- `/research` consolidates `/analysis` + `/backtester` + `/data`.
- A `desk-ui/` primitives module that replaces the 8 hand-rolled
  copies of `formatPct` / `MetricTile` / `tone` / tab bars.
- A `usePaperDeskQueries` hook that replaces the 30s
  summary/positions/journal triple-query duplicated across desks.

No v1 URLs break — `next.config.js` redirects every old route to its
new location (e.g. `/directional-options` → `/strategies/directional`,
`/health` → `/system?tab=health`).

## Layout

```
src/
├── app/
│   ├── layout.tsx              ← TopBar + Sidebar (NEW grouping) + main
│   ├── page.tsx                ← Overview
│   ├── strategies/
│   │   ├── layout.tsx          ← Pass-through
│   │   ├── directional/page.tsx  ← Built natively on desk-ui (prototype)
│   │   ├── nse | cbe | gann | commodity | auction | fractal | mp/page.tsx
│   │   │                         ← DeskStub linking to v1 (not yet ported)
│   ├── system/page.tsx         ← Consolidated health + lanes + brokers + budgets
│   ├── research/page.tsx       ← Consolidated backtests + data + validation
│   └── <other routes>/page.tsx ← PageStub linking to v1 (not yet ported)
└── components/
    ├── desk-ui/                ← Primitives module (the deduplication win)
    │   ├── index.ts
    │   ├── formatters.ts       ← formatMoney / formatPct / formatIST / …
    │   ├── tones.ts            ← tone / regimeTone / directionTone / …
    │   ├── refresh.ts          ← REFRESH_MS = {live,snapshot,summary,slow}
    │   ├── MetricTile.tsx
    │   ├── StatusBadge.tsx
    │   ├── Section.tsx
    │   └── DeskShell.tsx       ← Universal desk header + URL-synced tab bar
    ├── layout/
    │   ├── Sidebar.tsx         ← 5-group nav
    │   └── TopBar.tsx
    ├── strategies/directional/ ← v2 directional desk (4 panels)
    ├── DeskStub.tsx            ← For not-yet-ported strategy desks
    └── PageStub.tsx            ← For not-yet-ported top-level pages
```

## Running v2 locally

```bash
# Start v1 (port 3000) + backend + db + redis as usual:
docker compose up -d

# Start v2 alongside (port 3001):
docker compose --profile v2 up -d frontend-v2

# Open:
#   http://localhost:3000  ← v1 (production)
#   http://localhost:3001  ← v2 preview
```

Or natively (recommended for development):

```bash
cd frontend-v2
npm install
NEXT_PUBLIC_API_URL=http://localhost:8000 npm run dev
```

## Migration status

| Surface | v2 status |
|---|---|
| Overview (`/`) | **Live** — cross-lane book + per-desk paper summaries |
| `/system` (was `/health` + `/lane-health`) | **Skeleton** — Services tab fetches `/api/system/overview`; other tabs stub to v1 |
| `/research` (was `/analysis` + `/backtester` + `/data`) | **Skeleton** — tab-routed shell, content stubs to v1 |
| `/strategies/directional` | **Live** — full port; native on desk-ui primitives |
| `/strategies/{nse,cbe,gann,commodity,auction,fractal,mp}` | DeskStub — uses v2 shell, content links to v1 |
| `/trading`, `/positions`, `/analytics`, `/market`, `/charts`, `/orderflow`, `/sector-interaction`, `/macro-research`, `/agent`, `/settings` | PageStub — link to v1 |

## Porting a desk to v2

The directional desk is the reference. To port another desk:

1. Create `src/components/strategies/<name>/<Name>Desk.tsx`.
2. Wrap content in `<DeskShell>` with the desk's tabs and `v1Href`.
3. Use `usePaperDeskQueries({ deskKey, endpoints })` for the paper tab.
4. Use `desk-ui` primitives (`MetricTile`, `Section`, `StatusBadge`,
   `formatMoney`, `tone`, …) — never roll your own.
5. Replace the matching stub in `src/app/strategies/<name>/page.tsx`.
6. Smoke-test against the live backend. The redirect in
   `next.config.js` already routes the old URL here.

## Deprecating v1

When every desk and top-level surface is ported and stable:

1. Switch the redirects in `next.config.js` from 307 → 308.
2. Update the v1 `frontend/` `Dockerfile` to publish on port 3000 via
   the v2 build instead (or rename `frontend-v2` → `frontend` and
   delete the old code).
3. Drop the v2 profile gate in `docker-compose.yml`.
4. Update GH Actions deploy to push v2 only.

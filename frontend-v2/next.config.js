/**
 * v2 frontend — runs on port 3001 against the same backend (port 8000).
 *
 * The redirect table here makes every old URL (the v1 routes) land on
 * the new structure. This is what lets us perfect v2 in isolation
 * without breaking existing bookmarks: anyone who hits e.g.
 * /directional-options on this domain gets sent to /strategies/directional.
 *
 * `permanent: false` (307) on purpose — once v1 is fully deprecated we
 * flip these to permanent (308). For now we want browsers NOT to cache.
 */
const REDIRECTS = [
  // Strategy desks → /strategies/<name>
  { source: "/strategy",                  destination: "/strategies/nse" },
  { source: "/directional-options",       destination: "/strategies/directional" },
  { source: "/cbe",                       destination: "/strategies/cbe" },
  { source: "/auction-intelligence",      destination: "/strategies/auction" },
  { source: "/fractal-market-profile",    destination: "/strategies/fractal" },
  { source: "/mp-intelligence",           destination: "/strategies/mp" },
  { source: "/gann-tp-delta",             destination: "/strategies/gann" },
  { source: "/commodity",                 destination: "/strategies/commodity" },

  // System surfaces → /system?tab=…
  { source: "/health",                    destination: "/system?tab=health" },
  { source: "/lane-health",               destination: "/system?tab=lanes" },

  // Research surfaces → /research?tab=…
  { source: "/analysis",                  destination: "/research?tab=validation" },
  { source: "/backtester",                destination: "/research?tab=backtests" },
  { source: "/data",                      destination: "/research?tab=data" },

  // Live variants — keep the /strategies/<name>/live shape.
  { source: "/strategy/live",                  destination: "/strategies/nse/live" },
  { source: "/directional-options/live",       destination: "/strategies/directional/live" },
  { source: "/auction-intelligence/live",      destination: "/strategies/auction/live" },
  { source: "/fractal-market-profile/live",    destination: "/strategies/fractal/live" },
  { source: "/commodity/live",                 destination: "/strategies/commodity/live" },
];

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  async redirects() {
    return REDIRECTS.map((r) => ({ ...r, permanent: false }));
  },
};

module.exports = nextConfig;

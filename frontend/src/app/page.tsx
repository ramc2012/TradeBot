import Link from "next/link";
import {
  Activity,
  ArrowRight,
  BarChart3,
  Bot,
  Database,
  FlaskConical,
  Globe,
  Layers3,
  Settings,
  TrendingUp,
  type LucideIcon,
} from "lucide-react";

const LIVE_SURFACES = [
  {
    href: "/analysis",
    label: "Research Monitor",
    icon: Activity,
    description: "Cache growth, rate-limit pauses, validation updates, and MACD backtest runs.",
    cadence: "Dedicated live polling",
  },
  {
    href: "/trading",
    label: "Execution",
    icon: TrendingUp,
    description: "Positions, proposals, broker mode, and manual execution controls.",
    cadence: "WebSocket-first",
  },
  {
    href: "/auction-intelligence",
    label: "Auction Intelligence",
    icon: Layers3,
    description: "Market Profile, order flow, sleeve decisions, and paper-validation flow for the new MP stack.",
    cadence: "Demo + paper validation",
  },
  {
    href: "/market",
    label: "Market",
    icon: Globe,
    description: "Chain views, macro state, and market context without research-cache noise.",
    cadence: "On-demand refresh",
  },
  {
    href: "/agent",
    label: "Agent",
    icon: Bot,
    description: "Curie reasoning, scan output, and operator review flow.",
    cadence: "Event-driven",
  },
];

const SECONDARY_SURFACES = [
  {
    href: "/analytics",
    label: "Analytics",
    icon: BarChart3,
    description: "Portfolio performance and historical analytics.",
  },
  {
    href: "/backtester",
    label: "Backtester",
    icon: FlaskConical,
    description: "Generic walk-forward and scenario testing.",
  },
  {
    href: "/data",
    label: "F&O Data",
    icon: Database,
    description: "Raw data views and catalog inspection.",
  },
  {
    href: "/settings",
    label: "Settings",
    icon: Settings,
    description: "Broker connections, credentials, and configuration.",
  },
];

function SurfaceCard({
  href,
  label,
  description,
  cadence,
  icon: Icon,
}: {
  href: string;
  label: string;
  description: string;
  cadence?: string;
  icon: LucideIcon;
}) {
  return (
    <Link
      href={href}
      className="group rounded-2xl border border-bg-border bg-bg-secondary/55 p-4 transition-all duration-200 hover:border-accent-blue/35 hover:bg-bg-secondary/80"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="space-y-2">
          <div className="flex items-center gap-2">
            <div className="rounded-lg border border-accent-blue/20 bg-accent-blue/10 p-2 text-accent-blue">
              <Icon size={16} />
            </div>
            <div>
              <div className="text-sm font-semibold text-text-primary">{label}</div>
              {cadence && (
                <div className="text-[11px] uppercase tracking-[0.14em] text-text-muted">
                  {cadence}
                </div>
              )}
            </div>
          </div>
          <p className="text-sm leading-6 text-text-secondary">{description}</p>
        </div>
        <ArrowRight
          size={16}
          className="mt-1 shrink-0 text-text-muted transition-transform duration-200 group-hover:translate-x-1 group-hover:text-accent-blue"
        />
      </div>
    </Link>
  );
}

export default function HomePage() {
  return (
    <div className="mx-auto max-w-7xl space-y-6 pb-8">
      <section className="overflow-hidden rounded-3xl border border-bg-border bg-[radial-gradient(circle_at_top_left,rgba(0,212,163,0.14),transparent_30%),radial-gradient(circle_at_top_right,rgba(59,130,246,0.12),transparent_28%),linear-gradient(180deg,rgba(15,23,36,0.94),rgba(7,10,21,0.98))] px-6 py-8 md:px-8">
        <div className="max-w-4xl">
          <div className="flex items-center gap-2 text-[11px] uppercase tracking-[0.22em] text-text-muted">
            <Activity size={13} className="text-accent-green" />
            Nomad Curie Terminal
          </div>
          <h1 className="mt-3 max-w-3xl font-mono text-3xl font-semibold leading-tight text-text-primary md:text-4xl">
            Nomad Curie Terminal
          </h1>
        </div>
      </section>

      <section className="space-y-3">
        <h2 className="text-sm font-semibold uppercase tracking-[0.18em] text-text-secondary">
          Live Surfaces
        </h2>
        <div className="grid gap-4 lg:grid-cols-2">
          {LIVE_SURFACES.map((surface) => (
            <SurfaceCard key={surface.href} {...surface} />
          ))}
        </div>
      </section>

      <section className="grid gap-6 xl:grid-cols-[1.15fr_0.85fr]">
        <div className="space-y-3">
          <h2 className="text-sm font-semibold uppercase tracking-[0.18em] text-text-secondary">
            Secondary Tools
          </h2>
          <div className="grid gap-4 md:grid-cols-2">
            {SECONDARY_SURFACES.map((surface) => (
              <SurfaceCard key={surface.href} {...surface} />
            ))}
          </div>
        </div>

        <aside className="rounded-2xl border border-bg-border bg-bg-secondary/45 p-5">
          <h2 className="text-sm font-semibold uppercase tracking-[0.18em] text-text-secondary">
            Interaction Model
          </h2>
          <div className="mt-4 space-y-4 text-sm leading-6 text-text-secondary">
            <div>
              <div className="font-semibold text-text-primary">Research and validation</div>
              <div>Stay on <span className="font-mono text-accent-blue">/analysis</span> for cache growth, cooldown windows, and live strategy report updates.</div>
            </div>
            <div>
              <div className="font-semibold text-text-primary">Execution and supervision</div>
              <div>Use <span className="font-mono text-accent-blue">/trading</span> and <span className="font-mono text-accent-blue">/agent</span> when you need real-time positions, approvals, and reasoning.</div>
            </div>
            <div>
              <div className="font-semibold text-text-primary">Market context</div>
              <div>Use <span className="font-mono text-accent-blue">/market</span> for chain and macro context without paying for the research monitor’s heavier polling.</div>
            </div>
          </div>
        </aside>
      </section>
    </div>
  );
}

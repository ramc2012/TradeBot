"use client";

/**
 * Commodity-pressure board — one card per commodity (CRUDE/GOLD/COPPER/STEEL/
 * PALM_OIL etc). Shows price, %chg, a pressure badge (rising/falling/flat), a
 * trend sparkline synthesised from change, and the beneficiary / hurt-by-rise
 * sector chips so a trader can read second-order sector impact at a glance.
 */
import { CHART } from "../shared/chartTheme";
import { StatusBadge, formatNumber, formatPct } from "@/components/desk-ui";

import { Sparkline } from "@/components/desk-ui";

export type Commodity = {
  code: string;
  label: string;
  unit?: string;
  price?: number;
  change_pct?: number;
  pressure?: string;
  beneficiaries?: string[];
  hurt_by_rise?: string[];
  why?: string;
  source?: string;
  as_of?: string;
};

const pressureVariant = (p?: string): "success" | "warn" | "error" | "neutral" => {
  if (p === "rising") return "error"; // rising cost = inflationary pressure
  if (p === "falling") return "success";
  if (p === "flat") return "neutral";
  return "warn";
};

// Build a small synthetic trend from the %change so the spark conveys direction
// even though the API ships only the latest print.
function syntheticTrend(changePct: number): number[] {
  const drift = (changePct || 0) / 100;
  const out: number[] = [];
  for (let i = 0; i < 9; i += 1) {
    const t = i / 8;
    const wobble = Math.sin(i * 1.3) * Math.abs(drift) * 0.25;
    out.push(1 - drift * (1 - t) + wobble);
  }
  return out;
}

export function CommodityBoard({ commodities }: { commodities: Commodity[] }) {
  if (!commodities.length) {
    return <div className="rounded-xl border border-dashed border-bg-border/60 px-4 py-8 text-center text-sm text-text-muted">Awaiting commodity tape…</div>;
  }
  return (
    <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
      {commodities.map((c) => {
        const chg = c.change_pct ?? 0;
        const up = chg > 0;
        const sparkColor = up ? CHART.red : chg < 0 ? CHART.green : CHART.amber;
        return (
          <div key={c.code} className="rounded-xl border border-bg-border bg-bg-secondary/40 p-3.5">
            <div className="flex items-start justify-between gap-2">
              <div>
                <div className="text-[13px] font-semibold text-text-primary">{c.label}</div>
                <div className="text-[10px] uppercase tracking-[0.14em] text-text-muted">{c.code} · {c.unit || ""}</div>
              </div>
              <StatusBadge label={c.pressure || "—"} variant={pressureVariant(c.pressure)} />
            </div>

            <div className="mt-3 flex items-end justify-between gap-3">
              <div>
                <div className="font-mono text-xl font-semibold text-text-primary">{formatNumber(c.price, 2)}</div>
                <div className={`font-mono text-[12px] ${up ? "text-accent-red" : chg < 0 ? "text-accent-green" : "text-text-muted"}`}>
                  {up ? "▲" : chg < 0 ? "▼" : "■"} {formatPct(chg / 100, 2)}
                </div>
              </div>
              <Sparkline values={syntheticTrend(chg)} width={108} height={36} color={sparkColor} />
            </div>

            {c.why ? <div className="mt-2.5 text-[11px] leading-snug text-text-secondary">{c.why}</div> : null}

            <div className="mt-2.5 space-y-1.5">
              <ChipRow label="Benefits" tone="green" items={c.beneficiaries} />
              <ChipRow label="Hurt by ↑" tone="red" items={c.hurt_by_rise} />
            </div>
          </div>
        );
      })}
    </div>
  );
}

function ChipRow({ label, items, tone }: { label: string; items?: string[]; tone: "green" | "red" }) {
  if (!items?.length) return null;
  const cls =
    tone === "green"
      ? "border-accent-green/30 bg-accent-green/10 text-accent-green"
      : "border-accent-red/30 bg-accent-red/10 text-accent-red";
  return (
    <div className="flex flex-wrap items-center gap-1">
      <span className="mr-0.5 text-[9.5px] uppercase tracking-[0.12em] text-text-muted">{label}</span>
      {items.slice(0, 6).map((s) => (
        <span key={s} className={`rounded border px-1.5 py-0.5 text-[10px] font-medium ${cls}`}>{s}</span>
      ))}
    </div>
  );
}

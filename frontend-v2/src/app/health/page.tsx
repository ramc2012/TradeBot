"use client";

/**
 * /health — system service-health board. Native v2 surface (replaces the
 * v1 SystemHealthBoard + WS embed). Polls /api/system/health at the
 * snapshot cadence and shows core-service health (DB / redis / broker /
 * market-data / paper engines) plus the Upstox API budget.
 *
 * The /system hub embeds this as <HealthEmbed /> with no props, so the
 * default export must stay prop-free.
 */
import ServiceHealthBoard from "@/components/system/ServiceHealthBoard";

export default function HealthPage() {
  return (
    <div className="mx-auto max-w-[1680px] pb-10">
      <ServiceHealthBoard />
    </div>
  );
}

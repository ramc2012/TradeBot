/**
 * ONE time transform for every linked pane.
 *
 * lightweight-charts renders its axis in UTC, so `CandleChart` shifts plotted
 * timestamps by a fixed desk offset (+330 = IST) to show session hours. Any
 * pane that wants to share a crosshair with it must produce the SAME shifted
 * number for the same bar — otherwise the shared crosshair silently points at
 * a different bar on each pane, which looks like it works and is wrong.
 *
 * So the transform lives here, is imported by every pane, and is deliberately
 * a copy of nothing: `CandleChart` keeps its own private copy for its own
 * series, and this module is asserted against it by matching the bar times
 * before the flow pane is allowed to draw (see `alignFlowToPrice`).
 *
 * Pure module — no React, no imports.
 */

export const DESK_TZ_OFFSET_MINUTES = 330;

/** Seconds since epoch for a payload timestamp (number ms/s, or ISO string). */
export function toUnixSeconds(value: number | string): number {
  if (typeof value === "number") return value > 1e12 ? Math.floor(value / 1000) : value;
  const ms = Date.parse(value);
  return Math.floor((Number.isNaN(ms) ? Date.now() : ms) / 1000);
}

/** The plotted (tz-shifted) chart time for a payload timestamp. */
export function toChartTime(value: number | string, tzOffsetMinutes = DESK_TZ_OFFSET_MINUTES): number {
  return toUnixSeconds(value) + (tzOffsetMinutes || 0) * 60;
}

export type AlignmentVerdict = {
  aligned: boolean;
  /** Bars present on both panes. */
  overlap: number;
  /** Non-null when the panes may NOT share a crosshair. */
  reason: string | null;
};

/**
 * Decide whether a flow series may be drawn on the price pane's timeline.
 *
 * A shared crosshair across two series with different bar timestamps is a lie:
 * the pointer says 11:03 on one pane and lands on whatever the other pane
 * happens to have at that index. Rather than resample (which would invent
 * values), the flow pane refuses to draw and says why.
 */
export function alignFlowToPrice(
  priceTimes: number[],
  flowTimes: number[],
  minOverlapRatio = 0.6,
): AlignmentVerdict {
  if (!priceTimes.length) {
    return { aligned: false, overlap: 0, reason: "no price bars to align against" };
  }
  if (!flowTimes.length) {
    return { aligned: false, overlap: 0, reason: "no flow bars in this payload" };
  }
  const priceSet = new Set(priceTimes);
  let overlap = 0;
  for (const t of flowTimes) if (priceSet.has(t)) overlap += 1;
  const ratio = overlap / Math.min(priceTimes.length, flowTimes.length);
  if (ratio < minOverlapRatio) {
    return {
      aligned: false,
      overlap,
      reason: `flow bars do not align with price bars (${overlap} of ${flowTimes.length} timestamps match) — the two series are on different bar clocks, so a shared crosshair would point at different bars on each pane`,
    };
  }
  return { aligned: true, overlap, reason: null };
}

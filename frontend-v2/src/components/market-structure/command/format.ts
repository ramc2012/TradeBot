/**
 * Tiny formatting helpers for the matrix. `formatNumber` is re-exported from
 * desk-ui so the workspace can never drift from the desks' number rendering;
 * `formatAgeShortish` is the compact age used inside dense cells.
 */
export { formatNumber } from "@/components/desk-ui";

export function formatAgeShortish(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds)) return "—";
  const s = Math.max(0, Math.floor(seconds));
  if (s < 90) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 90) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h}h`;
  return `${Math.floor(h / 24)}d`;
}

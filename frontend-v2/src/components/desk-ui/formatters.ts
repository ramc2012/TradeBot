/**
 * Canonical formatters. Replaces the 8 hand-rolled copies of these
 * helpers scattered across the v1 workspaces.
 *
 * All formatters tolerate null / undefined / NaN and return a stable
 * placeholder ("—" by default) so tables don't break on partial data.
 */

const NA = "—";

export function formatMoney(value?: number | null, digits = 0): string {
  if (value == null || Number.isNaN(value)) return NA;
  return `₹${value.toLocaleString("en-IN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`;
}

export function formatSignedMoney(value?: number | null, digits = 0): string {
  if (value == null || Number.isNaN(value)) return NA;
  const prefix = value > 0 ? "+" : "";
  return `${prefix}₹${value.toLocaleString("en-IN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`;
}

export function formatNumber(value?: number | null, digits = 2): string {
  if (value == null || Number.isNaN(value)) return NA;
  return value.toFixed(digits);
}

export function formatSignedNumber(value?: number | null, digits = 2): string {
  if (value == null || Number.isNaN(value)) return NA;
  const prefix = value > 0 ? "+" : "";
  return `${prefix}${value.toFixed(digits)}`;
}

/**
 * Percent formatter. Treats `value` as a RATIO by default (0.05 → 5.00%).
 * Pass `asPercent: true` if your input is already a percent (5 → 5.00%).
 */
export function formatPct(
  value?: number | null,
  digits = 2,
  { asPercent = false }: { asPercent?: boolean } = {},
): string {
  if (value == null || Number.isNaN(value)) return NA;
  const pct = asPercent ? value : value * 100;
  return `${pct.toFixed(digits)}%`;
}

/**
 * IST timestamp formatter — DD MMM HH:mm.
 * Replaces ad-hoc `new Date(iso).toLocaleString("en-IN", …)` calls.
 */
export function formatIST(value?: string | number | Date | null): string {
  if (value == null) return NA;
  const parsed = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(parsed.getTime())) return String(value);
  return parsed.toLocaleString("en-IN", {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: "Asia/Kolkata",
  });
}

/** Full timestamp incl. year — for audit feeds, journal rows. */
export function formatTimestamp(value?: string | number | Date | null): string {
  if (value == null) return NA;
  const parsed = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(parsed.getTime())) return String(value);
  return parsed.toLocaleString("en-IN", {
    day: "2-digit",
    month: "short",
    year: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

/** Seconds → "Nh Mm Ss" or "Mm Ss" or "Ss". For data-age badges. */
export function formatDuration(seconds?: number | null): string {
  if (seconds == null || Number.isNaN(seconds) || seconds < 0) return NA;
  const s = Math.floor(seconds);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rem_s = s % 60;
  if (m < 60) return `${m}m ${rem_s}s`;
  const h = Math.floor(m / 60);
  const rem_m = m % 60;
  return `${h}h ${rem_m}m`;
}

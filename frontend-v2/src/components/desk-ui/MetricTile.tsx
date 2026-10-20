import { clsx } from "clsx";

/**
 * Standard metric tile. Replaces the 5 hand-rolled MetricTile / StatTile
 * components across v1 (each with subtly different padding, font sizes,
 * tone behaviour).
 */
export function MetricTile({
  label,
  value,
  detail,
  color,
  size = "md",
}: {
  label: string;
  value: string;
  detail?: string;
  color?: string;
  size?: "sm" | "md" | "lg";
}) {
  const padding = size === "sm" ? "px-3 py-2" : size === "lg" ? "px-5 py-4" : "px-4 py-3";
  const valueSize = size === "sm" ? "text-base" : size === "lg" ? "text-xl" : "text-lg";
  return (
    <div className={clsx("rounded-md border border-bg-border bg-bg-secondary/40", padding)}>
      <div className="text-[10px] uppercase tracking-[0.14em] text-text-muted">{label}</div>
      <div className={clsx("mt-0.5 font-mono font-semibold tabular-nums text-text-primary", valueSize, color)}>
        {value}
      </div>
      {detail ? <div className="mt-0.5 text-[11px] text-text-muted">{detail}</div> : null}
    </div>
  );
}

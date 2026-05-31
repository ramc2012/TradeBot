import { clsx } from "clsx";

/**
 * Standard section wrapper. The v1 frontend used a mix of
 * `rounded-2xl`, `rounded-[26px]`, `rounded-[28px]` with inconsistent
 * border / background. One Section here, used everywhere.
 */
export function Section({
  title,
  icon,
  description,
  rightSlot,
  padded = true,
  className,
  children,
}: {
  title?: string;
  icon?: React.ReactNode;
  description?: string;
  rightSlot?: React.ReactNode;
  padded?: boolean;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <section
      className={clsx(
        "rounded-2xl border border-bg-border bg-bg-secondary/24",
        padded && "p-5",
        className,
      )}
    >
      {title ? (
        <div className="mb-3 flex items-start justify-between gap-3">
          <div>
            <div className="flex items-center gap-2 text-sm font-semibold text-text-primary">
              {icon}
              {title}
            </div>
            {description ? (
              <div className="mt-1 text-xs text-text-muted">{description}</div>
            ) : null}
          </div>
          {rightSlot}
        </div>
      ) : null}
      {children}
    </section>
  );
}

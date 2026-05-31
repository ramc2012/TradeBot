"use client";

type SectionErrorFallbackProps = {
  title: string;
  detail: string;
  onRetry?: () => void;
  scopeLabel?: string;
  homeHref?: string;
  homeLabel?: string;
};

export default function SectionErrorFallback({
  title,
  detail,
  onRetry,
  scopeLabel = "Component Error",
  homeHref = "/",
  homeLabel = "Open Overview",
}: SectionErrorFallbackProps) {
  return (
    <section className="m-3 rounded-[24px] border border-accent-red/30 bg-accent-red/10 px-4 py-4">
      <div className="text-[10px] uppercase tracking-[0.18em] text-accent-red">{scopeLabel}</div>
      <div className="mt-2 text-sm font-semibold text-text-primary">{title}</div>
      <p className="mt-2 text-xs leading-5 text-text-secondary">{detail}</p>
      <div className="mt-4 flex flex-wrap gap-2">
        {onRetry ? (
          <button
            type="button"
            onClick={onRetry}
            className="rounded-full border border-accent-red/40 bg-accent-red/10 px-3 py-1.5 text-xs font-semibold text-accent-red transition hover:bg-accent-red/20"
          >
            Retry
          </button>
        ) : null}
        <a
          href={homeHref}
          className="rounded-full border border-bg-border bg-bg-secondary/40 px-3 py-1.5 text-xs font-semibold text-text-primary transition hover:bg-bg-secondary/60"
        >
          {homeLabel}
        </a>
      </div>
    </section>
  );
}

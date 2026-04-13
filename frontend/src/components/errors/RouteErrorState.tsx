"use client";

type RouteErrorStateProps = {
  title: string;
  detail: string;
  reset?: () => void;
  scopeLabel?: string;
  homeHref?: string;
  homeLabel?: string;
  retryLabel?: string;
};

export default function RouteErrorState({
  title,
  detail,
  reset,
  scopeLabel = "Route Error",
  homeHref = "/",
  homeLabel = "Back To Overview",
  retryLabel = "Retry Route",
}: RouteErrorStateProps) {
  return (
    <div className="mx-auto max-w-3xl space-y-4 py-10">
      <section className="rounded-[28px] border border-accent-red/30 bg-accent-red/10 px-5 py-5">
        <div className="text-[11px] uppercase tracking-[0.16em] text-accent-red">{scopeLabel}</div>
        <div className="mt-2 text-xl font-semibold text-text-primary">{title}</div>
        <p className="mt-2 text-sm leading-6 text-text-secondary">{detail}</p>
        <div className="mt-5 flex flex-wrap gap-3">
          {reset ? (
            <button
              type="button"
              onClick={() => reset()}
              className="rounded-full border border-accent-red/40 bg-accent-red/10 px-4 py-2 text-sm font-semibold text-accent-red transition hover:bg-accent-red/20"
            >
              {retryLabel}
            </button>
          ) : null}
          <a
            href={homeHref}
            className="rounded-full border border-bg-border bg-bg-secondary/40 px-4 py-2 text-sm font-semibold text-text-primary transition hover:bg-bg-secondary/60"
          >
            {homeLabel}
          </a>
        </div>
      </section>
    </div>
  );
}

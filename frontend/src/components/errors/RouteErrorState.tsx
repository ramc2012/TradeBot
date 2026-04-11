"use client";

type RouteErrorStateProps = {
  title: string;
  detail: string;
  reset: () => void;
};

export default function RouteErrorState({ title, detail, reset }: RouteErrorStateProps) {
  return (
    <div className="mx-auto max-w-3xl space-y-4 py-10">
      <section className="rounded-[28px] border border-accent-red/30 bg-accent-red/10 px-5 py-5">
        <div className="text-[11px] uppercase tracking-[0.16em] text-accent-red">Route Error</div>
        <div className="mt-2 text-xl font-semibold text-text-primary">{title}</div>
        <p className="mt-2 text-sm leading-6 text-text-secondary">{detail}</p>
        <div className="mt-5 flex flex-wrap gap-3">
          <button
            type="button"
            onClick={() => reset()}
            className="rounded-full border border-accent-red/40 bg-accent-red/10 px-4 py-2 text-sm font-semibold text-accent-red transition hover:bg-accent-red/20"
          >
            Retry Route
          </button>
          <a
            href="/"
            className="rounded-full border border-bg-border bg-bg-secondary/40 px-4 py-2 text-sm font-semibold text-text-primary transition hover:bg-bg-secondary/60"
          >
            Back To Overview
          </a>
        </div>
      </section>
    </div>
  );
}

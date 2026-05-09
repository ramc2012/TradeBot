"use client";

import RouteErrorState from "@/components/errors/RouteErrorState";

export default function GlobalError({
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <html lang="en">
      <body className="min-h-screen bg-bg-primary text-text-primary">
        <div className="mx-auto flex min-h-screen max-w-5xl items-center px-4 py-10">
          <RouteErrorState
            title="The application shell crashed."
            detail="A failure escaped the route-level handlers and interrupted the full workspace shell. Retry the shell or reopen the overview route."
            reset={reset}
            scopeLabel="Global Error"
            homeHref="/"
            homeLabel="Reopen Overview"
            retryLabel="Retry App Shell"
          />
        </div>
      </body>
    </html>
  );
}

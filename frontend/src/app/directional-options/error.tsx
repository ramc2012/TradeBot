"use client";

import RouteErrorState from "@/components/errors/RouteErrorState";

export default function DirectionalOptionsRouteError({
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <RouteErrorState
      title="The directional options desk crashed."
      detail="The long-premium module failed while building its workspace snapshot or backtest diagnostics. Retry the route without touching the existing strategy desks."
      reset={reset}
    />
  );
}

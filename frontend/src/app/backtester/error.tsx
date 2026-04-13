"use client";

import RouteErrorState from "@/components/errors/RouteErrorState";

export default function BacktesterRouteError({
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <RouteErrorState
      title="The backtester crashed."
      detail="The backtest workspace failed while rendering results or task state. Retry the route to recover without tearing down the shell."
      reset={reset}
    />
  );
}

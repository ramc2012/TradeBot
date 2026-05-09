"use client";

import RouteErrorState from "@/components/errors/RouteErrorState";

export default function AnalysisRouteError({
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <RouteErrorState
      title="The research monitor crashed."
      detail="The analysis route failed while rendering research-cache status, validation jobs, or backtest progress. Retry the route and resume from the same shell."
      reset={reset}
    />
  );
}

"use client";

import RouteErrorState from "@/components/errors/RouteErrorState";

export default function TradingRouteError({
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <RouteErrorState
      title="The trading workspace crashed."
      detail="The execution surface hit a rendering error. Retry this route without restarting the rest of the streamed workspace."
      reset={reset}
    />
  );
}

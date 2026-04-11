"use client";

import RouteErrorState from "@/components/errors/RouteErrorState";

export default function StrategyRouteError({
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <RouteErrorState
      title="The NSE strategy desk crashed."
      detail="The live strategy surface hit a rendering failure. Retry this route without reloading the rest of the workspace."
      reset={reset}
    />
  );
}

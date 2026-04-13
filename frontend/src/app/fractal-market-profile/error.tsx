"use client";

import RouteErrorState from "@/components/errors/RouteErrorState";

export default function FractalMarketProfileRouteError({
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <RouteErrorState
      title="The Fractal Market Profile desk crashed."
      detail="The dedicated FMP workspace failed while rendering the live profile, order-flow, replay, or ledger surfaces. Retry the route without disturbing the rest of the platform."
      reset={reset}
    />
  );
}

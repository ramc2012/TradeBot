"use client";

import RouteErrorState from "@/components/errors/RouteErrorState";

export default function MarketRouteError({
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <RouteErrorState
      title="The market console crashed."
      detail="The market route failed while rendering watchlists, option chains, or profile panels. Retry the route and keep the rest of the shell alive."
      reset={reset}
    />
  );
}

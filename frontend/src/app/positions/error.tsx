"use client";

import RouteErrorState from "@/components/errors/RouteErrorState";

export default function PositionsRouteError({
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <RouteErrorState
      title="The positions ledger crashed."
      detail="The combined positions route failed, but the rest of the workspace can stay live. Retry the ledger and reconnect this surface."
      reset={reset}
    />
  );
}

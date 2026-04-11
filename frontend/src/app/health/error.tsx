"use client";

import RouteErrorState from "@/components/errors/RouteErrorState";

export default function HealthRouteError({
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <RouteErrorState
      title="The health route crashed."
      detail="This runtime status surface failed independently. Retry just this route to recover the dashboard."
      reset={reset}
    />
  );
}

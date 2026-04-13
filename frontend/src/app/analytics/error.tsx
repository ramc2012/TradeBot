"use client";

import RouteErrorState from "@/components/errors/RouteErrorState";

export default function AnalyticsRouteError({
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <RouteErrorState
      title="The analytics dashboard crashed."
      detail="The performance and portfolio analytics route failed while rendering charts or summary metrics. Retry the route without reloading the rest of the app."
      reset={reset}
    />
  );
}

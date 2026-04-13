"use client";

import RouteErrorState from "@/components/errors/RouteErrorState";

export default function DataRouteError({
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <RouteErrorState
      title="The data workspace crashed."
      detail="The F&O data route failed while rendering task history, stats, or instrument catalogs. Retry the route without restarting the full app."
      reset={reset}
    />
  );
}

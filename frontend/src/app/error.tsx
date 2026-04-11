"use client";

import RouteErrorState from "@/components/errors/RouteErrorState";

export default function GlobalRouteError({
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <RouteErrorState
      title="The workspace view failed to render."
      detail="The shell stayed up, but this route crashed while rendering or hydrating. Retry the route or fall back to the overview surface."
      reset={reset}
    />
  );
}

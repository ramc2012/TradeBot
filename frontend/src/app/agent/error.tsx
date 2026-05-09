"use client";

import RouteErrorState from "@/components/errors/RouteErrorState";

export default function AgentRouteError({
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <RouteErrorState
      title="The strategy agent desk crashed."
      detail="The live agent monitor failed while rendering strategy state, commentary, or broker health. Retry the route without disturbing the rest of the shell."
      reset={reset}
    />
  );
}

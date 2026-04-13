"use client";

import RouteErrorState from "@/components/errors/RouteErrorState";

export default function SettingsRouteError({
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <RouteErrorState
      title="The settings workspace crashed."
      detail="The settings route failed while rendering broker connectivity, credentials, or notification controls. Retry the route without dropping the whole app."
      reset={reset}
    />
  );
}

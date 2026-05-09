"use client";

import RouteErrorState from "@/components/errors/RouteErrorState";

export default function SectorInteractionError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <RouteErrorState
      title="Sector interaction workspace failed."
      detail={error.message || "The sector network dashboard crashed while rendering."}
      reset={reset}
    />
  );
}

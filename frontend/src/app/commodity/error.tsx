"use client";

import RouteErrorState from "@/components/errors/RouteErrorState";

export default function CommodityRouteError({
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <RouteErrorState
      title="The commodity desk crashed."
      detail="This route failed while rendering the live commodity workspace. Retry the desk without tearing down the shared app shell."
      reset={reset}
    />
  );
}

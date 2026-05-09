"use client";

import RouteErrorState from "@/components/errors/RouteErrorState";

export default function AuctionIntelligenceRouteError({
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <RouteErrorState
      title="The Auction Intelligence console crashed."
      detail="The AI operator surface hit a rendering failure while building the market-profile or order-flow dashboard. Retry the route without disturbing the rest of the workspace."
      reset={reset}
    />
  );
}

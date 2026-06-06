"use client";

import { ErrorFallback } from "@/components/desk-ui/ErrorFallback";

export default function StrategyDeskError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return <ErrorFallback error={error} reset={reset} scope="desk" />;
}

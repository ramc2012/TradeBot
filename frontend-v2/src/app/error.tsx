"use client";

import { ErrorFallback } from "@/components/desk-ui/ErrorFallback";

export default function PageError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return <ErrorFallback error={error} reset={reset} scope="page" />;
}

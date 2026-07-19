import { Suspense } from "react";

import MarketStructureWorkspace from "@/components/market-structure/MarketStructureWorkspace";

export default function MarketStructurePage() {
  // The workspace reads its whole context from the URL, so it must render
  // inside a Suspense boundary (useSearchParams) rather than at build time.
  return (
    <Suspense fallback={<div className="p-6 text-sm text-text-muted">Loading workspace…</div>}>
      <MarketStructureWorkspace />
    </Suspense>
  );
}

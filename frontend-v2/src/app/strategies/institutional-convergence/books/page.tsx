import { Suspense } from "react";

import LaneBooksDesk from "@/components/books/LaneBooksDesk";

export default function ConvergenceBooksPage() {
  return (
    <Suspense fallback={null}>
      <LaneBooksDesk
        routeBase="/strategies/institutional-convergence/books"
        title="Convergence · books"
        description="Two separate books, never summed: backend/runtime/institutional_convergence/paper.json (NSE — enabled, running and never fired) and its MCX sibling commodity_paper.json."
      />
    </Suspense>
  );
}

import { Suspense } from "react";

import LaneBooksDesk from "@/components/books/LaneBooksDesk";

export default function DirectionalBooksPage() {
  return (
    <Suspense fallback={null}>
      <LaneBooksDesk
        routeBase="/strategies/directional/books"
        title="Long Premium · books"
        description="Order, trade, position and portfolio views read from directional_paper_positions — the authoritative Postgres book for the directional options lane."
      />
    </Suspense>
  );
}

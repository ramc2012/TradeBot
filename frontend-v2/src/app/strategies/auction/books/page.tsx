import { Suspense } from "react";

import LaneBooksDesk from "@/components/books/LaneBooksDesk";

export default function AuctionBooksPage() {
  return (
    <Suspense fallback={null}>
      <LaneBooksDesk
        routeBase="/strategies/auction/books"
        title="Auction IQ · books"
        description="Two separate books, never summed: the NSE index book in backend/runtime/auction_intelligence/paper_positions.json and the MCX commodity book in backend/runtime/auction_intelligence_commodity/commodity_paper.json."
      />
    </Suspense>
  );
}

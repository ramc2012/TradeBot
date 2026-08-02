import { redirect } from "next/navigation";

// The lifetime closed-trade ledger lives on /positions as the "Reports"
// tab (components/reports/ClosedTradeLedger). This route survives for old
// links.
export default function Page() {
  redirect("/positions?tab=reports");
}

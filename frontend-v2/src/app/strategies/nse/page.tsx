/**
 * /strategies/nse — redirect to the canonical merged NSE Strategy 1 desk.
 *
 * The classic multi-tab workspace and the dense live view were merged into
 * a single NSE-S1 tabbed desk on 2026-06-02 (NSE-S1 only; cross-desk
 * context lives on the dedicated commodity / directional / FMP pages).
 * Kept as a permanent redirect so existing links/bookmarks resolve.
 */
import { redirect } from "next/navigation";

export default function NseStrategyRedirectPage() {
  redirect("/strategies/nse/live");
}

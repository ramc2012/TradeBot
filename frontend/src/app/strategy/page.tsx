/**
 * /strategy — redirect to the canonical merged NSE Strategy 1 desk.
 *
 * The classic multi-tab /strategy workspace and the dense /strategy/live
 * view were merged into a single NSE-S1 tabbed desk on 2026-06-02 (user
 * decision: /strategy/live canonical, NSE-S1 only — cross-desk context
 * lives on the dedicated commodity / directional / FMP pages). This route
 * is kept as a permanent redirect so existing links/bookmarks resolve.
 */
import { redirect } from "next/navigation";

export default function StrategyRedirectPage() {
  redirect("/strategy/live");
}

/**
 * NSE Index desk — native v2 surface.
 *
 * Replaces the former v1 NseStrategyDesk embed with the native NseDesk
 * (DeskShell + shared strategy primitives). /strategies/nse permanently
 * redirects here, so both routes resolve to the same desk.
 */
import NseDesk from "@/components/strategies/nse/NseDesk";

export default function NseStrategyLivePage() {
  return <NseDesk />;
}

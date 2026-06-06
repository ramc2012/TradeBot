import { redirect } from "next/navigation";

// The native /strategies/fractal desk now carries the full live view;
// this legacy /live sub-route consolidates into it.
export default function Page() {
  redirect("/strategies/fractal");
}

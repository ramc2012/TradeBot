import { redirect } from "next/navigation";

// The native /strategies/auction desk now carries the full live view;
// this legacy /live sub-route consolidates into it.
export default function Page() {
  redirect("/strategies/auction");
}

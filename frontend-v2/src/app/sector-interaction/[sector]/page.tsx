import { redirect } from "next/navigation";

// The native Sector Interaction desk's Detail tab covers per-sector drill-down;
// this legacy deep-link consolidates into it.
export default function Page() {
  redirect("/sector-interaction");
}

import { redirect } from "next/navigation";

// The lane-invariants board lives on /system as the "Lane invariants" tab
// (components/system/LaneHealthBoard). This route survives for old links.
export default function Page() {
  redirect("/system?tab=lanes");
}

import { redirect } from "next/navigation";

// The service-health board lives on /system as the "Services" tab
// (components/system/ServiceHealthBoard). This route survives for old links.
export default function Page() {
  redirect("/system?tab=services");
}

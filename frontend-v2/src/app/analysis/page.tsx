import { redirect } from "next/navigation";

// The research monitor lives on /research as the "Validation" tab
// (components/research-monitor/ResearchMonitorBoard). This route survives
// for old links.
export default function Page() {
  redirect("/research?tab=validation");
}

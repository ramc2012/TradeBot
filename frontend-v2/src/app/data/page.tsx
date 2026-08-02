import { redirect } from "next/navigation";

// The F&O data-ingest console lives on /research as the "Data ingest" tab
// (components/data/DataIngestConsole). This route survives for old links.
export default function Page() {
  redirect("/research?tab=data");
}

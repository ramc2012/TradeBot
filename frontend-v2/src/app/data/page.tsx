"use client";

/**
 * /data — F&O data ingest console.
 *
 * Native v2 surface (replaces the v1 hand-rolled embed). Also rendered as the
 * "Data ingest" tab of /research, so this same default export is embedded by
 * the hub. The actual UI lives in DataIngestConsole.
 */
import DataIngestConsole from "@/components/data/DataIngestConsole";

export default function DataPage() {
  return <DataIngestConsole />;
}

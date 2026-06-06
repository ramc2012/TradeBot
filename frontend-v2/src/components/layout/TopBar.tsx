"use client";

/**
 * v2 top bar — a thin strip with v2 branding, environment indicator,
 * and a quick link back to v1. The big "ticker + broker status + mode
 * warning" stack from v1 will be ported in pieces; for now we keep the
 * shell lean so the v2 app is obviously identifiable.
 *
 * The v1 components (RealTimeTicker, BrokerStatusBar, LiveModeWarning)
 * remain available — when we want to mount them here, we can just
 * import from the v1 sources or copy them in. Doing so before the
 * underlying API surfaces are settled would just churn the v2 code.
 */
import Link from "next/link";
import { ExternalLink } from "lucide-react";

import { API_URL } from "@/lib/api";
import TopBarStatus from "./TopBarStatus";
import ThemeControl from "./ThemeControl";

export default function TopBar() {
  return (
    <div className="flex items-center justify-between gap-3 border-b border-bg-border bg-bg-secondary/30 px-3 py-1.5 text-[11px] text-text-secondary">
      <div className="flex items-center gap-2">
        <span className="v2-badge rounded px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-[0.18em]">
          v2 preview
        </span>
        <span className="hidden text-text-muted sm:inline">
          API · <span className="font-mono">{API_URL}</span>
        </span>
      </div>
      <div className="flex items-center gap-2.5">
        <TopBarStatus />
        <ThemeControl />
        <Link
          href="http://localhost:3000"
          target="_blank"
          rel="noreferrer"
          className="inline-flex items-center gap-1 rounded border border-bg-border bg-bg-primary/30 px-1.5 py-0.5 text-text-secondary hover:border-bg-active hover:text-text-primary"
        >
          v1
          <ExternalLink size={10} />
        </Link>
      </div>
    </div>
  );
}

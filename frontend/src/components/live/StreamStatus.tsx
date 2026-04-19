"use client";

import { clsx } from "clsx";
import { Wifi, WifiOff } from "lucide-react";

function formatSnapshotTime(value?: string | null) {
  if (!value) return "last successful snapshot";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString("en-IN", {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

type StreamStatusProps = {
  title?: string;
  isStreamConnected: boolean;
  isShowingSnapshot: boolean;
  snapshotSavedAt?: string | null;
  liveText?: string;
  snapshotText?: string;
  bootstrapText?: string;
  className?: string;
};

export function StreamStatus({
  title,
  isStreamConnected,
  isShowingSnapshot,
  snapshotSavedAt,
  liveText = "live websocket active",
  snapshotText,
  bootstrapText = "opening live stream",
  className,
}: StreamStatusProps) {
  const label = isStreamConnected ? "Streaming" : isShowingSnapshot ? "Snapshot" : "Bootstrap";
  const detail = isStreamConnected
    ? liveText
    : isShowingSnapshot
      ? (snapshotText || `saved ${formatSnapshotTime(snapshotSavedAt)}`)
      : bootstrapText;

  return (
    <div className={clsx("flex flex-wrap items-center gap-2", className)}>
      <span
        className={clsx(
          "inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-[11px] font-semibold uppercase tracking-[0.14em]",
          isStreamConnected
            ? "border-accent-green/30 bg-accent-green/10 text-accent-green"
            : isShowingSnapshot
              ? "border-accent-amber/30 bg-accent-amber/10 text-accent-amber"
              : "border-bg-active bg-bg-secondary/45 text-text-secondary",
        )}
      >
        {isStreamConnected ? <Wifi size={12} /> : <WifiOff size={12} />}
        {title ? `${title} · ${label}` : label}
      </span>
      <span className="text-xs text-text-muted">{detail}</span>
    </div>
  );
}

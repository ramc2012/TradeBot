"use client";
import { useState } from "react";
import { useStore } from "@/store";
import { AlertTriangle, X } from "lucide-react";

export default function LiveModeWarning() {
  const { mode } = useStore();
  const [dismissed, setDismissed] = useState(false);

  if (mode !== "live" || dismissed) return null;

  return (
    <div className="bg-accent-amber/10 border-b border-accent-amber/30 px-4 py-2 flex items-center gap-3 text-sm text-accent-amber shrink-0">
      <AlertTriangle size={16} className="shrink-0" />
      <span className="flex-1 font-medium">
        LIVE TRADING MODE ACTIVE — Real money orders will be executed. Ensure risk parameters are set correctly.
      </span>
      <button onClick={() => setDismissed(true)} className="hover:text-white">
        <X size={14} />
      </button>
    </div>
  );
}

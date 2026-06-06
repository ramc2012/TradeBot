"use client";

import { useEffect, useRef, useState } from "react";
import { Check, Palette } from "lucide-react";

import {
  DENSITIES,
  DENSITY_LABEL,
  THEME_LABEL,
  THEMES,
  useTheme,
  type Theme,
} from "@/components/ThemeProvider";

const SWATCH: Record<Theme, { bg: string; accent: string }> = {
  dark: { bg: "#080b18", accent: "#00d4a3" },
  midnight: { bg: "#02040a", accent: "#10e0b2" },
  light: { bg: "#f7f9fc", accent: "#00a884" },
};

export default function ThemeControl() {
  const { theme, density, setTheme, setDensity } = useTheme();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="inline-flex items-center gap-1 rounded border border-bg-border bg-bg-primary/30 px-1.5 py-0.5 text-text-secondary hover:border-bg-active hover:text-text-primary"
        title="Theme & density"
      >
        <Palette size={11} />
        <span className="hidden md:inline">{THEME_LABEL[theme]}</span>
      </button>

      {open ? (
        <div className="absolute right-0 z-50 mt-1.5 w-52 rounded-xl border border-bg-border bg-bg-card p-2.5 shadow-2xl">
          <div className="px-1 pb-1 text-[10px] uppercase tracking-[0.14em] text-text-muted">Theme</div>
          <div className="space-y-0.5">
            {THEMES.map((t) => (
              <button
                key={t}
                type="button"
                onClick={() => setTheme(t)}
                className="flex w-full items-center justify-between rounded-lg px-2 py-1.5 text-[12.5px] text-text-secondary hover:bg-bg-hover/50"
              >
                <span className="flex items-center gap-2">
                  <span className="h-3.5 w-3.5 rounded-full border border-white/10" style={{ background: SWATCH[t].bg }}>
                    <span className="block h-full w-full rounded-full" style={{ boxShadow: `inset 0 0 0 2px ${SWATCH[t].accent}` }} />
                  </span>
                  {THEME_LABEL[t]}
                </span>
                {theme === t ? <Check size={13} className="text-accent-green" /> : null}
              </button>
            ))}
          </div>

          <div className="mt-2 px-1 pb-1 text-[10px] uppercase tracking-[0.14em] text-text-muted">Density</div>
          <div className="flex gap-1">
            {DENSITIES.map((d) => (
              <button
                key={d}
                type="button"
                onClick={() => setDensity(d)}
                className={`flex-1 rounded-lg border px-1.5 py-1 text-[11px] transition-colors ${
                  density === d
                    ? "border-accent-blue/50 bg-accent-blue/10 text-text-primary"
                    : "border-bg-border text-text-muted hover:text-text-secondary"
                }`}
              >
                {DENSITY_LABEL[d]}
              </button>
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}

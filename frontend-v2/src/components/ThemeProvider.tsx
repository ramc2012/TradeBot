"use client";

/**
 * Theme + density controller. Themes swap the RGB-channel CSS variables on
 * <html data-theme>; density scales the root font-size (and therefore every
 * rem-based spacing/text token) via <html data-density> for information
 * density. Both persist to localStorage; a no-FOUC script in the root layout
 * applies them before first paint.
 */
import { createContext, useCallback, useContext, useEffect, useState } from "react";

export const THEMES = ["dark", "midnight", "light"] as const;
export type Theme = (typeof THEMES)[number];

export const DENSITIES = ["comfortable", "compact", "ultra"] as const;
export type Density = (typeof DENSITIES)[number];

export const THEME_LABEL: Record<Theme, string> = { dark: "Dark", midnight: "Midnight", light: "Light" };
export const DENSITY_LABEL: Record<Density, string> = { comfortable: "Comfortable", compact: "Compact", ultra: "Ultra" };

type Ctx = {
  theme: Theme;
  density: Density;
  setTheme: (t: Theme) => void;
  setDensity: (d: Density) => void;
};
const ThemeCtx = createContext<Ctx | null>(null);

export function useTheme(): Ctx {
  const c = useContext(ThemeCtx);
  if (!c) throw new Error("useTheme must be used within ThemeProvider");
  return c;
}

function apply(theme: Theme, density: Density) {
  if (typeof document === "undefined") return;
  const el = document.documentElement;
  el.dataset.theme = theme;
  if (density === "comfortable") delete el.dataset.density;
  else el.dataset.density = density;
}

export default function ThemeProvider({ children }: { children: React.ReactNode }) {
  const [theme, setThemeState] = useState<Theme>("dark");
  const [density, setDensityState] = useState<Density>("comfortable");

  useEffect(() => {
    const t = (localStorage.getItem("nomad-theme") as Theme) || "dark";
    const d = (localStorage.getItem("nomad-density") as Density) || "comfortable";
    setThemeState(THEMES.includes(t) ? t : "dark");
    setDensityState(DENSITIES.includes(d) ? d : "comfortable");
  }, []);

  const setTheme = useCallback(
    (t: Theme) => {
      setThemeState(t);
      try {
        localStorage.setItem("nomad-theme", t);
      } catch {
        /* ignore */
      }
      apply(t, density);
    },
    [density],
  );

  const setDensity = useCallback(
    (d: Density) => {
      setDensityState(d);
      try {
        localStorage.setItem("nomad-density", d);
      } catch {
        /* ignore */
      }
      apply(theme, d);
    },
    [theme],
  );

  return <ThemeCtx.Provider value={{ theme, density, setTheme, setDensity }}>{children}</ThemeCtx.Provider>;
}

/** Inline script string for the root layout <head> — applies persisted theme before paint (no flash). */
export const THEME_NO_FLASH_SCRIPT = `(function(){try{var t=localStorage.getItem('nomad-theme')||'dark';var d=localStorage.getItem('nomad-density')||'comfortable';var e=document.documentElement;e.dataset.theme=t;if(d!=='comfortable')e.dataset.density=d;}catch(_){}})();`;

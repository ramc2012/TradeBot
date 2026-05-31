import type { Config } from "tailwindcss";

// Identical theme tokens to the v1 frontend so any component lifted from
// v1 keeps its visual identity. When the desk-ui primitives are mature
// we can tune the scale here without affecting v1.
const config: Config = {
  content: [
    "./src/pages/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/components/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/app/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      colors: {
        bg: {
          primary: "#080b18",
          secondary: "#0d1117",
          tertiary: "#131929",
          card: "#0f1724",
          hover: "#1a2540",
          border: "#1e2d45",
          active: "#243454",
        },
        accent: {
          green: "#00d4a3",
          red: "#ff4757",
          amber: "#ffa502",
          blue: "#3b82f6",
          purple: "#8b5cf6",
          cyan: "#06b6d4",
        },
        text: {
          primary: "#e2e8f0",
          secondary: "#94a3b8",
          muted: "#4a5568",
        },
        paper: "#00d4a3",
        live: "#ffa502",
      },
      fontFamily: {
        mono: ["JetBrains Mono", "Fira Code", "monospace"],
        sans: ["Inter", "system-ui", "sans-serif"],
      },
    },
  },
  plugins: [],
};

export default config;

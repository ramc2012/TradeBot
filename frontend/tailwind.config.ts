import type { Config } from "tailwindcss";

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
      animation: {
        "pulse-green": "pulse-green 2s infinite",
        "slide-in": "slide-in 0.2s ease-out",
        ticker: "ticker 30s linear infinite",
      },
      keyframes: {
        "pulse-green": {
          "0%, 100%": { opacity: "1" },
          "50%": { opacity: "0.5" },
        },
        "slide-in": {
          from: { transform: "translateX(100%)", opacity: "0" },
          to: { transform: "translateX(0)", opacity: "1" },
        },
        ticker: {
          "0%": { transform: "translateX(100%)" },
          "100%": { transform: "translateX(-100%)" },
        },
      },
    },
  },
  plugins: [],
};

export default config;

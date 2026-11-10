import type { Config } from "tailwindcss";

// Colours are CSS variables defined in app/globals.css, so the palette lives in one place.
const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        page: "var(--page)",
        surface: "var(--surface)",
        raised: "var(--raised)",
        line: "var(--line)",
        ink: "var(--ink)",
        ink2: "var(--ink-2)",
        ink3: "var(--ink-3)",
        series: "var(--series-1)",
        good: "var(--good)",
        warning: "var(--warning)",
        serious: "var(--serious)",
        critical: "var(--critical)",
      },
      fontFamily: {
        sans: ["var(--font-sans)", "system-ui", "sans-serif"],
        mono: ["var(--font-mono)", "ui-monospace", "monospace"],
      },
    },
  },
  plugins: [],
};
export default config;

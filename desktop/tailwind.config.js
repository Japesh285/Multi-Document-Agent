/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      fontFamily: {
        sans: ["Inter", "Segoe UI", "system-ui", "sans-serif"],
        mono: ["JetBrains Mono", "Cascadia Code", "Consolas", "monospace"],
      },
      colors: {
        ink:    { 950: "#08090c", 900: "#0c0e13", 850: "#101218", 800: "#15181f",
                  750: "#1a1e26", 700: "#222632", 600: "#2c313e", 500: "#3a3f50" },
        chalk:  { 50: "#f5f7fa", 100: "#e6e9f0", 200: "#c7ccd9", 300: "#9ba2b3",
                  400: "#6e7588", 500: "#4a5063" },
        accent: { 400: "#7aa2ff", 500: "#5b86ff", 600: "#3b6cff", 700: "#2a55e8" },
        signal: { ok: "#4ade80", warn: "#fbbf24", err: "#f87171", info: "#60a5fa" },
      },
      boxShadow: {
        pane: "0 1px 0 rgba(255,255,255,0.04), 0 8px 24px -8px rgba(0,0,0,0.6)",
        chip: "inset 0 1px 0 rgba(255,255,255,0.05)",
      },
      animation: {
        "fade-in":   "fadeIn 0.18s ease-out",
        "slide-up":  "slideUp 0.22s ease-out",
        "pulse-dot": "pulseDot 1.6s ease-in-out infinite",
      },
      keyframes: {
        fadeIn:   { from: { opacity: 0 }, to: { opacity: 1 } },
        slideUp:  {
          from: { opacity: 0, transform: "translateY(4px)" },
          to:   { opacity: 1, transform: "translateY(0)" },
        },
        pulseDot: {
          "0%, 100%": { opacity: 0.4 },
          "50%":      { opacity: 1.0 },
        },
      },
    },
  },
  plugins: [],
};

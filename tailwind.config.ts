import type { Config } from 'tailwindcss'

// Theme note (v-theme): the whole app is built directly on Tailwind's
// `zinc` scale (bg-zinc-900, text-zinc-500, border-zinc-800, ...) rather
// than `dark:` variants - thousands of call sites across every page. To
// support both a dark and a light theme WITHOUT touching every one of
// those call sites, `zinc` itself is redefined here to resolve through
// CSS custom properties (--zinc-N, set in assets/css/main.css) instead of
// fixed hex values. Dark mode's variables equal the real stock Tailwind
// zinc palette (so dark mode is pixel-identical to before this change);
// `:root.light` in main.css swaps them for a light-appropriate scale.
// Because it's the shared `rgb(var(--zinc-N) / <alpha>)` pattern, opacity
// modifiers (bg-zinc-900/40, border-zinc-800/80, ...) keep working.
function zincVar(shade: number) {
  return ({ opacityValue }: { opacityValue?: string }) => {
    if (opacityValue === undefined) return `rgb(var(--zinc-${shade}))`
    return `rgb(var(--zinc-${shade}) / ${opacityValue})`
  }
}

export default <Partial<Config>>{
  darkMode: 'class',
  content: [],
  theme: {
    extend: {
      colors: {
        zinc: {
          50: zincVar(50),
          100: zincVar(100),
          200: zincVar(200),
          300: zincVar(300),
          400: zincVar(400),
          500: zincVar(500),
          600: zincVar(600),
          700: zincVar(700),
          800: zincVar(800),
          900: zincVar(900),
          950: zincVar(950),
        },
        py8n: {
          bg: '#0c0d10',
          panel: '#14161b',
          border: '#23262e',
          accent: '#f97316',
        },
      },
    },
  },
  plugins: [],
}

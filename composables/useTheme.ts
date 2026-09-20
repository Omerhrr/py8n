// App-shell color theme (dark/light) - shared by the layout, the toggle
// button and any page. Persists across reloads via localStorage, same
// pattern as useSidebar.ts. Dark is the default (matches how the app
// always looked before light mode existed); the <html class="light">
// toggle is what CSS variables in assets/css/main.css key off of.
const THEME_KEY = 'py8n.theme'
type Theme = 'dark' | 'light'

function applyThemeClass(theme: Theme) {
  if (!import.meta.client) return
  document.documentElement.classList.toggle('light', theme === 'light')
}

export function useTheme() {
  // useState factory runs once per app; ssr:false means this always runs in
  // the browser, so reading localStorage synchronously is safe. A tiny
  // inline script in nuxt.config (app.head.script) sets the same class
  // before Vue mounts, so there's no flash of the wrong theme either.
  const theme = useState<Theme>('py8n-theme', () => {
    if (!import.meta.client) return 'dark'
    try {
      const saved = localStorage.getItem(THEME_KEY)
      if (saved === 'light' || saved === 'dark') return saved
    } catch { /* storage unavailable - session default */ }
    return 'dark'
  })

  function setTheme(next: Theme) {
    theme.value = next
    applyThemeClass(next)
    if (import.meta.client) {
      try { localStorage.setItem(THEME_KEY, next) } catch { /* session-only */ }
    }
  }

  function toggle() {
    setTheme(theme.value === 'dark' ? 'light' : 'dark')
  }

  // keep the DOM class in sync even if theme.value changes from elsewhere
  // (e.g. another tab writing localStorage isn't watched here, but this
  // covers the common in-app case of two components sharing the state).
  if (import.meta.client) applyThemeClass(theme.value)

  return { theme, setTheme, toggle }
}

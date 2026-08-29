// App-shell sidebar state — shared by the layout, the sidebar and any page.
// The collapsed preference persists across reloads via localStorage.
const COLLAPSE_KEY = 'py8n.sidebar.collapsed'

export function useSidebar() {
  // useState factory runs once per app; ssr:false means this always runs in the
  // browser, so reading localStorage synchronously is safe (no hydration flash).
  const collapsed = useState<boolean>('py8n-sidebar-collapsed', () => {
    if (!import.meta.client) return false
    try {
      return localStorage.getItem(COLLAPSE_KEY) === '1'
    } catch {
      return false
    }
  })
  const mobileOpen = useState<boolean>('py8n-sidebar-mobile', () => false)

  function setCollapsed(next: boolean) {
    collapsed.value = next
    if (import.meta.client) {
      try {
        localStorage.setItem(COLLAPSE_KEY, next ? '1' : '0')
      } catch {
        /* storage unavailable — session-only */
      }
    }
  }

  function toggle() {
    setCollapsed(!collapsed.value)
  }

  function openMobile() {
    mobileOpen.value = true
  }

  function closeMobile() {
    mobileOpen.value = false
  }

  return { collapsed, mobileOpen, setCollapsed, toggle, openMobile, closeMobile }
}

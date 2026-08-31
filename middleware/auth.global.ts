// Global auth guard (v37, client-only - the app runs with ssr:false).
//
// Public surfaces (login, published app runners, standalone forms, public
// dashboards) never redirect. Everywhere else, an enforced backend
// (PY8N_REQUIRE_AUTH=true) demands a validated token; the default open mode
// lets everyone through exactly as before.
export default defineNuxtRouteMiddleware(async (to) => {
  if (import.meta.server) return

  const PUBLIC = ['/login', '/run/', '/d/', '/f/']
  if (PUBLIC.some(p => to.path === p || to.path.startsWith(p))) return

  const auth = useAuthStore()
  await auth.boot()

  if (!auth.requireAuth) return
  if (!auth.token) return navigateTo('/login')
  if (!auth.user) {
    const ok = await auth.fetchMe()
    if (!ok) return navigateTo('/login')
  }
})

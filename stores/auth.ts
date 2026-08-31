// Py8n auth store (v37) - session state for multi-user mode.
//
// The backend runs in one of two modes (probed via GET /auth/status):
//   open  (default): anonymous works everywhere; tokens are optional but
//         scope whatever they touch (resources get stamped with owner_id)
//   enforced (PY8N_REQUIRE_AUTH=true): build surfaces 401 without a token;
//         the UI redirects to /login
// The token persists in localStorage and rides along on every API call
// through composables/useApi.ts.
import { defineStore } from 'pinia'

const TOKEN_KEY = 'py8n.token'

interface Py8nUser {
  id: string
  email: string
  name: string
  role: string
  created_at: string | null
}

interface AuthStatus {
  require_auth: boolean
  has_users: boolean
  version: string
}

export const useAuthStore = defineStore('auth', () => {
  const token = ref('')
  const user = ref<Py8nUser | null>(null)
  const status = ref<AuthStatus | null>(null)
  const statusPromise = ref<Promise<void> | null>(null)
  const booted = ref(false)

  const requireAuth = computed(() => status.value?.require_auth === true)

  // Direct $fetch calls (the auth store must not import useApi - useApi
  // reads this store - so URLs are built with the same transport rules:
  // gateway mode proxies same-origin with XTransformPort, nitro mode hits
  // the local proxy route).
  function authUrl(path: string): string {
    const config = useRuntimeConfig()
    const apiPort = (config.public.apiPort as string) || '8000'
    const sep = path.includes('?') ? '&' : '?'
    return `/api/v1${path}${sep}XTransformPort=${apiPort}`
  }

  function restoreToken() {
    if (import.meta.client && !token.value) {
      token.value = localStorage.getItem(TOKEN_KEY) || ''
    }
  }

  function setSession(newToken: string, newUser: Py8nUser) {
    token.value = newToken
    user.value = newUser
    if (import.meta.client) localStorage.setItem(TOKEN_KEY, newToken)
  }

  function clearSession() {
    token.value = ''
    user.value = null
    if (import.meta.client) localStorage.removeItem(TOKEN_KEY)
  }

  // One-shot status probe shared across middleware / pages / api client.
  function ensureStatus(): Promise<void> {
    if (statusPromise.value) return statusPromise.value
    const p = (async () => {
      try {
        const res = await $fetch<AuthStatus>(authUrl('/auth/status'))
        status.value = res
      }
      catch {
        // backend down: keep the app usable in open mode
        status.value = { require_auth: false, has_users: true, version: 'unknown' }
      }
      finally {
        statusPromise.value = null
      }
    })()
    statusPromise.value = p
    return p
  }

  // Validate a restored token against the backend (401 clears it).
  async function fetchMe(): Promise<boolean> {
    if (!token.value) return false
    try {
      const res = await $fetch<Py8nUser>(authUrl('/auth/me'), {
        headers: { Authorization: `Bearer ${token.value}` },
      })
      user.value = res
      return true
    }
    catch {
      clearSession()
      return false
    }
  }

  async function login(email: string, password: string) {
    const res = await $fetch<{ token: string, user: Py8nUser }>(authUrl('/auth/login'), {
      method: 'POST',
      body: { email, password },
    })
    setSession(res.token, res.user)
  }

  async function register(email: string, password: string, name: string) {
    const res = await $fetch<{ token: string, user: Py8nUser }>(authUrl('/auth/register'), {
      method: 'POST',
      body: { email, password, name },
    })
    setSession(res.token, res.user)
    await ensureStatus() // has_users just flipped
  }

  async function logout() {
    clearSession()
  }

  // Client boot: restore token, probe mode, validate the token if present.
  async function boot() {
    if (booted.value || import.meta.server) return
    booted.value = true
    restoreToken()
    await ensureStatus()
    if (token.value) await fetchMe()
  }

  return {
    token, user, status, booted, requireAuth,
    restoreToken, setSession, clearSession, ensureStatus, fetchMe,
    login, register, logout, boot,
  }
})

// Central API client - transport-aware.
//
// gateway mode (sandbox + docker-compose caddy):
//   requests go to same-origin with ?XTransformPort=8000, Caddy proxies to the
//   FastAPI port. WebSocket uses the same query-param routing.
// nitro mode (plain local deploys without caddy):
//   a Nitro server route (/api-v1/...) proxies REST to the backend; WebSocket
//   connects directly to ws://<host>:8000.
export function useApi() {
  const config = useRuntimeConfig()
  const mode = (config.public.gatewayMode as string) || 'gateway'
  const apiPort = (config.public.apiPort as string) || '8000'

  const PREFIX = '/api/v1'

  function httpUrl(path: string): string {
    const sep = path.includes('?') ? '&' : '?'
    if (mode === 'gateway') return `${PREFIX}${path}${sep}XTransformPort=${apiPort}`
    return `${PREFIX}${path}`
  }

  async function request<T = any>(path: string, opts: any = {}): Promise<T> {
    // v37: ride the auth token on every call (no-op when logged out) and
    // bounce to /login when an enforced backend rejects a stale token.
    const auth = useAuthStore()
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
      ...((opts.headers as Record<string, string>) || {}),
    }
    if (auth.token) headers.Authorization = `Bearer ${auth.token}`
    return await $fetch<T>(httpUrl(path), {
      ...opts,
      headers,
      onResponseError({ response }) {
        if (response.status === 401 && auth.requireAuth) {
          auth.clearSession()
          if (import.meta.client && !location.pathname.startsWith('/login')) {
            navigateTo('/login')
          }
        }
      },
    })
  }

  const api = {
    get: <T = any>(path: string) => request<T>(path),
    post: <T = any>(path: string, body?: any) =>
      request<T>(path, { method: 'POST', body: body ?? {} }),
    put: <T = any>(path: string, body?: any) => request<T>(path, { method: 'PUT', body }),
    patch: <T = any>(path: string, body?: any) => request<T>(path, { method: 'PATCH', body }),
    del: <T = any>(path: string, query?: Record<string, string>) =>
      request<T>(path, { method: 'DELETE', query }),
    // multipart upload (dataset import, document extraction). Same auth as
    // every other call, but no Content-Type header - the browser must set
    // the multipart boundary itself.
    upload: <T = any>(path: string, form: FormData) => {
      const auth = useAuthStore()
      const headers: Record<string, string> = {}
      if (auth.token) headers.Authorization = `Bearer ${auth.token}`
      return $fetch<T>(httpUrl(path), {
        method: 'POST',
        body: form,
        headers,
        onResponseError({ response }) {
          if (response.status === 401 && auth.requireAuth) {
            auth.clearSession()
            if (import.meta.client && !location.pathname.startsWith('/login')) {
              navigateTo('/login')
            }
          }
        },
      })
    },
  }

  // v45: authenticated file download (dataset exports). Fetches the bytes
  // with the Bearer header (raw <a href> can't carry it) and hands them to
  // the browser as a named download.
  async function download(path: string, filename: string): Promise<void> {
    const auth = useAuthStore()
    const headers: Record<string, string> = {}
    if (auth.token) headers.Authorization = `Bearer ${auth.token}`
    const res = await fetch(httpUrl(path), { headers })
    if (!res.ok) {
      let detail = `HTTP ${res.status}`
      try {
        const body = await res.json()
        detail = body?.detail || detail
      } catch {}
      throw new Error(detail)
    }
    const blob = await res.blob()
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    document.body.appendChild(a)
    a.click()
    a.remove()
    URL.revokeObjectURL(url)
  }

  function wsUrl(executionId: string): string {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    const url = mode === 'gateway'
      ? `${proto}://${location.host}/ws/executions/${executionId}?XTransformPort=${apiPort}`
      : `${proto}://${location.hostname}:${apiPort}/ws/executions/${executionId}`
    // Browsers cannot set headers on WebSocket connects, so authenticated
    // mode rides the JWT as a query param; absent when logged out so the
    // legacy anonymous flow keeps working.
    const auth = useAuthStore()
    if (auth.token) return `${url}${url.includes('?') ? '&' : '?'}token=${encodeURIComponent(auth.token)}`
    return url
  }

  // URL for raw backend content (artifact images etc.) - gateway param honored.
  // Accepts paths with or without the /api/v1 prefix (artifact_url ships WITH it).
  function srcUrl(path: string): string {
    const p = path.startsWith(PREFIX) ? path.slice(PREFIX.length) : path
    if (mode === 'gateway') {
      return `${PREFIX}${p}${p.includes('?') ? '&' : '?'}XTransformPort=${apiPort}`
    }
    return `${PREFIX}${p}`
  }

  return { api, wsUrl, srcUrl, download, mode }
}

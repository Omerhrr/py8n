// Central API client — transport-aware.
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
    return await $fetch<T>(httpUrl(path), {
      ...opts,
      headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
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
  }

  function wsUrl(executionId: string): string {
    if (mode === 'gateway') {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws'
      return `${proto}://${location.host}/ws/executions/${executionId}?XTransformPort=${apiPort}`
    }
    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    return `${proto}://${location.hostname}:${apiPort}/ws/executions/${executionId}`
  }

  return { api, wsUrl, mode }
}

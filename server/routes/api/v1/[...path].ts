// Nitro proxy for the FastAPI backend (transport mode: "nitro").
// Used when Py8n runs without the Caddy gateway (plain local deploys /
// docker-compose without caddy). The sandbox and bundled compose use the
// gateway mode instead (XTransformPort query param handled by Caddy).
const API_ORIGIN = process.env.PY8N_API_ORIGIN || 'http://127.0.0.1:8000'

export default defineEventHandler(async (event) => {
  const path = event.path.replace(/^\/api\/v1/, '') || '/'
  return proxyRequest(event, `${API_ORIGIN}/api/v1${path}`, {
    headers: { 'x-py8n-proxy': 'nitro' },
  })
})

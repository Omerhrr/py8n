// py8n installable-apps service worker (v144).
//
// Registered site-wide (default scope "/") from pages/run/[slug].vue so any
// published app at /run/{slug} can be added to a home screen / desktop. It
// is deliberately generic - one worker serves every app, not one per build -
// so it never precaches specific asset filenames (Nuxt's hashed build
// output isn't known ahead of time here). Strategy:
//
//   - API calls (anything carrying ?XTransformPort=, this deployment's
//     gateway marker - see Caddyfile) and non-GET requests: always network,
//     NEVER cached. Records, auth, exports etc. must never be served stale.
//   - Everything else (the app shell, JS/CSS bundles, icons, the manifest):
//     stale-while-revalidate - serve the cached copy instantly if there is
//     one (so a re-open works offline / on a flaky connection), then update
//     the cache from the network in the background for next time.
//
// Bump CACHE_NAME to force every client to drop old entries on next visit.
const CACHE_NAME = 'py8n-run-v1'

self.addEventListener('install', (event) => {
  self.skipWaiting()
})

self.addEventListener('activate', (event) => {
  event.waitUntil(
    (async () => {
      const names = await caches.keys()
      await Promise.all(names.filter((n) => n !== CACHE_NAME).map((n) => caches.delete(n)))
      await self.clients.claim()
    })()
  )
})

function isApiCall(url) {
  return url.searchParams.has('XTransformPort') || url.pathname.startsWith('/api/')
}

self.addEventListener('fetch', (event) => {
  const req = event.request
  if (req.method !== 'GET') return // never intercept writes

  const url = new URL(req.url)
  if (url.origin !== self.location.origin) return // don't touch cross-origin (fonts CDN etc.)
  if (isApiCall(url)) return // let API/data traffic go straight to the network, uncached

  event.respondWith(
    (async () => {
      const cache = await caches.open(CACHE_NAME)
      const cached = await cache.match(req)
      const networkFetch = fetch(req)
        .then((res) => {
          if (res && res.ok) cache.put(req, res.clone())
          return res
        })
        .catch(() => cached) // offline: fall back to whatever's cached, if anything

      return cached || networkFetch
    })()
  )
})

// Py8n - Nuxt 3 frontend configuration
export default defineNuxtConfig({
  compatibilityDate: '2025-07-01',
  ssr: false, // editor SPA - no SEO needs, avoids canvas hydration issues
  app: {
    head: {
      title: 'Py8n',
      link: [{ rel: 'icon', type: 'image/svg+xml', href: '/logo.svg' }],
      script: [
        {
          // v-theme: set the light/dark class before Vue mounts so there's
          // no flash of the wrong theme on load - mirrors the logic in
          // composables/useTheme.ts, which takes over after hydration.
          innerHTML: `(function(){try{var t=localStorage.getItem('py8n.theme');if(t==='light'){document.documentElement.classList.add('light')}}catch(e){}})();`,
          type: 'text/javascript',
        },
      ],
    },
  },
  devtools: { enabled: false },
  modules: ['@nuxtjs/tailwindcss', '@pinia/nuxt'],
  css: [
    '@vue-flow/core/dist/style.css',
    '@vue-flow/core/dist/theme-default.css',
    '@vue-flow/controls/dist/style.css',
    '@vue-flow/minimap/dist/style.css',
  ],
  runtimeConfig: {
    public: {
      // gateway: route /api + /ws through the Caddy XTransformPort gateway
      // (sandbox + docker-compose caddy). Set NUXT_PUBLIC_GATEWAY_MODE=nitro
      // to proxy via the Nitro server route instead (plain local deploys).
      gatewayMode: process.env.NUXT_PUBLIC_GATEWAY_MODE || 'gateway',
      apiPort: '8000',
    },
  },
  tailwindcss: {
    cssPath: '~/assets/css/main.css',
    configPath: 'tailwind.config.ts',
  },
  vite: {
    server: {
      // The preview gateway forwards arbitrary external hostnames (DNS-rebinding
      // guard would block them). Sandbox/dev only - production builds are unaffected.
      allowedHosts: true,
      watch: {
        // Bug fix (found while getting the stack running): chokidar was
        // watching the WHOLE repo by default, including mini-services/
        // (the Python backend, its .venv - tens of thousands of files
        // from packages like moto/boto3 - and the bun llm-bridge
        // sidecar's own node_modules) and data/ (the sqlite db + files
        // the backend writes at runtime). None of that is frontend
        // source; watching it wastes fs watchers for no reason and can
        // exhaust the OS's inotify limit outright (ENOSPC) on a real
        // dev machine, killing the dev server on boot.
        ignored: ['**/mini-services/**', '**/data/**'],
      },
    },
  },
  nitro: {
    routeRules: {
      '/**': { headers: { 'Access-Control-Allow-Origin': '*' } },
    },
  },
})

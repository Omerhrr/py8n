// Py8n — Nuxt 3 frontend configuration
export default defineNuxtConfig({
  compatibilityDate: '2025-07-01',
  ssr: false, // editor SPA — no SEO needs, avoids canvas hydration issues
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
      // guard would block them). Sandbox/dev only — production builds are unaffected.
      allowedHosts: true,
    },
  },
  nitro: {
    routeRules: {
      '/**': { headers: { 'Access-Control-Allow-Origin': '*' } },
    },
  },
})

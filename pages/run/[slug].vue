<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import {
  Loader2, Search, Plus, Pencil, Trash2, X, CircleAlert, Rocket, Database, RefreshCw, ChevronLeft, ChevronRight, TriangleAlert, Lock,
  Layers, Menu, Download, Sun, Moon, ChevronsLeft, ChevronsRight,
} from 'lucide-vue-next'
import { useApi } from '~/composables/useApi'

// v145: chrome-less layout - the published app renders its OWN sidebar/tab
// nav (see pagesList below) when it has multiple pages; falling back to the
// platform's default layout stacked the py8n builder's own AppSidebar on
// top of that. Same fix as pages/f/[slug].vue already uses.
definePageMeta({ layout: 'plain' })

const { api, download, srcUrl } = useApi()
const route = useRoute()

interface FormField {
  name: string
  label?: string | null
  required?: boolean
  options?: (string | number | boolean)[] | null
  default?: string | number | boolean | null
  placeholder?: string | null
  multiple?: boolean // v136: options rendered as a checkbox group instead of a single-pick dropdown
  relation?: { dataset_id: string; display_column: string; value_column?: string | null } | null // v141: links to another dataset
}

interface AppComponent {
  id: string
  type: 'stat' | 'table' | 'form' | 'chart'
  label?: string
  title?: string
  agg?: string
  column?: string
  columns?: string[]
  page_size?: number
  fields?: (string | FormField)[]
  submit_label?: string
  chart_type?: string
  group_by?: string
  page?: string // v134: which sidebar/tab section this component belongs to
  // v138: ECharts-backed chart types beyond the original group_by/agg shape
  metrics?: string[] // radar
  row?: string
  col?: string // heatmap
  max?: number // gauge
  width?: 'full' | 'half' | 'third' | 'two_thirds' // v139: grid layout slot width
}

interface Runtime {
  app: { name: string; slug: string; description: string; config: { components?: AppComponent[]; theme?: { accent?: string; radius?: string; density?: string } } }
  dataset: { id: string; name: string; schema_json: { name: string; dtype: string }[]; row_count: number } | null
  stats: Record<string, number | null>
  chart: { labels: string[]; values: number[]; title: string; chart_type: string } | null
  // v46: every component rendered server-side + active filters
  components?: any[]
  filters?: Record<string, string[]>
  pages?: string[] // v134: ordered sidebar/tab section names
  relations?: Record<string, Record<string, string>> // v141: field/column -> {value: label}
}

const loading = ref(true)
const notFound = ref(false)
const forbidden = ref(false)
const loadError = ref<string | null>(null)
const rt = ref<Runtime | null>(null)

// v47 share tokens: when the owner enables share protection the public
// runtime endpoints demand ?t=<token> (or the X-Share-Token header) - the
// token this page received in its own URL rides on every runtime call.
const shareTok = computed(() => {
  const t = route.query.t
  const raw = Array.isArray(t) ? (t[0] ?? '') : t
  return raw ? String(raw) : ''
})

// v134/v149: which sidebar/tab section is active - declared early (before
// tq()/manifestHref/useHead below, which read it) because Nuxt's useHead
// evaluates its callback eagerly during setup, and a `const` referenced
// before its own declaration line throws (TDZ), not just "undefined".
const activeSection = ref('Dashboard')

// v149: multi-dataset apps - every records/export/CRUD call is scoped to the
// active page/tab so it hits THAT page's dataset (dataset_id_for_page on the
// backend falls back to the app's primary dataset for any page it doesn't
// recognize, so this is a no-op for every legacy single-dataset app).
function tq(sep: '?' | '&' = '?'): string {
  const parts: string[] = []
  if (shareTok.value) parts.push(`t=${encodeURIComponent(shareTok.value)}`)
  if (activeSection.value) parts.push(`page=${encodeURIComponent(activeSection.value)}`)
  return parts.length ? `${sep}${parts.join('&')}` : ''
}

// v144: installable PWA - a per-app manifest (name/icon/accent) plus the
// site-wide service worker (public/sw.js). The manifest link needs the same
// gateway (?XTransformPort=) treatment as any other API call, so it's built
// with srcUrl() rather than a plain path - see the manifest endpoint's own
// comment in api/apps.py for why the URL shape matters here.
const manifestHref = computed(() => {
  if (!route.params.slug) return ''
  return srcUrl(`/apps/${route.params.slug}/manifest.webmanifest${tq()}`)
})

useHead(() => ({
  link: manifestHref.value ? [{ rel: 'manifest', href: manifestHref.value }] : [],
  meta: rt.value?.app?.config?.theme?.accent
    ? [{ name: 'theme-color', content: rt.value.app.config.theme.accent }]
    : [],
}))

// beforeinstallprompt only fires when the browser's own install criteria are
// already met (manifest + service worker + served over http(s)) - capturing
// it just lets this page offer its OWN "Install app" button instead of
// relying on the browser's address-bar icon, which many people never notice.
const installPromptEvent = ref<any>(null)
const installedOrUnavailable = ref(false)
const installing = ref(false)

function registerServiceWorker() {
  if (!('serviceWorker' in navigator)) return
  navigator.serviceWorker.register('/sw.js').catch(() => {})
}

function watchInstallPrompt() {
  if (typeof window === 'undefined') return
  window.addEventListener('beforeinstallprompt', (e: any) => {
    e.preventDefault()
    installPromptEvent.value = e
  })
  window.addEventListener('appinstalled', () => {
    installPromptEvent.value = null
    installedOrUnavailable.value = true
  })
  if (window.matchMedia?.('(display-mode: standalone)').matches) installedOrUnavailable.value = true
}

async function installApp() {
  if (!installPromptEvent.value) return
  installing.value = true
  try {
    await installPromptEvent.value.prompt()
    await installPromptEvent.value.userChoice
  } finally {
    installPromptEvent.value = null
    installing.value = false
  }
}

const rows = ref<any[]>([])
const columns = ref<string[]>([])
const loadingRows = ref(false)
const search = ref('')
const page = ref(1)

const saving = ref(false)
const mutatingId = ref<string | null>(null)
const actionError = ref<string | null>(null)
const lastWarnings = ref<string[]>([])

// record modal
const showModal = ref(false)
const editIndex = ref<number | null>(null)
const formModel = ref<Record<string, any>>({})

const comps = computed<AppComponent[]>(() => rt.value?.app.config?.components || [])

// v134: multi-page apps - components carry an optional `page`; the runtime
// groups them into sidebar/tab sections. `pages` comes pre-ordered from the
// server (pages_of()); a single-page app (the common case, and every app
// saved before this existed) just gets one "Dashboard" section and no
// sidebar chrome is shown at all (see v-if="pagesList.length > 1" below).
const DEFAULT_PAGE = 'Dashboard'
const pagesList = computed<string[]>(() => (rt.value?.pages?.length ? rt.value.pages : [DEFAULT_PAGE]))

// v136: per-app accent color - a hex string picked in the builder, applied via
// the --accent CSS variable (see the root div below) so the whole runtime
// page - buttons, active nav, chart fills - reflects the app owner's choice
// without a build-time Tailwind safelist (arbitrary-value classes like
// `bg-[var(--accent)]` compile fine since the class string itself is static).
const DEFAULT_ACCENT = '#8b5cf6'
const accent = computed(() => rt.value?.app.config?.theme?.accent || DEFAULT_ACCENT)

// v136 fix: Tailwind's arbitrary-value opacity modifier (e.g. `bg-[var(--accent-80)]`)
// cannot split a CSS *variable* into an alpha channel - it silently produces
// `rgba(0,0,0,0)` (fully transparent) instead of the tinted color. So every
// translucent shade is pre-mixed into its own rgba() string here and exposed
// as its own --accent-NN variable (see the root div's :style below); the
// template then uses plain `bg-[var(--accent-15)]` etc. with NO opacity
// modifier, which Tailwind resolves correctly since the var already carries
// its own alpha.
function hexToRgba(hex: string, alpha: number): string {
  const h = hex.replace('#', '')
  const full = h.length === 3 ? h.split('').map((c) => c + c).join('') : h
  const n = parseInt(full, 16)
  if (Number.isNaN(n) || full.length !== 6) return `rgba(139, 92, 246, ${alpha})` // DEFAULT_ACCENT fallback
  const r = (n >> 16) & 255
  const g = (n >> 8) & 255
  const b = n & 255
  return `rgba(${r}, ${g}, ${b}, ${alpha})`
}
const accentVars = computed(() => ({
  '--accent': accent.value,
  '--accent-15': hexToRgba(accent.value, 0.15),
  '--accent-20': hexToRgba(accent.value, 0.2),
  '--accent-60': hexToRgba(accent.value, 0.6),
  '--accent-70': hexToRgba(accent.value, 0.7),
  '--accent-80': hexToRgba(accent.value, 0.8),
}))
// v140: card radius + spacing density - the third and fourth per-app design
// knobs alongside accent color and grid width.
const radius = computed(() => rt.value?.app.config?.theme?.radius || 'rounded')
const density = computed(() => rt.value?.app.config?.theme?.density || 'comfortable')
function surfaceClass(): string {
  const r = radius.value === 'sharp' ? 'rounded-md' : radius.value === 'soft' ? 'rounded-3xl' : 'rounded-2xl'
  const p = density.value === 'compact' ? 'p-3' : density.value === 'spacious' ? 'p-6' : 'p-4'
  return `${r} ${p}`
}
function radiusClass(): string {
  return radius.value === 'sharp' ? 'rounded-md' : radius.value === 'soft' ? 'rounded-3xl' : 'rounded-2xl'
}
function gapClass(): string {
  return density.value === 'compact' ? 'gap-3' : density.value === 'spacious' ? 'gap-6' : 'gap-4'
}

// v146: the published app gets its OWN light/dark toggle, independent of
// py8n's platform-wide theme (useTheme / <html class="light">, which stays
// whatever the builder happens to have set). The whole app is built
// directly on Tailwind's `zinc` scale resolving through CSS variables (see
// assets/css/main.css) rather than `dark:` variants, so re-pointing those
// SAME variables at <body> - below <html>, but still the real ancestor of
// both this page's own markup AND its Teleported modal/drawer, which mount
// under <body> too - reskins every zinc-* class on this page for free, with
// zero template class changes, and without touching the platform toggle
// any other tab/page shares. Applied/cleared imperatively (not scoped CSS)
// specifically so it reaches Teleported content and never leaks to other
// routes once this page unmounts.
type AppTheme = 'dark' | 'light'
const APP_THEME_VARS: Record<AppTheme, Record<string, string>> = {
  dark: {
    '--zinc-50': '250 250 250', '--zinc-100': '244 244 245', '--zinc-200': '228 228 231',
    '--zinc-300': '212 212 216', '--zinc-400': '161 161 170', '--zinc-500': '113 113 122',
    '--zinc-600': '82 82 91', '--zinc-700': '63 63 70', '--zinc-800': '39 39 42',
    '--zinc-900': '24 24 27', '--zinc-950': '9 9 11',
  },
  light: {
    '--zinc-50': '9 9 11', '--zinc-100': '24 24 27', '--zinc-200': '39 39 42',
    '--zinc-300': '63 63 70', '--zinc-400': '82 82 91', '--zinc-500': '113 113 122',
    '--zinc-600': '138 138 147', '--zinc-700': '161 161 170', '--zinc-800': '212 212 216',
    '--zinc-900': '240 240 242', '--zinc-950': '255 255 255',
  },
}
const appTheme = ref<AppTheme>('dark')
function appThemeStorageKey(): string {
  return `py8n.run-theme.${route.params.slug}`
}
function applyAppThemeVars(t: AppTheme) {
  if (typeof document === 'undefined') return
  for (const [k, v] of Object.entries(APP_THEME_VARS[t])) document.body.style.setProperty(k, v)
  document.body.style.colorScheme = t
}
function clearAppThemeVars() {
  if (typeof document === 'undefined') return
  for (const k of Object.keys(APP_THEME_VARS.dark)) document.body.style.removeProperty(k)
  document.body.style.removeProperty('color-scheme')
}
function toggleAppTheme() {
  appTheme.value = appTheme.value === 'dark' ? 'light' : 'dark'
  try { localStorage.setItem(appThemeStorageKey(), appTheme.value) } catch { /* session-only */ }
}
watch(appTheme, (t) => applyAppThemeVars(t))

// v146: collapsible desktop sidebar - a plain expand/collapse, persisted so
// it sticks across reloads (shared across apps; it's a viewer preference,
// not a per-app design setting like accent/radius/density above).
const SIDEBAR_COLLAPSE_KEY = 'py8n.run-sidebar-collapsed'
const desktopSidebarCollapsed = ref(false)
function toggleDesktopSidebar() {
  desktopSidebarCollapsed.value = !desktopSidebarCollapsed.value
  try { localStorage.setItem(SIDEBAR_COLLAPSE_KEY, desktopSidebarCollapsed.value ? '1' : '0') } catch { /* session-only */ }
}

const sidebarOpen = ref(false) // mobile toggle
function compSection(c: { page?: string }): string {
  return (c.page || '').trim() || DEFAULT_PAGE
}
function selectSection(p: string) {
  if (p === activeSection.value) { sidebarOpen.value = false; return }
  activeSection.value = p
  sidebarOpen.value = false
  page.value = 1
  loadRows()  // v149: multi-dataset apps - each page/tab has its own records
}

// v137: button component - link / in-app navigate / fire-a-webhook
function buttonStyleClass(style?: string): string {
  if (style === 'secondary') return 'border border-zinc-700 text-zinc-200 hover:bg-zinc-800'
  if (style === 'danger') return 'bg-red-500/90 text-white hover:bg-red-500'
  return 'bg-[var(--accent)] text-white hover:opacity-90' // primary (default)
}
const buttonState = ref<Record<string, 'idle' | 'loading' | 'success' | 'error'>>({})
async function fireButtonWebhook(c: any) {
  if (!c.webhook_url) return
  if (c.confirm_message && !confirm(c.confirm_message)) return
  buttonState.value[c.id] = 'loading'
  try {
    const res = await fetch(c.webhook_url, { method: c.method || 'POST', headers: { 'Content-Type': 'application/json' } })
    buttonState.value[c.id] = res.ok ? 'success' : 'error'
  } catch {
    buttonState.value[c.id] = 'error'
  } finally {
    setTimeout(() => { buttonState.value[c.id] = 'idle' }, 2500)
  }
}

const statsComps = computed(() => comps.value.filter((c) => c.type === 'stat' && compSection(c) === activeSection.value))
const chartComp = computed(() => comps.value.find((c) => c.type === 'chart' && compSection(c) === activeSection.value))
const tableComp = computed(() => comps.value.find((c) => c.type === 'table' && compSection(c) === activeSection.value))
const formComp = computed(() => comps.value.find((c) => c.type === 'form' && compSection(c) === activeSection.value))
const schema = computed(() => rt.value?.dataset?.schema_json || [])

const pageSize = computed(() => tableComp.value?.page_size || 10)

const filteredRows = computed(() => {
  const q = search.value.trim().toLowerCase()
  if (!q) return rows.value
  return rows.value.filter((r) =>
    columns.value.some((c) => String(r[c] ?? '').toLowerCase().includes(q)),
  )
})

const totalPages = computed(() => Math.max(1, Math.ceil(filteredRows.value.length / pageSize.value)))
const pagedRows = computed(() => {
  const start = (page.value - 1) * pageSize.value
  return filteredRows.value.slice(start, start + pageSize.value)
})

const tableColumns = computed(() => {
  const cfg = tableComp.value?.columns
  if (cfg?.length) return cfg
  return columns.value
})

const chartMax = computed(() => Math.max(1, ...(rt.value?.chart?.values || [1])))

// v46: server-rendered components + filter selections
// v134: scoped to the active sidebar/tab section - each rendered component
// already carries a `page` (server-stamped, defaults to DEFAULT_PAGE)
const allRenderedComps = computed<any[]>(() => rt.value?.components || [])
const renderedComps = computed<any[]>(() =>
  allRenderedComps.value.filter((c) => (c.page || DEFAULT_PAGE) === activeSection.value),
)
const filterComps = computed<any[]>(() => renderedComps.value.filter((c) => c.type === 'filter'))
const filterSel = ref<Record<string, string>>({})

function setFilter(col: string, value: string) {
  if (value) filterSel.value[col] = value
  else delete filterSel.value[col]
  loadRuntime()
}

// v47: one query string for the runtime call - share token plus the v46
// filter selections. (The old filterQuery() emitted "&filter.X=…" with no
// leading "?", which left the params inside the URL path and 404ed.)
function runtimeQuery(): string {
  const params = new URLSearchParams()
  if (shareTok.value) params.set('t', shareTok.value)
  for (const [col, val] of Object.entries(filterSel.value)) {
    if (val) params.set(`filter.${col}`, val)
  }
  const qs = params.toString()
  return qs ? `?${qs}` : ''
}

// v139: grid layout - full/half/third/two_thirds → a 12-col span (mobile always full)
function widthClass(c: any): string {
  const w = c.width || 'full'
  if (w === 'half') return 'col-span-12 md:col-span-6'
  if (w === 'third') return 'col-span-12 md:col-span-4'
  if (w === 'two_thirds') return 'col-span-12 md:col-span-8'
  return 'col-span-12'
}

// v138: full ECharts option builder - every chart_type renders through one
// <EChart> instance now, themed off the app's accent color.
function chartIsEmpty(c: any): boolean {
  if (c.chart_type === 'scatter') return !c.points?.length
  if (c.chart_type === 'gauge') return c.value === null || c.value === undefined
  if (c.chart_type === 'radar') return !c.metrics?.length
  if (c.chart_type === 'heatmap') return !c.cells?.length
  return !c.labels?.length
}

// v140: light/dark chart chrome. ECharts renders to canvas/SVG with literal
// colors, not CSS classes, so it can't inherit the zinc-* CSS-variable trick
// the rest of the page uses - this reads the platform theme toggle directly.
function chartTheme() {
  const isLight = appTheme.value === 'light'
  return {
    text: isLight ? '#52525b' : '#a1a1aa',    // axis / legend / label text
    dim: isLight ? '#a1a1aa' : '#71717a',      // dimmer text (gauge ticks)
    grid: isLight ? '#e4e4e7' : '#27272a',     // split lines
    axisLine: isLight ? '#d4d4d8' : '#3f3f46', // axis lines / gauge track
    border: isLight ? '#ffffff' : '#09090b',   // slice/cell separators = card bg
    strong: isLight ? '#18181b' : '#e4e4e7',   // high-contrast text (gauge value)
  }
}

function chartOption(c: any): any {
  const acc = accent.value
  const ct = chartTheme()
  const palette = [acc, '#0ea5e9', '#10b981', '#f59e0b', '#ef4444', '#14b8a6', '#a855f7', '#64748b']
  const axisText = { color: ct.text, fontSize: 11 }
  const common: any = {
    backgroundColor: 'transparent',
    textStyle: { color: ct.text, fontFamily: 'inherit' },
    color: palette,
  }
  const t = c.chart_type

  if (t === 'scatter') {
    const pts = (c.points || []).map((p: any) => [p.x, p.y])
    const numericX = typeof pts[0]?.[0] === 'number'
    return {
      ...common,
      grid: { left: 8, right: 16, top: 16, bottom: 8, containLabel: true },
      tooltip: { trigger: 'item' },
      xAxis: { type: numericX ? 'value' : 'category', axisLabel: axisText, splitLine: { show: false }, axisLine: { lineStyle: { color: ct.axisLine } } },
      yAxis: { type: 'value', axisLabel: axisText, splitLine: { lineStyle: { color: ct.grid } } },
      series: [{ type: 'scatter', data: pts, symbolSize: 8, itemStyle: { color: acc } }],
    }
  }
  if (t === 'gauge') {
    const val = c.value ?? 0
    const max = c.max ?? Math.max(10, Math.ceil(val * 1.5))
    return {
      ...common,
      series: [{
        type: 'gauge', min: 0, max, startAngle: 210, endAngle: -30,
        progress: { show: true, width: 14, itemStyle: { color: acc } },
        axisLine: { lineStyle: { width: 14, color: [[1, ct.grid]] } },
        pointer: { show: false }, axisTick: { show: false }, splitLine: { length: 8, lineStyle: { color: ct.axisLine } },
        axisLabel: { color: ct.dim, fontSize: 10, distance: 12 },
        anchor: { show: false },
        detail: { valueAnimation: true, color: ct.strong, fontSize: 24, fontWeight: 600, offsetCenter: [0, '20%'], formatter: (v: number) => (Number.isInteger(v) ? v : v.toFixed(2)) },
        data: [{ value: val }],
      }],
    }
  }
  if (t === 'radar') {
    const metrics = c.metrics || []
    const values = c.values || []
    const max = Math.max(1, ...values) * 1.2
    return {
      ...common,
      tooltip: {},
      radar: {
        indicator: metrics.map((m: string) => ({ name: m, max })),
        axisName: { color: ct.text, fontSize: 11 },
        splitLine: { lineStyle: { color: ct.grid } },
        axisLine: { lineStyle: { color: ct.axisLine } },
        splitArea: { show: false },
      },
      series: [{
        type: 'radar',
        data: [{ value: values, areaStyle: { color: acc, opacity: 0.25 }, lineStyle: { color: acc, width: 2 }, itemStyle: { color: acc } }],
      }],
    }
  }
  if (t === 'heatmap') {
    const rows = c.rows || []
    const cols = c.cols || []
    const cells = c.cells || []
    const max = Math.max(1, ...cells.map((x: any) => x[2]))
    return {
      ...common,
      tooltip: { position: 'top' },
      grid: { left: 90, right: 16, top: 16, bottom: 70, containLabel: true },
      xAxis: { type: 'category', data: cols, axisLabel: { ...axisText, rotate: cols.length > 5 ? 30 : 0 }, splitArea: { show: true } },
      yAxis: { type: 'category', data: rows, axisLabel: axisText, splitArea: { show: true } },
      visualMap: {
        min: 0, max, calculable: true, orient: 'horizontal', left: 'center', bottom: 0,
        textStyle: { color: ct.text }, inRange: { color: [ct.grid, acc] },
      },
      series: [{ type: 'heatmap', data: cells, label: { show: false }, itemStyle: { borderColor: ct.border, borderWidth: 1 } }],
    }
  }

  // bar / line / area / pie / donut / funnel / treemap share the labels/values shape
  const labels = c.labels || []
  const values = c.values || []
  if (t === 'pie' || t === 'donut') {
    return {
      ...common,
      tooltip: { trigger: 'item' },
      series: [{
        type: 'pie', radius: t === 'donut' ? ['45%', '72%'] : '72%', avoidLabelOverlap: true,
        label: { color: ct.text, fontSize: 11 },
        itemStyle: { borderColor: ct.border, borderWidth: 2 },
        data: labels.map((l: string, i: number) => ({ name: l, value: values[i] })),
      }],
    }
  }
  if (t === 'funnel') {
    return {
      ...common,
      tooltip: { trigger: 'item' },
      series: [{
        type: 'funnel', left: '8%', width: '84%', sort: 'descending',
        label: { color: ct.text, fontSize: 11 },
        itemStyle: { borderColor: ct.border, borderWidth: 1 },
        data: labels.map((l: string, i: number) => ({ name: l, value: values[i] })),
      }],
    }
  }
  if (t === 'treemap') {
    return {
      ...common,
      tooltip: {},
      series: [{
        type: 'treemap', roam: false, breadcrumb: { show: false },
        label: { color: ct.strong, fontSize: 11 },
        itemStyle: { borderColor: ct.border },
        data: labels.map((l: string, i: number) => ({ name: l, value: values[i] })),
      }],
    }
  }
  // bar / line / area
  return {
    ...common,
    grid: { left: 8, right: 16, top: 16, bottom: 8, containLabel: true },
    tooltip: { trigger: 'axis' },
    xAxis: {
      type: 'category', data: labels,
      axisLabel: { ...axisText, interval: 0, rotate: labels.length > 6 ? 30 : 0 },
      axisLine: { lineStyle: { color: ct.axisLine } },
    },
    yAxis: { type: 'value', axisLabel: axisText, splitLine: { lineStyle: { color: ct.grid } } },
    series: [{
      type: t === 'area' ? 'line' : 'bar',
      data: values,
      areaStyle: t === 'area' ? { color: acc, opacity: 0.25 } : undefined,
      itemStyle: { color: acc, borderRadius: t === 'bar' ? [4, 4, 0, 0] : 0 },
      lineStyle: t === 'area' || t === 'line' ? { color: acc, width: 2 } : undefined,
      smooth: t === 'line' || t === 'area',
      symbol: t === 'line' || t === 'area' ? 'circle' : 'none',
      symbolSize: 6,
      barWidth: t === 'bar' ? '55%' : undefined,
    }],
  }
}

// v46: per-chart pie/donut style (any chart component, not just the first)
function compPieStyle(c: any) {
  const labels = c.labels || []
  const values = c.values || []
  const total = values.reduce((a: number, b: number) => a + b, 0) || 1
  const palette = [accent.value, '#0ea5e9', '#10b981', '#f59e0b', '#ef4444', '#14b8a6', '#a855f7', '#64748b']
  let acc = 0
  const stops: string[] = []
  values.forEach((v: number, i: number) => {
    const from = (acc / total) * 360
    acc += v
    const to = (acc / total) * 360
    stops.push(`${palette[i % palette.length]} ${from}deg ${to}deg`)
  })
  return { background: `conic-gradient(${stops.join(', ')})`, total, palette, labels }
}

// conic-gradient pie style
const pieStyle = computed(() => {
  const labels = rt.value?.chart?.labels || []
  const values = rt.value?.chart?.values || []
  const total = values.reduce((a, b) => a + b, 0) || 1
  const palette = [accent.value, '#0ea5e9', '#10b981', '#f59e0b', '#ef4444', '#14b8a6', '#a855f7', '#64748b']
  let acc = 0
  const stops: string[] = []
  values.forEach((v, i) => {
    const from = (acc / total) * 360
    acc += v
    const to = (acc / total) * 360
    stops.push(`${palette[i % palette.length]} ${from}deg ${to}deg`)
  })
  return { background: `conic-gradient(${stops.join(', ')})`, total, palette, labels }
})

async function loadRuntime() {
  loading.value = true
  notFound.value = false
  forbidden.value = false
  loadError.value = null
  try {
    rt.value = await api.get<Runtime>(`/apps/${route.params.slug}/runtime${runtimeQuery()}`)
    // v134: keep the current section if it still exists (e.g. after a
    // filter refresh); otherwise fall back to the first available page
    if (!pagesList.value.includes(activeSection.value)) activeSection.value = pagesList.value[0]
    await loadRows()
  } catch (e: any) {
    if (e?.status === 404 || e?.statusCode === 404) notFound.value = true
    else if (e?.status === 403 || e?.statusCode === 403) forbidden.value = true
    else loadError.value = e?.data?.detail || e?.message || 'Failed to load app'
  } finally {
    loading.value = false
  }
}

async function loadRows() {
  if (!rt.value?.dataset) return
  loadingRows.value = true
  try {
    const r = await api.get<any>(`/apps/${route.params.slug}/records?offset=0&limit=1000${tq('&')}`)
    rows.value = r.rows || []
    columns.value = r.columns || []
    if (page.value > totalPages.value) page.value = 1
  } catch (e: any) {
    actionError.value = e?.data?.detail || e?.message || 'Failed to load records'
  } finally {
    loadingRows.value = false
  }
}

async function refreshAll() {
  await loadRuntime()
}

// v142: Export button - downloads exactly what this viewer can see (a
// grant-scoped share link only ever gets their slice - the backend enforces
// that, this just picks the format).
const exportOpen = ref(false)
const exporting = ref(false)
async function exportRecords(fmt: 'csv' | 'xlsx' | 'json') {
  exportOpen.value = false
  exporting.value = true
  actionError.value = null
  try {
    const ext = fmt
    await download(`/apps/${route.params.slug}/export?fmt=${fmt}${tq('&')}`, `${rt.value?.app.slug || 'records'}.${ext}`)
  } catch (e: any) {
    actionError.value = e?.message || 'Export failed'
  } finally {
    exporting.value = false
  }
}

onMounted(() => {
  try {
    const savedTheme = localStorage.getItem(appThemeStorageKey())
    if (savedTheme === 'light' || savedTheme === 'dark') appTheme.value = savedTheme
  } catch { /* default dark */ }
  applyAppThemeVars(appTheme.value)
  try {
    desktopSidebarCollapsed.value = localStorage.getItem(SIDEBAR_COLLAPSE_KEY) === '1'
  } catch { /* default expanded */ }
  loadRuntime()
  registerServiceWorker()
  watchInstallPrompt()
})

onUnmounted(() => {
  // this page's theme override must not bleed into whatever's navigated to
  // next - clearing it restores the platform-wide <html class="light"> toggle
  clearAppThemeVars()
})

// ---------------------------------------------------------------- form
function normField(f: string | FormField): FormField {
  return typeof f === 'string' ? { name: f } : f
}

function formFields(): FormField[] {
  return (formComp.value?.fields || []).map((f) => normField(f))
}

function openCreate() {
  if (!formComp.value) return
  const model: Record<string, any> = {}
  for (const f of formFields()) {
    if (f.multiple) {
      model[f.name] = f.default !== null && f.default !== undefined ? [String(f.default)] : []
    } else {
      model[f.name] = f.default !== null && f.default !== undefined ? String(f.default) : ''
    }
  }
  formModel.value = model
  editIndex.value = null
  actionError.value = null
  lastWarnings.value = []
  showModal.value = true
}

// v136: multi-select fields are stored as a "; "-joined string (see
// apply_form_options server-side); split it back into an array for the
// checkbox-group model.
function splitMulti(v: unknown): string[] {
  if (v === null || v === undefined || v === '') return []
  return String(v).split(';').map((s) => s.trim()).filter(Boolean)
}

function toggleMulti(fieldName: string, opt: string | number | boolean, checked: boolean) {
  const cur: string[] = Array.isArray(formModel.value[fieldName]) ? formModel.value[fieldName] : []
  const s = String(opt)
  formModel.value[fieldName] = checked ? [...cur.filter((v) => v !== s), s] : cur.filter((v) => v !== s)
}

function openEdit(index: number) {
  if (!formComp.value || !rows.value[index]) return
  const model: Record<string, any> = {}
  for (const f of formFields()) {
    const v = rows.value[index][f.name]
    model[f.name] = f.multiple ? splitMulti(v) : (v === null || v === undefined ? '' : String(v))
  }
  formModel.value = model
  editIndex.value = index
  actionError.value = null
  lastWarnings.value = []
  showModal.value = true
}

function dtypeOf(col: string) {
  return schema.value.find((c) => c.name === col)?.dtype || 'text'
}

// v141: dataset-linked fields - the runtime resolves labels once per load
// (rt.value.relations) so the create/edit dropdown and the table's cell
// display always agree, without either page re-fetching the linked dataset.
function relationOptions(name: string): { value: string; label: string }[] {
  const lk = rt.value?.relations?.[name]
  if (!lk) return []
  return Object.entries(lk)
    .map(([value, label]) => ({ value, label }))
    .sort((a, b) => a.label.localeCompare(b.label))
}

function cellText(row: any, col: string): string {
  const raw = row[col]
  const lk = rt.value?.relations?.[col]
  if (lk && raw !== null && raw !== undefined) {
    const label = lk[String(raw)]
    if (label !== undefined) return label
  }
  return raw === null || raw === undefined || raw === '' ? '-' : String(raw)
}

async function submitForm() {
  if (!formComp.value) return
  saving.value = true
  actionError.value = null
  lastWarnings.value = []
  try {
    let res: any
    if (editIndex.value === null) {
      res = await api.post<any>(`/apps/${route.params.slug}/records${tq()}`, { record: formModel.value })
    } else {
      res = await api.patch<any>(`/apps/${route.params.slug}/records/${editIndex.value}${tq()}`, { record: formModel.value })
    }
    lastWarnings.value = res?.warnings || []
    showModal.value = false
    await refreshAll()
  } catch (e: any) {
    actionError.value = e?.data?.detail || e?.message || 'Save failed'
    // keep the modal open so the user can fix the input
    if (!showModal.value) actionError.value = actionError.value
  } finally {
    saving.value = false
  }
}

// The backend addresses records by their index in the UNFILTERED dataset
// (parquet row order), so map a rendered row back to its real index - the
// old `(page - 1) * pageSize + ri` math indexed into filteredRows and made
// search + edit/delete hit the wrong record.
function rawIndex(row: any): number {
  return rows.value.indexOf(row)
}

async function removeRow(index: number) {
  if (!confirm('Delete this record?')) return
  mutatingId.value = `del-${index}`
  actionError.value = null
  try {
    await api.del(`/apps/${route.params.slug}/records/${index}${tq()}`)
    await refreshAll()
  } catch (e: any) {
    actionError.value = e?.data?.detail || e?.message || 'Delete failed'
  } finally {
    mutatingId.value = null
  }
}
</script>

<template>
  <div class="pb-16 text-zinc-100" :style="accentVars">
    <!-- loading -->
    <div v-if="loading" class="mt-24 flex justify-center text-zinc-500"><Loader2 class="h-6 w-6 animate-spin" /></div>

    <!-- not published / missing -->
    <div v-else-if="notFound" class="mt-24 text-center">
      <span class="mx-auto flex h-14 w-14 items-center justify-center rounded-2xl bg-zinc-900">
        <Rocket class="h-6 w-6 text-zinc-600" />
      </span>
      <p class="mt-4 text-sm font-medium text-zinc-300">App not found (or not published)</p>
      <p class="mt-1 text-xs text-zinc-500">Check the link, or ask the builder to publish it first.</p>
    </div>

    <!-- v47: share-protected app opened without (or with a stale) token -->
    <div v-else-if="forbidden" class="mt-24 text-center">
      <span class="mx-auto flex h-14 w-14 items-center justify-center rounded-2xl bg-zinc-900">
        <Lock class="h-6 w-6 text-zinc-600" />
      </span>
      <p class="mt-4 text-sm font-medium text-zinc-300">This link requires a valid share token</p>
      <p class="mt-1 text-xs text-zinc-500">Ask the app owner for a fresh link with ?t=… (regenerating revokes old ones).</p>
    </div>

    <p v-else-if="loadError" class="mx-auto mt-10 max-w-2xl rounded-xl border border-amber-500/30 bg-amber-500/10 px-4 py-2.5 text-sm text-amber-300">{{ loadError }}</p>

    <template v-else-if="rt">
      <!-- header -->
      <header class="sticky top-0 z-20 border-b border-zinc-800/80 bg-zinc-950/90 backdrop-blur">
        <div class="mx-auto flex max-w-6xl items-center gap-3 px-4 py-3.5 lg:px-6">
          <button
            class="rounded-lg border border-zinc-800 bg-zinc-900/60 p-1.5 text-zinc-400 transition hover:text-zinc-200 md:hidden"
            title="Menu"
            @click="sidebarOpen = true"
          >
            <Menu class="h-4 w-4" />
          </button>
          <span class="flex h-9 w-9 items-center justify-center rounded-xl bg-[var(--accent-15)]">
            <Rocket class="h-4 w-4 text-[var(--accent)]" />
          </span>
          <div class="min-w-0 flex-1">
            <h1 class="truncate text-base font-bold leading-tight">{{ rt.app.name }}</h1>
            <p class="truncate text-xs text-zinc-500">{{ rt.app.description || rt.app.slug }}</p>
          </div>
          <span v-if="rt.dataset" class="hidden items-center gap-1.5 rounded-full border border-zinc-800 bg-zinc-900/60 px-2.5 py-1 text-[11px] text-zinc-400 sm:flex">
            <Database class="h-3 w-3 text-sky-400" /> {{ rt.dataset.name }} · {{ rt.dataset.row_count }} records
          </span>
          <button
            v-if="installPromptEvent && !installedOrUnavailable"
            class="flex items-center gap-1.5 rounded-lg border border-zinc-800 bg-zinc-900/60 px-2.5 py-1.5 text-xs text-zinc-400 transition hover:text-zinc-200 disabled:opacity-50"
            :disabled="installing"
            title="Install this app"
            @click="installApp"
          >
            <Loader2 v-if="installing" class="h-3.5 w-3.5 animate-spin" />
            <Download v-else class="h-3.5 w-3.5" /> Install
          </button>
          <button class="rounded-lg border border-zinc-800 bg-zinc-900/60 p-1.5 text-zinc-400 transition hover:text-zinc-200" title="Refresh" @click="refreshAll">
            <RefreshCw class="h-3.5 w-3.5" :class="loadingRows && 'animate-spin'" />
          </button>
          <!-- v146: Export button hidden for now - PWA install covers the
               "take this app with you" need, and the xlsx export still has
               rough edges. Logic (exportRecords/exportOpen) and the backend
               endpoint are untouched, so this is a one-line flip (drop the
               "false &&") to bring it straight back when it's wanted. -->
          <div v-if="false && rt.dataset" class="relative">
            <button
              class="flex items-center gap-1.5 rounded-lg border border-zinc-800 bg-zinc-900/60 px-2.5 py-1.5 text-xs text-zinc-400 transition hover:text-zinc-200 disabled:opacity-50"
              :disabled="exporting"
              @click="exportOpen = !exportOpen"
            >
              <Loader2 v-if="exporting" class="h-3.5 w-3.5 animate-spin" />
              <Download v-else class="h-3.5 w-3.5" /> Export
            </button>
            <div v-if="exportOpen" class="absolute right-0 top-full z-30 mt-1 w-32 overflow-hidden rounded-lg border border-zinc-800 bg-zinc-900 py-1 shadow-xl" @click.self="exportOpen = false">
              <button class="block w-full px-3 py-1.5 text-left text-xs text-zinc-300 hover:bg-zinc-800" @click="exportRecords('csv')">CSV</button>
              <button class="block w-full px-3 py-1.5 text-left text-xs text-zinc-300 hover:bg-zinc-800" @click="exportRecords('xlsx')">Excel (.xlsx)</button>
              <button class="block w-full px-3 py-1.5 text-left text-xs text-zinc-300 hover:bg-zinc-800" @click="exportRecords('json')">JSON</button>
            </div>
          </div>
          <button
            v-if="formComp"
            class="flex items-center gap-1.5 rounded-lg bg-[var(--accent)] px-3 py-1.5 text-xs font-semibold text-white shadow-lg shadow-[var(--accent-20)] transition hover:opacity-90"
            @click="openCreate"
          >
            <Plus class="h-3.5 w-3.5" /> {{ formComp.submit_label || 'Create' }}
          </button>
        </div>
      </header>

      <div class="mx-auto flex max-w-6xl items-start gap-6 px-4 lg:px-6">
        <!-- v146: the app's own sidebar (desktop) - always present now, not
             just for multi-page apps, collapsible, and carries this app's
             own light/dark toggle (independent of py8n's platform theme -
             see applyAppThemeVars above). -->
        <aside
          class="sticky top-[4.5rem] hidden shrink-0 pt-5 transition-[width] duration-150 md:block"
          :class="desktopSidebarCollapsed ? 'w-12' : 'w-44'"
        >
          <div class="flex items-center" :class="desktopSidebarCollapsed ? 'justify-center' : 'justify-between px-2'">
            <p v-if="!desktopSidebarCollapsed" class="text-[10px] font-bold uppercase tracking-wide text-zinc-600">Sections</p>
            <button
              class="rounded-lg p-1.5 text-zinc-500 transition hover:bg-zinc-900 hover:text-zinc-200"
              :title="desktopSidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar'"
              @click="toggleDesktopSidebar"
            >
              <ChevronsRight v-if="desktopSidebarCollapsed" class="h-3.5 w-3.5" />
              <ChevronsLeft v-else class="h-3.5 w-3.5" />
            </button>
          </div>

          <nav v-if="pagesList.length > 1" class="mt-2 space-y-0.5">
            <button
              v-for="p in pagesList"
              :key="p"
              class="flex w-full items-center gap-2 rounded-lg py-1.5 text-left text-xs font-medium transition"
              :class="[
                activeSection === p ? 'bg-[var(--accent-15)] text-[var(--accent)]' : 'text-zinc-400 hover:bg-zinc-900 hover:text-zinc-200',
                desktopSidebarCollapsed ? 'justify-center px-0' : 'px-2.5',
              ]"
              :title="p"
              @click="selectSection(p)"
            >
              <Layers class="h-3.5 w-3.5 shrink-0" /> <span v-if="!desktopSidebarCollapsed" class="truncate">{{ p }}</span>
            </button>
          </nav>

          <div class="mt-4 border-t border-zinc-800/80 pt-3">
            <button
              class="flex w-full items-center gap-2 rounded-lg py-1.5 text-left text-xs font-medium text-zinc-400 transition hover:bg-zinc-900 hover:text-zinc-200"
              :class="desktopSidebarCollapsed ? 'justify-center px-0' : 'px-2.5'"
              :title="appTheme === 'dark' ? 'Switch this app to light mode' : 'Switch this app to dark mode'"
              @click="toggleAppTheme"
            >
              <Sun v-if="appTheme === 'dark'" class="h-3.5 w-3.5 shrink-0" />
              <Moon v-else class="h-3.5 w-3.5 shrink-0" />
              <span v-if="!desktopSidebarCollapsed">{{ appTheme === 'dark' ? 'Light mode' : 'Dark mode' }}</span>
            </button>
          </div>
        </aside>

        <div class="min-w-0 flex-1">
        <p v-if="actionError" class="mt-4 flex items-center gap-2 rounded-xl border border-amber-500/30 bg-amber-500/10 px-4 py-2 text-xs text-amber-300">
          <CircleAlert class="h-3.5 w-3.5 shrink-0" /> {{ actionError }}
        </p>
        <p v-if="lastWarnings.length" class="mt-4 flex items-center gap-2 rounded-xl border border-yellow-500/40 bg-yellow-500/10 px-4 py-2 text-xs text-yellow-300">
          <TriangleAlert class="h-3.5 w-3.5 shrink-0" />
          <span><b>Saved with warnings:</b> {{ lastWarnings.join(' · ') }}</span>
        </p>

        <!-- filter bar (v46) -->
        <div v-if="filterComps.length" class="mt-5 flex flex-wrap items-end gap-3 border border-zinc-800/80 bg-zinc-900/40" :class="surfaceClass()">
          <div v-for="fc in filterComps" :key="fc.id">
            <label class="block text-[10px] uppercase tracking-wide text-zinc-500">{{ fc.label }}</label>
            <select
              class="mt-1 rounded-lg border border-zinc-800 bg-zinc-950/60 px-2.5 py-1.5 text-xs outline-none focus:border-[var(--accent-60)]"
              :value="filterSel[fc.column] || ''"
              @change="setFilter(fc.column, ($event.target as HTMLSelectElement).value)"
            >
              <option value="">All</option>
              <option v-for="o in fc.options" :key="o" :value="o">{{ o }}</option>
            </select>
          </div>
          <span v-if="Object.keys(filterSel).length" class="ml-auto text-[10px] text-zinc-500">
            filters active · stats and charts below are filtered
          </span>
        </div>

        <!-- stats + kpis + markdown + charts: every component rendered server-side (v46) -->
        <!-- v139: 12-col grid - each component claims full/half/third/two-thirds width (see widthClass) -->
        <div class="mt-5 grid grid-cols-12" :class="gapClass()">
        <template v-for="c in renderedComps" :key="c.id">
          <!-- stat / kpi -->
          <div v-if="c.type === 'stat' || c.type === 'kpi'" class="border border-zinc-800/80 bg-zinc-900/40" :class="[surfaceClass(), widthClass(c)]">
            <p class="text-[11px] uppercase tracking-wide text-zinc-500">{{ c.label || c.id }}</p>
            <p class="mt-1 text-2xl font-bold">{{ c.value === null || c.value === undefined ? '-' : c.value }}</p>
          </div>

          <!-- markdown -->
          <div v-else-if="c.type === 'markdown'" class="border border-zinc-800/80 bg-zinc-900/40" :class="[surfaceClass(), widthClass(c)]">
            <p v-if="c.title" class="text-sm font-semibold text-zinc-200">{{ c.title }}</p>
            <!-- body is HTML-escaped server-side before markdown transforms -->
            <div class="mt-1 text-xs leading-relaxed text-zinc-400 [&_a]:text-sky-400 [&_a]:underline [&_code]:rounded [&_code]:bg-zinc-800 [&_code]:px-1 [&_strong]:text-zinc-200" v-html="c.html" />
          </div>

          <!-- button (v137): link / in-app navigate / fire a webhook workflow -->
          <div v-else-if="c.type === 'button'" class="flex items-center" :class="widthClass(c)">
            <a
              v-if="c.action === 'link'"
              :href="c.url"
              :target="c.new_tab ? '_blank' : '_self'"
              rel="noopener noreferrer"
              class="inline-flex items-center gap-1.5 rounded-lg px-4 py-2 text-sm font-semibold transition"
              :class="buttonStyleClass(c.style)"
            >
              {{ c.label }}
            </a>
            <button
              v-else-if="c.action === 'navigate'"
              class="inline-flex items-center gap-1.5 rounded-lg px-4 py-2 text-sm font-semibold transition"
              :class="buttonStyleClass(c.style)"
              @click="selectSection(c.target_page)"
            >
              {{ c.label }}
            </button>
            <button
              v-else-if="c.action === 'webhook'"
              class="inline-flex items-center gap-1.5 rounded-lg px-4 py-2 text-sm font-semibold transition disabled:opacity-60"
              :class="buttonStyleClass(c.style)"
              :disabled="buttonState[c.id] === 'loading'"
              @click="fireButtonWebhook(c)"
            >
              <Loader2 v-if="buttonState[c.id] === 'loading'" class="h-3.5 w-3.5 animate-spin" />
              {{ buttonState[c.id] === 'success' ? '✓ Done' : buttonState[c.id] === 'error' ? 'Failed - retry' : c.label }}
            </button>
          </div>

          <!-- chart (v138: every chart_type renders through ECharts) -->
          <div v-else-if="c.type === 'chart'" class="border border-zinc-800/80 bg-zinc-900/40" :class="[surfaceClass(), widthClass(c)]">
            <div class="flex items-center justify-between gap-2">
              <p class="text-sm font-semibold text-zinc-200">{{ c.title || 'Chart' }}</p>
              <span class="rounded-full bg-zinc-800 px-2 py-0.5 text-[10px] uppercase text-zinc-400">{{ c.chart_type }}</span>
            </div>
            <p v-if="chartIsEmpty(c)" class="mt-2 text-[11px] text-zinc-600">No data to chart yet.</p>
            <ClientOnly v-else>
              <EChart :option="chartOption(c)" :height="c.chart_type === 'gauge' ? 200 : 260" class="mt-2" />
            </ClientOnly>
          </div>
        </template>
        </div>

        <!-- table -->
        <div v-if="tableComp" class="mt-4 overflow-hidden border border-zinc-800/80 bg-zinc-900/40" :class="radiusClass()">
          <div class="flex items-center gap-3 border-b border-zinc-800/80 px-4 py-2.5">
            <p class="text-sm font-semibold text-zinc-200">{{ tableComp.title || 'Records' }}</p>
            <div class="relative ml-auto w-56">
              <Search class="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-zinc-500" />
              <input
                v-model="search"
                placeholder="Search records…"
                class="w-full rounded-lg border border-zinc-800 bg-zinc-950/60 py-1.5 pl-8 pr-2 text-xs outline-none focus:border-[var(--accent-60)]"
                @input="page = 1"
              />
            </div>
          </div>
          <div class="overflow-x-auto">
            <table class="w-full text-left text-xs">
              <thead>
                <tr class="border-b border-zinc-800/80 text-zinc-500">
                  <th v-for="col in tableColumns" :key="col" class="px-4 py-2 font-medium">{{ col }}</th>
                  <th v-if="formComp" class="px-4 py-2 text-right font-medium">Actions</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(row, ri) in pagedRows" :key="ri" class="border-b border-zinc-900/80 text-zinc-300 last:border-0 hover:bg-zinc-900/40">
                  <td v-for="col in tableColumns" :key="col" class="max-w-[240px] truncate px-4 py-2.5">
                    {{ cellText(row, col) }}
                  </td>
                  <td v-if="formComp" class="whitespace-nowrap px-4 py-2 text-right">
                    <button class="rounded-lg p-1.5 text-zinc-500 transition hover:bg-sky-500/10 hover:text-sky-400" title="Edit" @click="openEdit(rawIndex(row))">
                      <Pencil class="h-3.5 w-3.5" />
                    </button>
                    <button class="rounded-lg p-1.5 text-zinc-500 transition hover:bg-amber-500/10 hover:text-amber-400" title="Delete" @click="removeRow(rawIndex(row))">
                      <Loader2 v-if="mutatingId === `del-${rawIndex(row)}`" class="h-3.5 w-3.5 animate-spin" />
                      <Trash2 v-else class="h-3.5 w-3.5" />
                    </button>
                  </td>
                </tr>
                <tr v-if="!pagedRows.length">
                  <td :colspan="tableColumns.length + 1" class="px-4 py-8 text-center text-zinc-600">
                    {{ search ? 'No records match the search.' : 'No records yet - add the first one.' }}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <!-- pagination -->
          <div v-if="totalPages > 1 || filteredRows.length" class="flex items-center justify-between border-t border-zinc-800/80 px-4 py-2 text-[11px] text-zinc-500">
            <span>{{ filteredRows.length }} record{{ filteredRows.length === 1 ? '' : 's' }}<template v-if="search"> (filtered from {{ rows.length }})</template></span>
            <div v-if="totalPages > 1" class="flex items-center gap-1.5">
              <button class="rounded-lg border border-zinc-800 p-1 transition hover:text-zinc-200 disabled:opacity-30" :disabled="page <= 1" @click="page--">
                <ChevronLeft class="h-3 w-3" />
              </button>
              <span>page {{ page }} / {{ totalPages }}</span>
              <button class="rounded-lg border border-zinc-800 p-1 transition hover:text-zinc-200 disabled:opacity-30" :disabled="page >= totalPages" @click="page++">
                <ChevronRight class="h-3 w-3" />
              </button>
            </div>
          </div>
        </div>

        <p v-if="!statsComps.length && !chartComp && !tableComp" class="mt-10 text-center text-sm text-zinc-500">
          {{ pagesList.length > 1 ? 'Nothing on this section yet.' : 'This app has no components yet - ask the builder to add some.' }}
        </p>
        </div>
      </div>

      <!-- v134/v146: mobile sidebar drawer (sections + this app's own theme toggle) -->
      <Teleport to="body">
        <div v-if="sidebarOpen" class="fixed inset-0 z-50 md:hidden">
          <div class="absolute inset-0 bg-black/70" @click="sidebarOpen = false" />
          <nav class="relative z-10 flex h-full w-64 flex-col border-r border-zinc-800 bg-zinc-950 p-4">
            <div class="mb-2 flex items-center justify-between">
              <p class="text-[10px] font-bold uppercase tracking-wide text-zinc-600">{{ pagesList.length > 1 ? 'Sections' : rt.app.name }}</p>
              <button class="rounded-lg p-1 text-zinc-500 hover:text-zinc-200" @click="sidebarOpen = false"><X class="h-4 w-4" /></button>
            </div>
            <div v-if="pagesList.length > 1" class="space-y-0.5">
              <button
                v-for="p in pagesList"
                :key="p"
                class="flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-sm font-medium transition"
                :class="activeSection === p ? 'bg-[var(--accent-15)] text-[var(--accent)]' : 'text-zinc-400 hover:bg-zinc-900 hover:text-zinc-200'"
                @click="selectSection(p)"
              >
                <Layers class="h-3.5 w-3.5 shrink-0" /> <span class="truncate">{{ p }}</span>
              </button>
            </div>
            <div class="mt-auto border-t border-zinc-800/80 pt-3">
              <button
                class="flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-sm font-medium text-zinc-400 transition hover:bg-zinc-900 hover:text-zinc-200"
                @click="toggleAppTheme"
              >
                <Sun v-if="appTheme === 'dark'" class="h-3.5 w-3.5 shrink-0" />
                <Moon v-else class="h-3.5 w-3.5 shrink-0" />
                {{ appTheme === 'dark' ? 'Light mode' : 'Dark mode' }}
              </button>
            </div>
          </nav>
        </div>
      </Teleport>

      <!-- record modal -->
      <Teleport to="body">
        <div v-if="showModal" class="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4 backdrop-blur-sm" @click.self="showModal = false">
          <div class="w-full max-w-md rounded-2xl border border-zinc-800 bg-zinc-900 p-5 shadow-2xl">
            <div class="flex items-center justify-between">
              <h2 class="text-sm font-bold">{{ editIndex === null ? (formComp?.title || 'Add record') : `Edit record #${editIndex + 1}` }}</h2>
              <button class="rounded-lg p-1 text-zinc-500 hover:text-zinc-200" @click="showModal = false"><X class="h-4 w-4" /></button>
            </div>
            <p v-if="actionError" class="mt-3 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-300">{{ actionError }}</p>
            <div class="mt-3 space-y-2.5">
              <div v-for="f in formFields()" :key="f.name">
                <label class="text-[10px] uppercase tracking-wide text-zinc-500">
                  {{ f.label || f.name }}<span v-if="f.required" class="text-red-400"> *</span>
                </label>
                <!-- v141: linked to another dataset → searchable-by-eye dropdown of its rows -->
                <select
                  v-if="f.relation"
                  v-model="formModel[f.name]"
                  class="mt-1 w-full rounded-xl border border-zinc-800 bg-zinc-950/60 px-3 py-2 text-sm outline-none focus:border-[var(--accent-60)]"
                >
                  <option value="">choose…</option>
                  <option v-for="o in relationOptions(f.name)" :key="o.value" :value="o.value">{{ o.label }}</option>
                </select>
                <!-- options + multiple → checkbox group (v136) -->
                <div v-else-if="f.multiple && f.options && f.options.length" class="mt-1 flex flex-wrap gap-1.5">
                  <label
                    v-for="o in f.options"
                    :key="String(o)"
                    class="flex cursor-pointer items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs transition"
                    :class="(formModel[f.name] || []).includes(String(o)) ? 'border-[var(--accent-60)] bg-[var(--accent-15)] text-[var(--accent)]' : 'border-zinc-800 text-zinc-400 hover:border-zinc-600'"
                  >
                    <input
                      type="checkbox"
                      class="sr-only"
                      :checked="(formModel[f.name] || []).includes(String(o))"
                      @change="toggleMulti(f.name, o, ($event.target as HTMLInputElement).checked)"
                    />
                    {{ o }}
                  </label>
                </div>
                <!-- options → dropdown (v30) -->
                <select
                  v-else-if="f.options && f.options.length"
                  v-model="formModel[f.name]"
                  class="mt-1 w-full rounded-xl border border-zinc-800 bg-zinc-950/60 px-3 py-2 text-sm outline-none focus:border-[var(--accent-60)]"
                >
                  <option value="" disabled>choose…</option>
                  <option v-for="o in f.options" :key="String(o)" :value="String(o)">{{ o }}</option>
                </select>
                <select
                  v-else-if="dtypeOf(f.name) === 'boolean'"
                  v-model="formModel[f.name]"
                  class="mt-1 w-full rounded-xl border border-zinc-800 bg-zinc-950/60 px-3 py-2 text-sm outline-none focus:border-[var(--accent-60)]"
                >
                  <option value="">-</option>
                  <option value="true">true</option>
                  <option value="false">false</option>
                </select>
                <input
                  v-else
                  v-model="formModel[f.name]"
                  :type="dtypeOf(f.name) === 'integer' || dtypeOf(f.name) === 'number' ? 'number' : 'text'"
                  :step="dtypeOf(f.name) === 'number' ? 'any' : undefined"
                  :placeholder="f.placeholder || ''"
                  class="mt-1 w-full rounded-xl border border-zinc-800 bg-zinc-950/60 px-3 py-2 text-sm outline-none focus:border-[var(--accent-60)]"
                />
              </div>
            </div>
            <button
              class="mt-4 flex w-full items-center justify-center gap-1.5 rounded-xl bg-[var(--accent)] py-2 text-sm font-semibold text-white transition hover:opacity-90 disabled:opacity-40"
              :disabled="saving"
              @click="submitForm"
            >
              <Loader2 v-if="saving" class="h-4 w-4 animate-spin" />
              {{ editIndex === null ? (formComp?.submit_label || 'Create') : 'Save changes' }}
            </button>
          </div>
        </div>
      </Teleport>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue'
import {
  Loader2, Save, Rocket, ExternalLink, Database, Plus, Trash2, X, RefreshCw,
  Gauge, Table2, ClipboardList, BarChart3, ArrowLeft, Unlink, CircleAlert,
  ShieldCheck, Link2, TriangleAlert, PlusCircle, XCircle, TrendingUp, Filter,
  Share2, Copy, Check, KeyRound, Power, History, ChevronDown, FileText,
  GripVertical, ChevronUp, Layers, MousePointerClick, LayoutGrid, Download,
} from 'lucide-vue-next'
import { useApi } from '~/composables/useApi'
import { useTheme } from '~/composables/useTheme'

const { api, download } = useApi()
const route = useRoute()

interface FormField {
  name: string
  label?: string | null
  required?: boolean
  options?: (string | number | boolean)[] | null
  default?: string | number | boolean | null
  placeholder?: string | null
  multiple?: boolean // v136: options rendered as a checkbox group on /run/[slug]
  relation?: { dataset_id: string; display_column: string; value_column?: string | null } | null // v141: links to another dataset
}

interface RuleClause { field: string; op: string; value?: any }

interface AppRule {
  id?: string
  name?: string
  event?: string
  when?: { all: RuleClause[] }
  action: string
  message?: string
  field?: string
  value?: any
  formula?: string
}

interface AppComponent {
  id: string
  type: 'stat' | 'table' | 'form' | 'chart' | 'kpi' | 'markdown' | 'filter' | 'button'
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
  x?: string
  y?: string
  body?: string
  multiple?: boolean
  page?: string // v134: which sidebar/tab section this component belongs to
  // v137: button component
  style?: 'primary' | 'secondary' | 'danger'
  action?: 'link' | 'navigate' | 'webhook'
  url?: string
  new_tab?: boolean
  target_page?: string
  webhook_url?: string
  method?: 'GET' | 'POST'
  confirm_message?: string
  // v138: ECharts-backed chart types beyond the original group_by/agg shape
  metrics?: string[] // radar
  row?: string
  col?: string // heatmap
  max?: number // gauge
}

interface AppDetail {
  id: string
  name: string
  slug: string
  description: string
  dataset_id: string | null
  dataset_name: string | null
  config: { components?: AppComponent[]; rules?: AppRule[]; theme?: { accent?: string; radius?: string; density?: string } }
  status: string
  share_token: string | null  // v47: owner-facing share ACL
}

interface DatasetMeta {
  id: string
  name: string
  row_count: number
  schema_json: { name: string; dtype: string }[]
}

const loading = ref(true)
const saving = ref(false)
const publishing = ref(false)
const error = ref<string | null>(null)
const notice = ref<string | null>(null)

const appRow = ref<AppDetail | null>(null)
const datasets = ref<DatasetMeta[]>([])
const bindId = ref('')
const rows = ref<any[]>([])
const schema = ref<{ name: string; dtype: string }[]>([])

const comps = computed<AppComponent[]>(() => appRow.value?.config?.components || [])

// v134: multi-page apps - "Dashboard" is always the implicit first page
// (components with no `page` set, or `page` blank, land there), followed by
// every other page name in first-seen order. Mirrors pages_of() server-side.
const DEFAULT_PAGE = 'Dashboard'
const appPages = computed<string[]>(() => {
  const seen = [DEFAULT_PAGE]
  for (const c of comps.value) {
    const p = (c.page || '').trim() || DEFAULT_PAGE
    if (!seen.includes(p)) seen.push(p)
  }
  return seen
})

const editingName = ref('')
const editingDesc = ref('')

const isPublished = computed(() => appRow.value?.status === 'published')
const dirty = ref(false)

function touch() {
  dirty.value = true
  schedulePreview()  // v46: debounced server preview refresh
}

// ---------------------------------------------------------------- v46 server preview
const previewComps = ref<any[]>([])
const previewLoading = ref(false)
const previewError = ref<string | null>(null)
let previewTimer: ReturnType<typeof setTimeout> | null = null

function schedulePreview() {
  if (previewTimer) clearTimeout(previewTimer)
  previewTimer = setTimeout(loadPreview, 700)
}

async function loadPreview() {
  if (!appRow.value || !bindId.value || isPublished.value) return
  previewLoading.value = true
  previewError.value = null
  try {
    const body = await api.post<{ components: any[] }>(`/apps/${appRow.value.id}/preview`, { components: comps.value })
    previewComps.value = body.components || []
  } catch (e: any) {
    previewError.value = e?.data?.detail || e?.message || 'Preview failed'
    previewComps.value = []
  } finally {
    previewLoading.value = false
  }
}

async function load() {
  loading.value = true
  try {
    const [a, ds] = await Promise.all([
      api.get<AppDetail>(`/apps/${route.params.id}`),
      api.get<DatasetMeta[]>('/datasets'),
    ])
    appRow.value = a
    datasets.value = ds
    editingName.value = a.name
    editingDesc.value = a.description || ''
    bindId.value = a.dataset_id || ''
    rules.value = (a.config?.rules || []).map((r) => JSON.parse(JSON.stringify(r)))
    rulesDirty.value = false
    if (a.dataset_id) await loadBound(a.dataset_id)
    loadPreview()  // v46: initial server preview
    loadSheetsSync()  // v143
  } catch (e: any) {
    error.value = e?.data?.detail || e?.message || 'Failed to load app'
  } finally {
    loading.value = false
  }
}

async function loadBound(dsId: string) {
  if (!dsId) { rows.value = []; schema.value = []; return }
  const ds = datasets.value.find((d) => d.id === dsId)
  schema.value = ds?.schema_json || []
  try {
    const r = await api.get<any>(`/datasets/${dsId}/rows?offset=0&limit=1000`)
    rows.value = r.rows || []
    if (!schema.value.length) schema.value = (r.columns || []).map((c: string) => ({ name: c, dtype: 'text' }))
  } catch { rows.value = [] }
}

onMounted(load)

async function bindDataset() {
  if (!appRow.value) return
  error.value = null
  const prevId = appRow.value.dataset_id || ''
  try {
    // Persist any live layout edits FIRST, against the current binding. The
    // PATCH below replaces appRow wholesale with the server row, so a plain
    // dataset_id PATCH used to silently discard unsaved component edits.
    if ((dirty.value || rulesDirty.value) && !isPublished.value) {
      await save()
      if (error.value) {
        bindId.value = prevId // save failed - the binding did not change
        return
      }
    }
    const updated = await api.patch<any>(`/apps/${appRow.value.id}`, { dataset_id: bindId.value })
    appRow.value = updated
    dirty.value = false
    await loadBound(bindId.value)
    notice.value = bindId.value ? 'Dataset bound' : 'Dataset unbound'
  } catch (e: any) {
    bindId.value = prevId // the select must reflect the app's real binding
    error.value = e?.data?.detail || e?.message || 'Bind failed'
  }
}

async function regenerate() {
  if (!appRow.value) return
  error.value = null
  try {
    const updated = await api.post<any>(`/apps/${appRow.value.id}/generate`)
    appRow.value = updated
    dirty.value = false
    notice.value = 'Layout regenerated from the dataset'
  } catch (e: any) {
    error.value = e?.data?.detail || e?.message || 'Regenerate failed'
  }
}

// v142: export the bound dataset straight from the builder (owner-scoped -
// the editor always sees the whole dataset, unlike a grant-scoped viewer).
const exportOpen = ref(false)
const exporting = ref(false)
async function exportData(fmt: 'csv' | 'xlsx' | 'json') {
  exportOpen.value = false
  if (!bindId.value) return
  exporting.value = true
  error.value = null
  try {
    await download(`/datasets/${bindId.value}/export?fmt=${fmt}`, `${appRow.value?.slug || 'data'}.${fmt}`)
  } catch (e: any) {
    error.value = e?.message || 'Export failed'
  } finally {
    exporting.value = false
  }
}

// v143: sync the bound dataset to a Google Sheet tab, on demand. Settings
// live server-side in config.sheets_sync (same pattern as rules - editable
// without touching the locked layout config of a published app).
interface SheetsSyncCfg { sheet: string; tab: string; credential_id: string | null; write_mode: 'overwrite' | 'append' }
interface CredentialLite { id: string; name: string; type: string }
const sheetsSyncOpen = ref(false)
const sheetsSyncCfg = ref<SheetsSyncCfg>({ sheet: '', tab: '', credential_id: null, write_mode: 'overwrite' })
const sheetsSyncSaving = ref(false)
const sheetsSyncSyncing = ref(false)
const sheetsSyncError = ref<string | null>(null)
const sheetsSyncResult = ref<string | null>(null)
const sheetsCredentials = ref<CredentialLite[]>([])
const showNewSheetsCred = ref(false)
const newCredName = ref('')
const newCredJson = ref('')
const newCredError = ref<string | null>(null)

async function loadSheetsSync() {
  if (!appRow.value) return
  try {
    const r = await api.get<any>(`/apps/${appRow.value.id}/sheets-sync`)
    sheetsSyncCfg.value = { write_mode: 'overwrite', ...r.sheets_sync }
  } catch { /* new app, nothing saved yet - defaults are fine */ }
}

async function loadSheetsCredentials() {
  try {
    const all = await api.get<CredentialLite[]>('/credentials')
    sheetsCredentials.value = (all || []).filter((c) => c.type === 'google_service_account')
  } catch { sheetsCredentials.value = [] }
}

function toggleSheetsSync() {
  sheetsSyncOpen.value = !sheetsSyncOpen.value
  if (sheetsSyncOpen.value && !sheetsCredentials.value.length) loadSheetsCredentials()
}

async function saveSheetsSync() {
  if (!appRow.value) return
  sheetsSyncSaving.value = true
  sheetsSyncError.value = null
  sheetsSyncResult.value = null
  try {
    const r = await api.put<any>(`/apps/${appRow.value.id}/sheets-sync`, sheetsSyncCfg.value)
    sheetsSyncCfg.value = r.sheets_sync
    sheetsSyncResult.value = 'Saved'
  } catch (e: any) {
    sheetsSyncError.value = e?.data?.detail || e?.message || 'Save failed'
  } finally {
    sheetsSyncSaving.value = false
  }
}

async function runSheetsSyncNow() {
  if (!appRow.value) return
  sheetsSyncSyncing.value = true
  sheetsSyncError.value = null
  sheetsSyncResult.value = null
  try {
    const r = await api.post<any>(`/apps/${appRow.value.id}/sheets-sync/run`)
    sheetsSyncResult.value = `Synced ${r.rows_written} row${r.rows_written === 1 ? '' : 's'} to the sheet`
  } catch (e: any) {
    sheetsSyncError.value = e?.data?.detail || e?.message || 'Sync failed'
  } finally {
    sheetsSyncSyncing.value = false
  }
}

async function createSheetsCredential() {
  newCredError.value = null
  if (!newCredName.value.trim()) { newCredError.value = 'Name is required'; return }
  let parsed: any
  try {
    parsed = JSON.parse(newCredJson.value)
  } catch {
    newCredError.value = 'Paste the full service-account JSON key file contents'
    return
  }
  if (!parsed.client_email || !parsed.private_key) {
    newCredError.value = 'That JSON is missing client_email/private_key - use the key file Google Cloud gave you'
    return
  }
  try {
    const cred = await api.post<CredentialLite>('/credentials', {
      name: newCredName.value.trim(),
      type: 'google_service_account',
      data: { json: newCredJson.value },
    })
    sheetsCredentials.value.push(cred)
    sheetsSyncCfg.value.credential_id = cred.id
    showNewSheetsCred.value = false
    newCredName.value = ''
    newCredJson.value = ''
  } catch (e: any) {
    newCredError.value = e?.data?.detail || e?.message || 'Could not save the credential'
  }
}

// Current editor config - components are edited in place on appRow, rules
// live in the separate `rules` ref; the server stores both in one config.
function currentConfig() {
  return {
    components: appRow.value?.config?.components ?? [],
    rules: rules.value,
    theme: appRow.value?.config?.theme,  // v136: per-app accent color
  }
}

// v136: per-app accent color, shown in the editor and applied on /run/[slug]
const DEFAULT_ACCENT = '#8b5cf6'
const ACCENT_PRESETS = ['#8b5cf6', '#0ea5e9', '#10b981', '#f59e0b', '#ef4444', '#ec4899', '#14b8a6', '#64748b']
// v138: chart types that share the group_by + agg + labels/values pipeline -
// gauge/heatmap/radar each need their own fields (see editor template below).
const GROUPED_CHART_TYPES = ['bar', 'line', 'area', 'pie', 'donut', 'funnel', 'treemap']
const appAccent = computed(() => appRow.value?.config?.theme?.accent || DEFAULT_ACCENT)
function setAppAccent(hex: string | null) {
  if (!appRow.value) return
  if (!appRow.value.config.theme) appRow.value.config.theme = {}
  appRow.value.config.theme.accent = hex || undefined
  touch()
}

// v140: card radius + spacing density, mirrored from pages/run/[slug].vue so
// the live preview matches the published runtime. Light/dark itself is the
// platform-wide toggle (useTheme) - every zinc-* class already follows it via
// CSS variables; only ECharts' literal hex colors need to read it directly.
const { theme } = useTheme()
const RADIUS_OPTIONS = ['sharp', 'rounded', 'soft'] as const
const DENSITY_OPTIONS = ['compact', 'comfortable', 'spacious'] as const
const appRadius = computed(() => appRow.value?.config?.theme?.radius || 'rounded')
const appDensity = computed(() => appRow.value?.config?.theme?.density || 'comfortable')
function setAppRadius(r: string) {
  if (!appRow.value) return
  if (!appRow.value.config.theme) appRow.value.config.theme = {}
  appRow.value.config.theme.radius = r === 'rounded' ? undefined : r
  touch()
}
function setAppDensity(d: string) {
  if (!appRow.value) return
  if (!appRow.value.config.theme) appRow.value.config.theme = {}
  appRow.value.config.theme.density = d === 'comfortable' ? undefined : d
  touch()
}
function surfaceClass(): string {
  const r = appRadius.value === 'sharp' ? 'rounded-md' : appRadius.value === 'soft' ? 'rounded-3xl' : 'rounded-2xl'
  const p = appDensity.value === 'compact' ? 'p-3' : appDensity.value === 'spacious' ? 'p-6' : 'p-4'
  return `${r} ${p}`
}
function radiusClass(): string {
  return appRadius.value === 'sharp' ? 'rounded-md' : appRadius.value === 'soft' ? 'rounded-3xl' : 'rounded-2xl'
}
function gapClass(): string {
  return appDensity.value === 'compact' ? 'gap-3' : appDensity.value === 'spacious' ? 'gap-6' : 'gap-4'
}

// v139: grid layout - full/half/third/two_thirds → a 12-col span (mobile always full)
function widthClass(c: any): string {
  const w = c.width || 'full'
  if (w === 'half') return 'col-span-12 md:col-span-6'
  if (w === 'third') return 'col-span-12 md:col-span-4'
  if (w === 'two_thirds') return 'col-span-12 md:col-span-8'
  return 'col-span-12'
}

// v138: full ECharts option builder, mirrored from pages/run/[slug].vue so
// the builder's live preview matches the published runtime exactly.
function chartIsEmpty(c: any): boolean {
  if (c.chart_type === 'scatter') return !c.points?.length
  if (c.chart_type === 'gauge') return c.value === null || c.value === undefined
  if (c.chart_type === 'radar') return !c.metrics?.length
  if (c.chart_type === 'heatmap') return !c.cells?.length
  return !c.labels?.length
}

function chartTheme() {
  const isLight = theme.value === 'light'
  return {
    text: isLight ? '#52525b' : '#a1a1aa',
    dim: isLight ? '#a1a1aa' : '#71717a',
    grid: isLight ? '#e4e4e7' : '#27272a',
    axisLine: isLight ? '#d4d4d8' : '#3f3f46',
    border: isLight ? '#ffffff' : '#09090b',
    strong: isLight ? '#18181b' : '#e4e4e7',
  }
}

function chartOption(c: any): any {
  const acc = appAccent.value
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

async function save() {
  if (!appRow.value) return
  saving.value = true
  error.value = null
  try {
    const payload: Record<string, any> = {
      name: editingName.value.trim() || appRow.value.name,
      description: editingDesc.value,
    }
    // The layout used to ride only in local state - this PATCH never carried
    // it, so Save/Publish persisted a stale server config and every component
    // edit was silently lost on reload. Send the full editor config (PATCH
    // replaces config wholesale, so rules go along to avoid wiping them).
    // Only when a dataset is bound: the backend validates component columns
    // against the bound schema, and an unbound app has none.
    if (!isPublished.value && appRow.value.dataset_id) payload.config = currentConfig()
    const updated = await api.patch<any>(`/apps/${appRow.value.id}`, payload)
    appRow.value = updated
    editingName.value = updated.name
    rules.value = (updated.config?.rules || []).map((r: any) => JSON.parse(JSON.stringify(r)))
    rulesDirty.value = false
    dirty.value = false
    notice.value = 'Saved'
  } catch (e: any) {
    error.value = e?.data?.detail || e?.message || 'Save failed'
  } finally {
    saving.value = false
  }
}

async function togglePublish() {
  if (!appRow.value) return
  if (dirty.value) {
    await save()
    // a failed save must not publish the stale server config
    if (error.value) return
  }
  publishing.value = true
  error.value = null
  try {
    if (isPublished.value) {
      appRow.value = await api.post<any>(`/apps/${appRow.value.id}/unpublish`)
      notice.value = 'Unpublished - back to draft'
    } else {
      appRow.value = await api.post<any>(`/apps/${appRow.value.id}/publish`)
      notice.value = `Published live at /run/${appRow.value.slug}`
    }
  } catch (e: any) {
    error.value = e?.data?.detail || e?.message || 'Publish failed'
  } finally {
    publishing.value = false
  }
}

// ---------------------------------------------------------------- components
const TYPE_ICONS: Record<string, any> = { stat: Gauge, table: Table2, form: ClipboardList, chart: BarChart3, kpi: TrendingUp, markdown: FileText, filter: Filter, button: MousePointerClick }
const TYPE_COLORS: Record<string, string> = {
  stat: 'bg-sky-500/15 text-sky-400',
  table: 'bg-lime-500/15 text-lime-400',
  form: 'bg-amber-500/15 text-amber-400',
  chart: 'bg-violet-500/15 text-violet-400',
  kpi: 'bg-cyan-500/15 text-cyan-400',
  markdown: 'bg-zinc-500/15 text-zinc-300',
  filter: 'bg-rose-500/15 text-rose-400',
  button: 'bg-orange-500/15 text-orange-400',
}

// Collision-proof id for new components/rules. A session counter resets on
// reload, so `stat_new1` could collide with a saved component - the backend
// rejects duplicate component ids with a 400 on the next save/publish.
function genId(prefix: string): string {
  try {
    if (typeof crypto !== 'undefined' && crypto.randomUUID) return `${prefix}_${crypto.randomUUID()}`
  } catch { /* older browsers */ }
  return `${prefix}_${Date.now().toString(36)}${++uidCounter}`
}

function addComponent(type: AppComponent['type']) {
  if (!appRow.value) return
  const comps2 = appRow.value.config.components || (appRow.value.config.components = [])
  if (comps2.length >= 24) { error.value = 'Too many components (max 24)'; return }
  const cols = schema.value.map((c) => c.name)
  const numeric = schema.value.filter((c) => c.dtype === 'integer' || c.dtype === 'number').map((c) => c.name)
  const text = schema.value.filter((c) => c.dtype === 'text').map((c) => c.name)
  if (type === 'stat') {
    comps2.push({ id: genId('stat'), type, label: 'New stat', agg: numeric.length ? 'avg' : 'count', column: numeric[0] })
  } else if (type === 'kpi') {
    comps2.push({ id: genId('kpi'), type, label: 'New KPI', agg: numeric.length ? 'sum' : 'count', column: numeric[0] })
  } else if (type === 'table') {
    comps2.push({ id: genId('table'), type, title: 'Records', columns: cols.slice(0, 8), page_size: 10 })
  } else if (type === 'form') {
    comps2.push({ id: genId('form'), type, title: 'Add record', fields: cols.slice(0, 6), submit_label: 'Create' })
  } else if (type === 'markdown') {
    comps2.push({ id: genId('md'), type, title: 'Note', body: '## Heading\n**bold**, *italic*, `code` and [links](https://example.com)' })
  } else if (type === 'filter') {
    comps2.push({ id: genId('filter'), type, column: text[0] || cols[0], label: 'Filter' })
  } else if (type === 'button') {
    comps2.push({ id: genId('btn'), type, label: 'Click me', style: 'primary', action: 'link', url: 'https://', new_tab: true })
  } else {
    comps2.push({ id: genId('chart'), type, title: 'Breakdown', chart_type: 'bar', group_by: text[0] || cols[0], agg: 'count' })
  }
  touch()
}

// v138: radar chart metric picker - toggles a numeric column in/out of comp.metrics
function toggleRadarMetric(comp: AppComponent, name: string, checked: boolean) {
  const cur = comp.metrics || []
  comp.metrics = checked ? [...cur.filter((m) => m !== name), name] : cur.filter((m) => m !== name)
  touch()
}

function removeComponent(i: number) {
  if (!appRow.value) return
  appRow.value.config.components?.splice(i, 1)
  touch()
}

// v134: freeform reassembly - drag-and-drop reorder (native HTML5 DnD, no
// library) plus keyboard-accessible move-up/move-down buttons as a fallback
// for anyone who can't or doesn't want to drag. Both mutate the same
// components array in place, so either path stays in sync with save/preview.
function moveComponent(i: number, dir: -1 | 1) {
  if (!appRow.value) return
  const comps2 = appRow.value.config.components
  if (!comps2) return
  const j = i + dir
  if (j < 0 || j >= comps2.length) return
  const [item] = comps2.splice(i, 1)
  comps2.splice(j, 0, item)
  touch()
}

const dragIndex = ref<number | null>(null)
const dragOverIndex = ref<number | null>(null)

function onDragStart(i: number, ev: DragEvent) {
  if (isPublished.value) { ev.preventDefault(); return }
  dragIndex.value = i
  ev.dataTransfer?.setData('text/plain', String(i))
  if (ev.dataTransfer) ev.dataTransfer.effectAllowed = 'move'
}
function onDragOver(i: number, ev: DragEvent) {
  if (dragIndex.value === null) return
  ev.preventDefault()
  dragOverIndex.value = i
}
function onDrop(i: number, ev: DragEvent) {
  ev.preventDefault()
  const from = dragIndex.value
  dragIndex.value = null
  dragOverIndex.value = null
  if (from === null || from === i || !appRow.value) return
  const comps2 = appRow.value.config.components
  if (!comps2) return
  const [item] = comps2.splice(from, 1)
  comps2.splice(i, 0, item)
  touch()
}
function onDragEnd() {
  dragIndex.value = null
  dragOverIndex.value = null
}

function toggleInList(comp: AppComponent, key: 'columns' | 'fields', col: string) {
  if (key === 'fields') {
    // v30: fields may be option objects - toggle by name, keep the rest intact
    const objs = normFields(comp).map((f) => ({ ...f }))
    const i = objs.findIndex((f) => f.name === col)
    if (i >= 0) objs.splice(i, 1)
    else objs.push({ name: col })
    comp.fields = objs
    touch()
    return
  }
  const list = comp[key] || (comp[key] = [])
  const i = list.indexOf(col)
  if (i >= 0) list.splice(i, 1)
  else list.push(col)
  touch()
}

// ------------------------------------------------------------- form fields (v30)
function fieldName(f: string | FormField): string {
  return typeof f === 'string' ? f : f.name
}

function normFields(comp: AppComponent): FormField[] {
  return (comp.fields || []).map((f: any) => (typeof f === 'string' ? { name: f } : f))
}

function hasField(comp: AppComponent, col: string): boolean {
  return (comp.fields || []).some((f) => fieldName(f as any) === col)
}

function updateField(comp: AppComponent, idx: number, patch: Partial<FormField>) {
  const objs = normFields(comp).map((f) => ({ ...f }))
  objs[idx] = { ...objs[idx], ...patch }
  comp.fields = objs
  touch()
}

function parseOptions(raw: string): (string | number)[] {
  return raw.split(',').map((s) => s.trim()).filter(Boolean)
}

// ------------------------------------------------------------- v141: relations (link a field to another dataset)
function relationDatasetSchema(dsId: string | undefined | null): { name: string; dtype: string }[] {
  return datasets.value.find((d) => d.id === dsId)?.schema_json || []
}

function setFieldRelation(comp: AppComponent, idx: number, dsId: string) {
  if (!dsId) {
    updateField(comp, idx, { relation: null })
    return
  }
  const sch = relationDatasetSchema(dsId)
  const displayCol = sch.find((c) => c.dtype === 'text')?.name || sch[0]?.name || ''
  updateField(comp, idx, { relation: { dataset_id: dsId, display_column: displayCol, value_column: displayCol } })
}

// ------------------------------------------------------------- business rules (v30)
const rules = ref<AppRule[]>([])
const rulesDirty = ref(false)
const rulesSaving = ref(false)
const RULE_OPS = ['eq', 'ne', 'gt', 'gte', 'lt', 'lte', 'contains', 'not_contains', 'starts_with', 'ends_with', 'empty', 'not_empty']
const VALUELESS_OPS = new Set(['empty', 'not_empty'])
const ACTION_COLORS: Record<string, string> = {
  block: 'bg-red-500/15 text-red-400',
  warn: 'bg-amber-500/15 text-amber-400',
  set: 'bg-sky-500/15 text-sky-400',
}

let uidCounter = 0
function addRule() {
  const firstCol = schema.value[0]?.name || ''
  rules.value.push({
    id: genId('rule'),
    name: '',
    event: 'create',
    when: { all: [{ field: firstCol, op: 'not_empty' }] },
    action: 'block',
    message: '',
  })
  rulesDirty.value = true
}

function removeRule(i: number) {
  rules.value.splice(i, 1)
  rulesDirty.value = true
}

function addClause(rule: AppRule) {
  if (!rule.when) rule.when = { all: [] }
  rule.when.all.push({ field: schema.value[0]?.name || '', op: 'eq', value: '' })
  rulesDirty.value = true
}

function ruleSummary(r: AppRule): string {
  const clauses = r.when?.all || []
  if (!clauses.length) return 'always'
  return clauses.map((c) => `${c.field} ${c.op}${VALUELESS_OPS.has(c.op) ? '' : ` ${c.value ?? ''}`}`).join(' AND ')
}

async function saveRules() {
  if (!appRow.value) return
  rulesSaving.value = true
  error.value = null
  try {
    const cleaned = rules.value.map((r) => ({
      ...r,
      name: r.name?.trim() || undefined,
      when: r.when?.all?.length ? { all: r.when.all } : undefined,
    }))
    await api.put(`/apps/${appRow.value.id}/rules`, { rules: cleaned })
    rulesDirty.value = false
    notice.value = 'Rules saved - live immediately'
  } catch (e: any) {
    error.value = e?.data?.detail || e?.message || 'Saving rules failed'
  } finally {
    rulesSaving.value = false
  }
}

// ------------------------------------------------------------- share form link (v30)
const formCopied = ref(false)
async function copyFormLink() {
  if (!appRow.value) return
  const url = `${window.location.origin}/f/${appRow.value.slug}`
  try {
    await navigator.clipboard.writeText(url)
  } catch {
    window.prompt('Copy the form link:', url)
  }
  formCopied.value = true
  setTimeout(() => (formCopied.value = false), 2000)
}

// ------------------------------------------------------------- share link (v47)
// The share_token lives on the app row itself - nothing is persisted
// client-side; toggling PUTs {enabled} and the response row carries the
// fresh (or cleared) token.
const shareOpen = ref(false)
const shareBusy = ref(false)
const shareCopied = ref(false)

const shareProtected = computed(() => !!appRow.value?.share_token)

function shareUrl(): string {
  if (!appRow.value?.share_token) return ''
  return `${window.location.origin}/run/${appRow.value.slug}?t=${appRow.value.share_token}`
}

async function toggleShare() {
  if (!appRow.value) return
  shareBusy.value = true
  error.value = null
  try {
    appRow.value = await api.put<any>(`/apps/${appRow.value.id}/share`, { enabled: !appRow.value.share_token })
  } catch (e: any) {
    error.value = e?.data?.detail || e?.message || 'Share toggle failed'
  } finally {
    shareBusy.value = false
  }
}

async function copyShare() {
  const url = shareUrl()
  if (!url) return
  try {
    await navigator.clipboard.writeText(url)
  } catch {
    window.prompt('Copy the share link:', url)
  }
  shareCopied.value = true
  setTimeout(() => (shareCopied.value = false), 2000)
}

// ------------------------------------------------- row-level grants (v48)
// Named share doors: each grant pairs a token with a row filter, so a
// viewer only ever sees (and for eq grants, writes) rows in their slice.
interface ShareGrant {
  id: string
  name: string
  token: string
  row_filter: { column: string; op: string; value: unknown }
  enabled: boolean
  created_at: string | null
  url: string
  access_count?: number
  last_access_at?: string | null
}

// v49: share-surface audit trail (newest events from /grants/audit)
interface GrantEvent {
  id: string
  grant_id: string | null
  grant_name: string | null
  action: string
  outcome: 'allowed' | 'denied'
  detail: string | null
  created_at: string | null
}

const grants = ref<ShareGrant[]>([])
const grantsLoading = ref(false)
const grantEvents = ref<GrantEvent[]>([])
const showGrantActivity = ref(false)
const grantName = ref('')
const grantColumn = ref('')
const grantOp = ref<'eq' | 'in' | 'neq'>('eq')
const grantValue = ref('')
const grantBusy = ref(false)
const grantError = ref('')
const grantCopied = ref('')

const schemaColumns = computed<string[]>(() =>
  (schema.value || []).map((c: any) => c.name),
)

async function loadGrants() {
  if (!appRow.value) return
  grantsLoading.value = true
  try {
    const [list, audit] = await Promise.all([
      api.get<ShareGrant[]>(`/apps/${appRow.value.id}/grants`),
      api.get<GrantEvent[]>(`/apps/${appRow.value.id}/grants/audit?limit=8`).catch(() => [] as GrantEvent[]),
    ])
    grants.value = list
    grantEvents.value = audit
  } catch {
    grants.value = []
    grantEvents.value = []
  } finally {
    grantsLoading.value = false
  }
}

watch(shareOpen, (open) => {
  if (open) loadGrants()
})

function parseGrantValue(): unknown {
  const raw = grantValue.value.trim()
  if (grantOp.value === 'in') {
    return raw.split(',').map((s) => s.trim()).filter(Boolean)
  }
  if (raw === 'true') return true
  if (raw === 'false') return false
  if (raw !== '' && !Number.isNaN(Number(raw))) return Number(raw)
  return raw
}

async function createGrant() {
  if (!appRow.value || !grantName.value.trim() || !grantColumn.value || !grantValue.value.trim()) return
  grantBusy.value = true
  grantError.value = ''
  try {
    await api.post(`/apps/${appRow.value.id}/grants`, {
      name: grantName.value.trim(),
      column: grantColumn.value,
      op: grantOp.value,
      value: parseGrantValue(),
    })
    grantName.value = ''
    grantValue.value = ''
    await loadGrants()
  } catch (e: any) {
    grantError.value = e?.data?.detail || e?.message || 'Create grant failed'
  } finally {
    grantBusy.value = false
  }
}

async function toggleGrant(g: ShareGrant) {
  if (!appRow.value) return
  try {
    await api.put(`/apps/${appRow.value.id}/grants/${g.id}`, { enabled: !g.enabled })
    await loadGrants()
  } catch (e: any) {
    grantError.value = e?.data?.detail || e?.message || 'Toggle failed'
  }
}

async function revokeGrant(g: ShareGrant) {
  if (!appRow.value) return
  if (!confirm(`Revoke grant "${g.name}"? Every link holding its token stops working now.`)) return
  try {
    await api.del(`/apps/${appRow.value.id}/grants/${g.id}`)
    await loadGrants()
  } catch (e: any) {
    grantError.value = e?.data?.detail || e?.message || 'Revoke failed'
  }
}

async function copyGrantUrl(g: ShareGrant) {
  const url = `${window.location.origin}${g.url}`
  try {
    await navigator.clipboard.writeText(url)
  } catch {
    window.prompt('Copy the grant link:', url)
  }
  grantCopied.value = g.id
  setTimeout(() => (grantCopied.value = ''), 2000)
}

function grantSummary(g: ShareGrant): string {
  const f = g.row_filter || { column: '?', op: '?', value: '?' }
  const val = Array.isArray(f.value) ? f.value.join(', ') : String(f.value)
  return `${f.column} ${f.op === 'eq' ? '=' : f.op === 'neq' ? '!=' : 'in'} ${val}`
}

// v49: audit helpers
const ACTION_LABELS: Record<string, string> = {
  view_runtime: 'viewed the app',
  list_records: 'listed records',
  create_record: 'created a record',
  update_record: 'edited a record',
  delete_record: 'deleted a record',
  view_form: 'opened the form',
  submit_form: 'submitted the form',
  access: 'knocked on the door',
}

function eventLabel(e: GrantEvent): string {
  const who = e.grant_name || 'unknown caller'
  const what = ACTION_LABELS[e.action] || e.action
  return `${who} ${what}`
}

function timeAgo(iso: string | null): string {
  if (!iso) return ''
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000)
  if (s < 60) return 'just now'
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  return `${Math.floor(s / 86400)}d ago`
}

// ---------------------------------------------------------------- preview
function numVal(v: any): number | null {
  const n = typeof v === 'number' ? v : parseFloat(v)
  return Number.isFinite(n) ? n : null
}

function statValue(comp: AppComponent): string {
  if (comp.agg === 'count') return String(rows.value.length)
  const col = comp.column
  const nums = rows.value.map((r) => numVal(r[col])).filter((n): n is number => n !== null)
  if (!nums.length) return '-'
  let v: number
  if (comp.agg === 'sum') v = nums.reduce((a, b) => a + b, 0)
  else if (comp.agg === 'min') v = Math.min(...nums)
  else if (comp.agg === 'max') v = Math.max(...nums)
  else v = nums.reduce((a, b) => a + b, 0) / nums.length
  return Math.abs(v) >= 1000 ? Math.round(v).toLocaleString() : String(Math.round(v * 100) / 100)
}

const chartComp = computed(() => comps.value.find((c) => c.type === 'chart'))
const chartData = computed(() => {
  const comp = chartComp.value
  if (!comp?.group_by) return { labels: [], values: [] }
  const counts: Record<string, number> = {}
  for (const r of rows.value) {
    const k = String(r[comp.group_by] ?? '(blank)')
    const v = comp.agg && comp.agg !== 'count' ? numVal(r[comp.column || '']) : 1
    if (v === null) continue
    counts[k] = (counts[k] || 0) + v
  }
  const entries = Object.entries(counts).sort((a, b) => b[1] - a[1]).slice(0, 12)
  return { labels: entries.map((e) => e[0]), values: entries.map((e) => Math.round(e[1] * 100) / 100) }
})

const tableComp = computed(() => comps.value.find((c) => c.type === 'table'))
const tableRows = computed(() => {
  const cols = tableComp.value?.columns
  if (!cols?.length) return rows.value.slice(0, tableComp.value?.page_size || 10)
  return rows.value.slice(0, tableComp.value?.page_size || 10)
})

const formComp = computed(() => comps.value.find((c) => c.type === 'form'))

function dtypeOf(col: string) {
  return schema.value.find((c) => c.name === col)?.dtype || 'text'
}
</script>

<template>
  <div class="min-h-screen pb-16 text-zinc-100">
    <!-- top bar -->
    <header class="sticky top-0 z-20 border-b border-zinc-800/80 bg-zinc-950/90 backdrop-blur">
      <div class="mx-auto flex max-w-7xl items-center gap-3 px-4 py-3 lg:px-6">
        <button class="rounded-lg p-1.5 text-zinc-500 transition hover:bg-zinc-900 hover:text-zinc-200" title="Back to apps" @click="navigateTo('/apps')">
          <ArrowLeft class="h-4 w-4" />
        </button>
        <div class="min-w-0 flex-1">
          <div class="flex items-center gap-2">
            <input
              v-model="editingName"
              class="min-w-0 max-w-xs truncate rounded-lg border border-transparent bg-transparent px-1.5 py-0.5 text-sm font-bold outline-none transition hover:border-zinc-700 focus:border-violet-500/60"
              :disabled="isPublished"
              @input="touch"
            />
            <span
              class="rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase"
              :class="isPublished ? 'bg-emerald-500/15 text-emerald-400' : 'bg-amber-500/15 text-amber-400'"
            >{{ appRow?.status || '…' }}</span>
          </div>
          <p class="ml-1.5 text-[11px] text-zinc-500">
            {{ appRow?.dataset_name ? `bound to ${appRow.dataset_name}` : 'no dataset bound' }}
            <template v-if="appRow && isPublished"> · /run/{{ appRow.slug }}</template>
          </p>
        </div>
        <button
          class="flex items-center gap-1.5 rounded-lg border border-zinc-800 bg-zinc-900/60 px-3 py-1.5 text-xs font-medium text-zinc-300 transition hover:border-zinc-600"
          :title="appRow?.share_token ? 'Share protection is ON - links need the token' : 'Share protection is OFF'"
          @click="shareOpen = true"
        >
          <Share2 class="h-3.5 w-3.5" /> Share
        </button>
        <button
          v-if="isPublished && formComp"
          class="flex items-center gap-1.5 rounded-lg border border-sky-500/30 bg-sky-500/10 px-3 py-1.5 text-xs font-medium text-sky-400 transition hover:bg-sky-500/20"
          title="Copy the standalone form link (/f/slug)"
          @click="copyFormLink"
        >
          <Link2 class="h-3.5 w-3.5" /> {{ formCopied ? 'Copied!' : 'Form link' }}
        </button>
        <button
          v-if="isPublished"
          class="flex items-center gap-1.5 rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-3 py-1.5 text-xs font-medium text-emerald-400 transition hover:bg-emerald-500/20"
          @click="navigateTo(`/run/${appRow?.slug}`)"
        >
          <ExternalLink class="h-3.5 w-3.5" /> Open app
        </button>
        <button
          class="flex items-center gap-1.5 rounded-lg border border-zinc-800 bg-zinc-900/60 px-3 py-1.5 text-xs font-medium text-zinc-300 transition hover:border-zinc-600 disabled:opacity-40"
          :disabled="saving || isPublished"
          @click="save"
        >
          <Loader2 v-if="saving" class="h-3.5 w-3.5 animate-spin" />
          <Save v-else class="h-3.5 w-3.5" />
          {{ dirty ? 'Save*' : 'Save' }}
        </button>
        <button
          class="flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-semibold text-white shadow-lg transition disabled:opacity-40"
          :class="isPublished ? 'bg-zinc-700 shadow-none hover:bg-zinc-600' : 'bg-emerald-500 shadow-emerald-500/20 hover:bg-emerald-400'"
          :disabled="publishing"
          @click="togglePublish"
        >
          <Loader2 v-if="publishing" class="h-3.5 w-3.5 animate-spin" />
          <Rocket v-else class="h-3.5 w-3.5" />
          {{ isPublished ? 'Unpublish' : 'Publish' }}
        </button>
      </div>
    </header>

    <div v-if="loading" class="mt-16 flex justify-center text-zinc-500"><Loader2 class="h-6 w-6 animate-spin" /></div>
    <div v-else-if="!appRow" class="mt-16 text-center text-sm text-zinc-500">{{ error || 'App not found' }}</div>

    <div v-else class="mx-auto max-w-7xl px-4 lg:px-6">
      <p v-if="notice" class="mt-4 rounded-xl border border-emerald-500/30 bg-emerald-500/10 px-4 py-2 text-xs text-emerald-300">{{ notice }}</p>
      <p v-if="error" class="mt-4 flex items-center gap-2 rounded-xl border border-amber-500/30 bg-amber-500/10 px-4 py-2 text-xs text-amber-300">
        <CircleAlert class="h-3.5 w-3.5 shrink-0" /> {{ error }}
      </p>

      <div class="mt-5 grid gap-5 lg:grid-cols-[400px_1fr]">
        <!-- ------------------------------ left: config -->
        <div class="space-y-4">
          <!-- dataset binding -->
          <section class="rounded-2xl border border-zinc-800/80 bg-zinc-900/40 p-4">
            <h2 class="flex items-center gap-2 text-xs font-bold uppercase tracking-wide text-zinc-400">
              <Database class="h-3.5 w-3.5 text-sky-400" /> Data
            </h2>
            <div class="mt-3 flex gap-2">
              <select
                v-model="bindId"
                class="min-w-0 flex-1 rounded-xl border border-zinc-800 bg-zinc-950/60 px-3 py-2 text-sm outline-none focus:border-violet-500/60"
                @change="bindDataset"
              >
                <option value="">- no dataset -</option>
                <option v-for="d in datasets" :key="d.id" :value="d.id">{{ d.name }} ({{ d.row_count }})</option>
              </select>
              <button
                v-if="bindId"
                class="rounded-xl border border-zinc-800 bg-zinc-950/60 px-2.5 text-zinc-400 transition hover:border-amber-500/40 hover:text-amber-400"
                title="Unbind dataset"
                @click="bindId = ''; bindDataset()"
              >
                <Unlink class="h-3.5 w-3.5" />
              </button>
            </div>
            <button
              class="mt-2 flex w-full items-center justify-center gap-1.5 rounded-xl border border-violet-500/30 bg-violet-500/10 py-2 text-xs font-medium text-violet-300 transition hover:bg-violet-500/20 disabled:opacity-40"
              :disabled="!bindId || isPublished"
              @click="regenerate"
            >
              <RefreshCw class="h-3.5 w-3.5" /> Regenerate layout from data
            </button>
            <!-- v142: export the bound dataset straight from the builder -->
            <div v-if="bindId" class="relative mt-1.5">
              <button
                class="flex w-full items-center justify-center gap-1.5 rounded-xl border border-zinc-800 bg-zinc-900/60 py-2 text-xs font-medium text-zinc-400 transition hover:text-zinc-200 disabled:opacity-40"
                :disabled="exporting"
                @click="exportOpen = !exportOpen"
              >
                <Loader2 v-if="exporting" class="h-3.5 w-3.5 animate-spin" />
                <Download v-else class="h-3.5 w-3.5" /> Export data
              </button>
              <div v-if="exportOpen" class="absolute left-0 right-0 top-full z-30 mt-1 overflow-hidden rounded-lg border border-zinc-800 bg-zinc-900 py-1 shadow-xl" @click.self="exportOpen = false">
                <button class="block w-full px-3 py-1.5 text-left text-xs text-zinc-300 hover:bg-zinc-800" @click="exportData('csv')">CSV</button>
                <button class="block w-full px-3 py-1.5 text-left text-xs text-zinc-300 hover:bg-zinc-800" @click="exportData('xlsx')">Excel (.xlsx)</button>
                <button class="block w-full px-3 py-1.5 text-left text-xs text-zinc-300 hover:bg-zinc-800" @click="exportData('json')">JSON</button>
              </div>
            </div>
          </section>

          <!-- v143: Google Sheets sync - push the bound dataset to a sheet on demand -->
          <section v-if="bindId" class="rounded-2xl border border-zinc-800/80 bg-zinc-900/40 p-4">
            <button class="flex w-full items-center justify-between text-left" @click="toggleSheetsSync">
              <h2 class="flex items-center gap-1.5 text-xs font-bold uppercase tracking-wide text-zinc-400">
                <FileText class="h-3.5 w-3.5 text-emerald-400" /> Google Sheets sync
              </h2>
              <ChevronDown class="h-3.5 w-3.5 text-zinc-500 transition" :class="{ 'rotate-180': sheetsSyncOpen }" />
            </button>
            <div v-if="sheetsSyncOpen" class="mt-3 space-y-2.5">
              <p class="text-[11px] text-zinc-500">Push this app's records into a Google Sheet tab, on demand. Share the sheet with the service account's email as Editor first.</p>
              <div>
                <label class="text-[10px] font-medium uppercase tracking-wide text-zinc-500">Sheet URL or ID</label>
                <input
                  v-model="sheetsSyncCfg.sheet"
                  type="text"
                  placeholder="https://docs.google.com/spreadsheets/d/..."
                  class="mt-1 w-full rounded-lg border border-zinc-800 bg-zinc-950/60 px-2.5 py-1.5 text-xs outline-none focus:border-emerald-500/60"
                />
              </div>
              <div>
                <label class="text-[10px] font-medium uppercase tracking-wide text-zinc-500">Tab name</label>
                <input
                  v-model="sheetsSyncCfg.tab"
                  type="text"
                  placeholder="Sheet1"
                  class="mt-1 w-full rounded-lg border border-zinc-800 bg-zinc-950/60 px-2.5 py-1.5 text-xs outline-none focus:border-emerald-500/60"
                />
              </div>
              <div>
                <label class="text-[10px] font-medium uppercase tracking-wide text-zinc-500">Write mode</label>
                <select v-model="sheetsSyncCfg.write_mode" class="mt-1 w-full rounded-lg border border-zinc-800 bg-zinc-950/60 px-2.5 py-1.5 text-xs outline-none focus:border-emerald-500/60">
                  <option value="overwrite">Overwrite (clears the tab, writes header + rows)</option>
                  <option value="append">Append (adds rows after what's there)</option>
                </select>
              </div>
              <div>
                <label class="text-[10px] font-medium uppercase tracking-wide text-zinc-500">Service-account credential</label>
                <select v-model="sheetsSyncCfg.credential_id" class="mt-1 w-full rounded-lg border border-zinc-800 bg-zinc-950/60 px-2.5 py-1.5 text-xs outline-none focus:border-emerald-500/60">
                  <option :value="null">- select -</option>
                  <option v-for="c in sheetsCredentials" :key="c.id" :value="c.id">{{ c.name }}</option>
                </select>
                <button class="mt-1.5 text-[11px] text-emerald-400 hover:underline" @click="showNewSheetsCred = !showNewSheetsCred">
                  {{ showNewSheetsCred ? 'Cancel' : '+ New service-account credential' }}
                </button>
                <div v-if="showNewSheetsCred" class="mt-2 space-y-2 rounded-lg border border-zinc-800 bg-zinc-950/60 p-2.5">
                  <input
                    v-model="newCredName"
                    type="text"
                    placeholder="Credential name"
                    class="w-full rounded-lg border border-zinc-800 bg-zinc-900/60 px-2.5 py-1.5 text-xs outline-none focus:border-emerald-500/60"
                  />
                  <textarea
                    v-model="newCredJson"
                    rows="4"
                    placeholder='Paste the full service-account JSON key file, e.g. {"type": "service_account", "client_email": "...", "private_key": "...", ...}'
                    class="w-full rounded-lg border border-zinc-800 bg-zinc-900/60 px-2.5 py-1.5 font-mono text-[11px] outline-none focus:border-emerald-500/60"
                  />
                  <p v-if="newCredError" class="text-[11px] text-red-400">{{ newCredError }}</p>
                  <button class="w-full rounded-lg bg-emerald-500/15 py-1.5 text-xs font-medium text-emerald-300 hover:bg-emerald-500/25" @click="createSheetsCredential">
                    Save credential
                  </button>
                </div>
              </div>
              <div class="flex gap-2">
                <button
                  class="flex flex-1 items-center justify-center gap-1.5 rounded-xl border border-zinc-800 bg-zinc-950/60 py-2 text-xs font-medium text-zinc-300 transition hover:text-zinc-100 disabled:opacity-40"
                  :disabled="sheetsSyncSaving"
                  @click="saveSheetsSync"
                >
                  <Loader2 v-if="sheetsSyncSaving" class="h-3.5 w-3.5 animate-spin" /> Save settings
                </button>
                <button
                  class="flex flex-1 items-center justify-center gap-1.5 rounded-xl border border-emerald-500/30 bg-emerald-500/10 py-2 text-xs font-medium text-emerald-300 transition hover:bg-emerald-500/20 disabled:opacity-40"
                  :disabled="sheetsSyncSyncing || !sheetsSyncCfg.sheet || !sheetsSyncCfg.credential_id"
                  @click="runSheetsSyncNow"
                >
                  <Loader2 v-if="sheetsSyncSyncing" class="h-3.5 w-3.5 animate-spin" /> Sync now
                </button>
              </div>
              <p v-if="sheetsSyncResult" class="text-[11px] text-emerald-400">{{ sheetsSyncResult }}</p>
              <p v-if="sheetsSyncError" class="text-[11px] text-red-400">{{ sheetsSyncError }}</p>
            </div>
          </section>

          <!-- description -->
          <section class="rounded-2xl border border-zinc-800/80 bg-zinc-900/40 p-4">
            <h2 class="text-xs font-bold uppercase tracking-wide text-zinc-400">Description</h2>
            <textarea
              v-model="editingDesc"
              rows="2"
              class="mt-2 w-full rounded-xl border border-zinc-800 bg-zinc-950/60 px-3 py-2 text-sm outline-none focus:border-violet-500/60"
              :disabled="isPublished"
              @input="touch"
            />
          </section>

          <!-- theme (v136): per-app accent color, applied to the published runtime -->
          <section class="rounded-2xl border border-zinc-800/80 bg-zinc-900/40 p-4">
            <h2 class="text-xs font-bold uppercase tracking-wide text-zinc-400">Theme</h2>
            <p class="mt-1 text-[11px] text-zinc-500">Pick an accent color - it's applied to buttons, active nav, and charts on the published app.</p>
            <div class="mt-2.5 flex flex-wrap items-center gap-2.5">
              <input
                type="color"
                :value="appAccent"
                class="h-9 w-9 cursor-pointer rounded-lg border border-zinc-800 bg-zinc-950/60 p-0.5"
                :disabled="isPublished"
                @input="setAppAccent(($event.target as HTMLInputElement).value)"
              />
              <button
                v-for="c in ACCENT_PRESETS"
                :key="c"
                type="button"
                class="h-6 w-6 rounded-full border-2 transition"
                :class="appAccent.toLowerCase() === c.toLowerCase() ? 'border-white/80' : 'border-transparent hover:border-white/40'"
                :style="{ background: c }"
                :disabled="isPublished"
                @click="setAppAccent(c)"
              />
              <span class="text-[11px] font-mono text-zinc-500">{{ appAccent }}</span>
              <button
                v-if="appRow?.config?.theme?.accent"
                type="button"
                class="ml-auto text-[10px] text-zinc-500 underline decoration-dotted hover:text-zinc-300"
                :disabled="isPublished"
                @click="setAppAccent(null)"
              >
                reset to default
              </button>
            </div>

            <!-- v140: corner radius + spacing density - light/dark itself follows the
                 platform-wide toggle in the sidebar, so it isn't a per-app setting here. -->
            <div class="mt-4 grid gap-3 sm:grid-cols-2">
              <div>
                <p class="text-[10px] uppercase tracking-wide text-zinc-500">Corner radius</p>
                <div class="mt-1.5 flex gap-1.5">
                  <button
                    v-for="r in RADIUS_OPTIONS"
                    :key="r"
                    type="button"
                    class="flex-1 border px-2 py-1.5 text-[11px] capitalize transition"
                    :class="[
                      r === 'sharp' ? 'rounded-md' : r === 'soft' ? 'rounded-3xl' : 'rounded-2xl',
                      appRadius === r ? 'border-violet-500/60 bg-violet-500/15 text-violet-300' : 'border-zinc-800 bg-zinc-900/60 text-zinc-500 hover:text-zinc-300',
                    ]"
                    :disabled="isPublished"
                    @click="setAppRadius(r)"
                  >{{ r }}</button>
                </div>
              </div>
              <div>
                <p class="text-[10px] uppercase tracking-wide text-zinc-500">Density</p>
                <div class="mt-1.5 flex gap-1.5">
                  <button
                    v-for="d in DENSITY_OPTIONS"
                    :key="d"
                    type="button"
                    class="flex-1 rounded-lg border px-2 py-1.5 text-[11px] capitalize transition"
                    :class="appDensity === d ? 'border-violet-500/60 bg-violet-500/15 text-violet-300' : 'border-zinc-800 bg-zinc-900/60 text-zinc-500 hover:text-zinc-300'"
                    :disabled="isPublished"
                    @click="setAppDensity(d)"
                  >{{ d }}</button>
                </div>
              </div>
            </div>
          </section>

          <!-- components -->
          <section class="rounded-2xl border border-zinc-800/80 bg-zinc-900/40 p-4">
            <div class="flex items-center justify-between">
              <h2 class="text-xs font-bold uppercase tracking-wide text-zinc-400">Components ({{ comps.length }})</h2>
            </div>
            <div v-if="isPublished" class="mt-2 rounded-lg bg-amber-500/10 px-3 py-2 text-[11px] text-amber-300">
              Published apps are locked - unpublish to edit components.
            </div>
            <!-- v134: drag a component's Page field to name a new sidebar section; -->
            <!-- every existing page name is suggested here so typos don't fork a page in two -->
            <datalist id="app-page-names">
              <option v-for="p in appPages" :key="p" :value="p" />
            </datalist>
            <p v-if="appPages.length > 1" class="mt-2 flex flex-wrap items-center gap-1.5 text-[10px] text-zinc-500">
              <Layers class="h-3 w-3" /> Pages:
              <span v-for="p in appPages" :key="p" class="rounded-full border border-zinc-800 bg-zinc-900/60 px-2 py-0.5 text-zinc-400">{{ p }}</span>
            </p>

            <div class="mt-3 space-y-3">
              <div
                v-for="(comp, i) in comps"
                :key="comp.id"
                class="rounded-xl border p-3 transition"
                :class="dragOverIndex === i && dragIndex !== i ? 'border-violet-500/70 bg-violet-500/5' : 'border-zinc-800 bg-zinc-950/50'"
                :draggable="!isPublished"
                @dragstart="onDragStart(i, $event)"
                @dragover="onDragOver(i, $event)"
                @drop="onDrop(i, $event)"
                @dragend="onDragEnd"
              >
                <div class="flex items-center gap-2">
                  <span
                    class="flex h-6 w-6 shrink-0 items-center justify-center rounded-lg text-zinc-600"
                    :class="isPublished ? 'cursor-not-allowed opacity-30' : 'cursor-grab hover:text-zinc-300 active:cursor-grabbing'"
                    title="Drag to reorder"
                  >
                    <GripVertical class="h-3.5 w-3.5" />
                  </span>
                  <span class="flex h-6 w-6 items-center justify-center rounded-lg" :class="TYPE_COLORS[comp.type]">
                    <component :is="TYPE_ICONS[comp.type]" class="h-3 w-3" />
                  </span>
                  <span class="flex-1 truncate text-xs font-semibold text-zinc-300">{{ comp.title || comp.label || comp.id }}</span>
                  <div class="flex items-center gap-0.5">
                    <button class="rounded p-1 text-zinc-600 transition hover:bg-zinc-800 hover:text-zinc-300 disabled:opacity-20" :disabled="isPublished || i === 0" title="Move up" @click="moveComponent(i, -1)">
                      <ChevronUp class="h-3.5 w-3.5" />
                    </button>
                    <button class="rounded p-1 text-zinc-600 transition hover:bg-zinc-800 hover:text-zinc-300 disabled:opacity-20" :disabled="isPublished || i === comps.length - 1" title="Move down" @click="moveComponent(i, 1)">
                      <ChevronDown class="h-3.5 w-3.5" />
                    </button>
                    <button class="rounded p-1 text-zinc-600 transition hover:bg-amber-500/10 hover:text-amber-400 disabled:opacity-30" :disabled="isPublished" @click="removeComponent(i)">
                      <Trash2 class="h-3.5 w-3.5" />
                    </button>
                  </div>
                </div>

                <!-- v134: which sidebar/tab page this component lives on -->
                <div class="mt-2 flex items-center gap-1.5 text-[10px] text-zinc-500">
                  <Layers class="h-3 w-3 shrink-0" />
                  <span class="shrink-0">Page</span>
                  <input
                    :value="comp.page || DEFAULT_PAGE"
                    list="app-page-names"
                    placeholder="Dashboard"
                    class="min-w-0 flex-1 rounded-md border border-zinc-800 bg-zinc-900/60 px-2 py-1 text-[11px] outline-none focus:border-violet-500/60"
                    :disabled="isPublished"
                    @change="comp.page = ($event.target as HTMLInputElement).value.trim() || undefined; touch()"
                  />
                </div>

                <!-- v139: grid layout - how much of the row this component fills -->
                <div class="mt-1.5 flex items-center gap-1.5 text-[10px] text-zinc-500">
                  <LayoutGrid class="h-3 w-3 shrink-0" />
                  <span class="shrink-0">Width</span>
                  <div class="flex flex-1 gap-1">
                    <button
                      v-for="w in (['full', 'half', 'third', 'two_thirds'] as const)"
                      :key="w"
                      type="button"
                      class="flex-1 rounded-md border px-1.5 py-1 text-[10px] transition"
                      :class="(comp.width || 'full') === w ? 'border-violet-500/60 bg-violet-500/15 text-violet-300' : 'border-zinc-800 bg-zinc-900/60 text-zinc-500 hover:text-zinc-300'"
                      :disabled="isPublished"
                      @click="comp.width = w === 'full' ? undefined : w; touch()"
                    >{{ w === 'two_thirds' ? '⅔' : w === 'third' ? '⅓' : w === 'half' ? '½' : 'full' }}</button>
                  </div>
                </div>

                <div class="mt-2 space-y-2">
                  <!-- stat editors -->
                  <template v-if="comp.type === 'stat'">
                    <input v-model="comp.label" placeholder="Label" class="w-full rounded-lg border border-zinc-800 bg-zinc-900/60 px-2.5 py-1.5 text-xs outline-none focus:border-violet-500/60" :disabled="isPublished" @input="touch" />
                    <div class="flex gap-2">
                      <select v-model="comp.agg" class="flex-1 rounded-lg border border-zinc-800 bg-zinc-900/60 px-2 py-1.5 text-xs outline-none focus:border-violet-500/60" :disabled="isPublished" @change="touch">
                        <option value="count">count</option><option value="sum">sum</option><option value="avg">avg</option><option value="min">min</option><option value="max">max</option>
                      </select>
                      <select v-if="comp.agg !== 'count'" v-model="comp.column" class="flex-1 rounded-lg border border-zinc-800 bg-zinc-900/60 px-2 py-1.5 text-xs outline-none focus:border-violet-500/60" :disabled="isPublished" @change="touch">
                        <option value="" disabled>column…</option>
                        <option v-for="c in schema" :key="c.name" :value="c.name">{{ c.name }}</option>
                      </select>
                    </div>
                  </template>

                  <!-- table editors -->
                  <template v-else-if="comp.type === 'table'">
                    <input v-model="comp.title" placeholder="Title" class="w-full rounded-lg border border-zinc-800 bg-zinc-900/60 px-2.5 py-1.5 text-xs outline-none focus:border-violet-500/60" :disabled="isPublished" @input="touch" />
                    <div class="flex flex-wrap gap-1">
                      <button
                        v-for="c in schema" :key="c.name"
                        class="rounded-full border px-2 py-0.5 text-[10px] transition"
                        :class="(comp.columns || []).includes(c.name) ? 'border-lime-500/50 bg-lime-500/10 text-lime-300' : 'border-zinc-800 text-zinc-500 hover:border-zinc-600'"
                        :disabled="isPublished"
                        @click="toggleInList(comp, 'columns', c.name)"
                      >{{ c.name }}</button>
                    </div>
                    <label class="flex items-center gap-2 text-[11px] text-zinc-500">rows per page
                      <input v-model.number="comp.page_size" type="number" min="1" max="100" class="w-16 rounded-lg border border-zinc-800 bg-zinc-900/60 px-2 py-1 text-xs outline-none focus:border-violet-500/60" :disabled="isPublished" @input="touch" />
                    </label>
                  </template>

                  <!-- form editors -->
                  <template v-else-if="comp.type === 'form'">
                    <input v-model="comp.title" placeholder="Title" class="w-full rounded-lg border border-zinc-800 bg-zinc-900/60 px-2.5 py-1.5 text-xs outline-none focus:border-violet-500/60" :disabled="isPublished" @input="touch" />
                    <div class="flex flex-wrap gap-1">
                      <button
                        v-for="c in schema" :key="c.name"
                        class="rounded-full border px-2 py-0.5 text-[10px] transition"
                        :class="hasField(comp, c.name) ? 'border-amber-500/50 bg-amber-500/10 text-amber-300' : 'border-zinc-800 text-zinc-500 hover:border-zinc-600'"
                        :disabled="isPublished"
                        @click="toggleInList(comp, 'fields', c.name)"
                      >{{ c.name }}</button>
                    </div>
                    <input v-model="comp.submit_label" placeholder="Submit button label" class="w-full rounded-lg border border-zinc-800 bg-zinc-900/60 px-2.5 py-1.5 text-xs outline-none focus:border-violet-500/60" :disabled="isPublished" @input="touch" />

                    <!-- v30: per-field options -->
                    <div v-if="normFields(comp).length" class="space-y-2 border-t border-zinc-800/80 pt-2">
                      <p class="text-[10px] font-semibold uppercase tracking-wide text-zinc-500">Field options</p>
                      <div
                        v-for="(f, fi) in normFields(comp)"
                        :key="f.name"
                        class="space-y-1.5 rounded-lg border border-zinc-800/80 bg-zinc-950/40 p-2"
                      >
                        <div class="flex items-center gap-2">
                          <span class="text-[11px] font-semibold text-zinc-300">{{ f.name }}</span>
                          <span class="text-[9px] text-zinc-600">{{ dtypeOf(f.name) }}</span>
                          <label v-if="(f.options || []).length" class="ml-auto flex cursor-pointer items-center gap-1 text-[10px] text-zinc-400">
                            <input
                              type="checkbox"
                              class="accent-rose-400"
                              :checked="!!f.multiple"
                              :disabled="isPublished"
                              @change="updateField(comp, fi, { multiple: ($event.target as HTMLInputElement).checked })"
                            />
                            multi-select
                          </label>
                          <label class="flex cursor-pointer items-center gap-1 text-[10px] text-zinc-400" :class="(f.options || []).length ? '' : 'ml-auto'">
                            <input
                              type="checkbox"
                              class="accent-amber-500"
                              :checked="!!f.required"
                              :disabled="isPublished"
                              @change="updateField(comp, fi, { required: ($event.target as HTMLInputElement).checked })"
                            />
                            required
                          </label>
                        </div>
                        <div class="grid grid-cols-2 gap-1.5">
                          <input
                            :value="f.label ?? ''"
                            placeholder="Label"
                            class="rounded-lg border border-zinc-800 bg-zinc-900/60 px-2 py-1 text-[11px] outline-none focus:border-violet-500/60"
                            :disabled="isPublished"
                            @change="updateField(comp, fi, { label: ($event.target as HTMLInputElement).value || null })"
                          />
                          <input
                            :value="(f.options || []).join(', ')"
                            placeholder="options, comma, separated"
                            class="rounded-lg border border-zinc-800 bg-zinc-900/60 px-2 py-1 text-[11px] outline-none focus:border-violet-500/60"
                            :disabled="isPublished"
                            @change="updateField(comp, fi, { options: parseOptions(($event.target as HTMLInputElement).value) })"
                          />
                          <input
                            :value="f.default ?? ''"
                            placeholder="Default value"
                            class="rounded-lg border border-zinc-800 bg-zinc-900/60 px-2 py-1 text-[11px] outline-none focus:border-violet-500/60"
                            :disabled="isPublished"
                            @change="updateField(comp, fi, { default: ($event.target as HTMLInputElement).value || null })"
                          />
                          <input
                            :value="f.placeholder ?? ''"
                            placeholder="Placeholder"
                            class="rounded-lg border border-zinc-800 bg-zinc-900/60 px-2 py-1 text-[11px] outline-none focus:border-violet-500/60"
                            :disabled="isPublished"
                            @change="updateField(comp, fi, { placeholder: ($event.target as HTMLInputElement).value || null })"
                          />
                        </div>

                        <!-- v141: link this field to another dataset - a real relation, not free text -->
                        <div class="space-y-1.5 border-t border-zinc-800/60 pt-1.5">
                          <div class="flex items-center gap-1.5 text-[9px] uppercase tracking-wide text-zinc-600">
                            <Link2 class="h-2.5 w-2.5" /> Link to dataset
                          </div>
                          <div class="grid grid-cols-3 gap-1.5">
                            <select
                              :value="f.relation?.dataset_id || ''"
                              class="col-span-3 rounded-lg border border-zinc-800 bg-zinc-900/60 px-2 py-1 text-[11px] outline-none focus:border-violet-500/60 sm:col-span-1"
                              :disabled="isPublished"
                              @change="setFieldRelation(comp, fi, ($event.target as HTMLSelectElement).value)"
                            >
                              <option value="">no link (plain field)</option>
                              <option v-for="d in datasets" :key="d.id" :value="d.id">{{ d.name }}</option>
                            </select>
                            <template v-if="f.relation">
                              <select
                                :value="f.relation.display_column"
                                class="rounded-lg border border-zinc-800 bg-zinc-900/60 px-2 py-1 text-[11px] outline-none focus:border-violet-500/60"
                                :disabled="isPublished"
                                @change="updateField(comp, fi, { relation: { ...f.relation, display_column: ($event.target as HTMLSelectElement).value } })"
                              >
                                <option v-for="c in relationDatasetSchema(f.relation.dataset_id)" :key="c.name" :value="c.name">{{ c.name }}</option>
                              </select>
                              <select
                                :value="f.relation.value_column || f.relation.display_column"
                                class="rounded-lg border border-zinc-800 bg-zinc-900/60 px-2 py-1 text-[11px] outline-none focus:border-violet-500/60"
                                :disabled="isPublished"
                                @change="updateField(comp, fi, { relation: { ...f.relation, value_column: ($event.target as HTMLSelectElement).value } })"
                              >
                                <option v-for="c in relationDatasetSchema(f.relation.dataset_id)" :key="c.name" :value="c.name">{{ c.name }}</option>
                              </select>
                            </template>
                          </div>
                          <p v-if="f.relation" class="text-[9px] text-zinc-600">
                            Shows <span class="text-zinc-400">{{ f.relation.display_column }}</span>, stores <span class="text-zinc-400">{{ f.relation.value_column || f.relation.display_column }}</span> from {{ datasets.find((d) => d.id === f.relation!.dataset_id)?.name }}
                          </p>
                        </div>
                      </div>
                      <p class="text-[10px] text-zinc-600">options → dropdown in the form (or a checkbox group with multi-select) · default fills empty submissions · required is enforced server-side · linking to a dataset turns it into a live picker of that dataset's rows</p>
                    </div>
                  </template>

                  <!-- kpi editors (v46: stat with the extended aggs) -->
                  <template v-else-if="comp.type === 'kpi'">
                    <input v-model="comp.label" placeholder="Label" class="w-full rounded-lg border border-zinc-800 bg-zinc-900/60 px-2.5 py-1.5 text-xs outline-none focus:border-violet-500/60" :disabled="isPublished" @input="touch" />
                    <div class="flex gap-2">
                      <select v-model="comp.agg" class="flex-1 rounded-lg border border-zinc-800 bg-zinc-900/60 px-2 py-1.5 text-xs outline-none focus:border-violet-500/60" :disabled="isPublished" @change="touch">
                        <option value="count">count</option><option value="count_distinct">count distinct</option><option value="sum">sum</option><option value="avg">avg</option><option value="median">median</option><option value="min">min</option><option value="max">max</option>
                      </select>
                      <select v-if="comp.agg !== 'count'" v-model="comp.column" class="flex-1 rounded-lg border border-zinc-800 bg-zinc-900/60 px-2 py-1.5 text-xs outline-none focus:border-violet-500/60" :disabled="isPublished" @change="touch">
                        <option value="" disabled>column…</option>
                        <option v-for="c in schema" :key="c.name" :value="c.name">{{ c.name }}</option>
                      </select>
                    </div>
                  </template>

                  <!-- markdown editors (v46) -->
                  <template v-else-if="comp.type === 'markdown'">
                    <input v-model="comp.title" placeholder="Title (optional)" class="w-full rounded-lg border border-zinc-800 bg-zinc-900/60 px-2.5 py-1.5 text-xs outline-none focus:border-violet-500/60" :disabled="isPublished" @input="touch" />
                    <textarea
                      v-model="comp.body"
                      rows="5"
                      placeholder="**bold**, *italic*, `code`, [links](https://…)"
                      class="w-full rounded-lg border border-zinc-800 bg-zinc-900/60 px-2.5 py-1.5 font-mono text-[11px] outline-none focus:border-violet-500/60"
                      :disabled="isPublished"
                      @input="touch"
                    />
                    <p class="text-[10px] text-zinc-600">Markdown-lite: **bold** · *italic* · `code` · [text](https://link) · line breaks. HTML is escaped.</p>
                  </template>

                  <!-- filter editors (v46) -->
                  <template v-else-if="comp.type === 'filter'">
                    <input v-model="comp.label" placeholder="Label" class="w-full rounded-lg border border-zinc-800 bg-zinc-900/60 px-2.5 py-1.5 text-xs outline-none focus:border-violet-500/60" :disabled="isPublished" @input="touch" />
                    <select v-model="comp.column" class="w-full rounded-lg border border-zinc-800 bg-zinc-900/60 px-2 py-1.5 text-xs outline-none focus:border-violet-500/60" :disabled="isPublished" @change="touch">
                      <option value="" disabled>filter on column…</option>
                      <option v-for="c in schema" :key="c.name" :value="c.name">{{ c.name }}</option>
                    </select>
                    <label class="flex items-center gap-2 text-[11px] text-zinc-500">
                      <input v-model="comp.multiple" type="checkbox" class="accent-rose-400" :disabled="isPublished" @change="touch" /> allow multiple selections
                    </label>
                  </template>

                  <!-- button editors (v137): link / in-app navigate / fire a webhook -->
                  <template v-else-if="comp.type === 'button'">
                    <input v-model="comp.label" placeholder="Button label" class="w-full rounded-lg border border-zinc-800 bg-zinc-900/60 px-2.5 py-1.5 text-xs outline-none focus:border-violet-500/60" :disabled="isPublished" @input="touch" />
                    <div class="flex gap-2">
                      <select v-model="comp.style" class="flex-1 rounded-lg border border-zinc-800 bg-zinc-900/60 px-2 py-1.5 text-xs outline-none focus:border-violet-500/60" :disabled="isPublished" @change="touch">
                        <option value="primary">primary (accent color)</option>
                        <option value="secondary">secondary</option>
                        <option value="danger">danger</option>
                      </select>
                      <select v-model="comp.action" class="flex-1 rounded-lg border border-zinc-800 bg-zinc-900/60 px-2 py-1.5 text-xs outline-none focus:border-violet-500/60" :disabled="isPublished" @change="touch">
                        <option value="link">open a link</option>
                        <option value="navigate">go to page</option>
                        <option value="webhook">trigger workflow</option>
                      </select>
                    </div>

                    <template v-if="comp.action === 'link'">
                      <input v-model="comp.url" placeholder="https://example.com" class="w-full rounded-lg border border-zinc-800 bg-zinc-900/60 px-2.5 py-1.5 text-xs outline-none focus:border-violet-500/60" :disabled="isPublished" @input="touch" />
                      <label class="flex items-center gap-2 text-[11px] text-zinc-500">
                        <input v-model="comp.new_tab" type="checkbox" class="accent-orange-400" :disabled="isPublished" @change="touch" /> open in a new tab
                      </label>
                    </template>

                    <template v-else-if="comp.action === 'navigate'">
                      <input v-model="comp.target_page" list="app-page-names" placeholder="Page to jump to (e.g. Dashboard)" class="w-full rounded-lg border border-zinc-800 bg-zinc-900/60 px-2.5 py-1.5 text-xs outline-none focus:border-violet-500/60" :disabled="isPublished" @input="touch" />
                    </template>

                    <template v-else-if="comp.action === 'webhook'">
                      <input v-model="comp.webhook_url" placeholder="Webhook URL (from a Webhook trigger node)" class="w-full rounded-lg border border-zinc-800 bg-zinc-900/60 px-2.5 py-1.5 text-xs outline-none focus:border-violet-500/60" :disabled="isPublished" @input="touch" />
                      <div class="flex gap-2">
                        <select v-model="comp.method" class="flex-1 rounded-lg border border-zinc-800 bg-zinc-900/60 px-2 py-1.5 text-xs outline-none focus:border-violet-500/60" :disabled="isPublished" @change="touch">
                          <option value="POST">POST</option>
                          <option value="GET">GET</option>
                        </select>
                        <input v-model="comp.confirm_message" placeholder="Confirm text (optional)" class="flex-1 rounded-lg border border-zinc-800 bg-zinc-900/60 px-2.5 py-1.5 text-xs outline-none focus:border-violet-500/60" :disabled="isPublished" @input="touch" />
                      </div>
                      <p class="text-[10px] text-zinc-600">Paste the URL from a Webhook trigger node's workflow - viewers click the button, the workflow fires, and the button shows success/failure.</p>
                    </template>
                  </template>

                  <!-- chart editors (v138: full ECharts type list) -->
                  <template v-else-if="comp.type === 'chart'">
                    <input v-model="comp.title" placeholder="Title" class="w-full rounded-lg border border-zinc-800 bg-zinc-900/60 px-2.5 py-1.5 text-xs outline-none focus:border-violet-500/60" :disabled="isPublished" @input="touch" />
                    <div class="flex gap-2">
                      <select v-model="comp.chart_type" class="flex-1 rounded-lg border border-zinc-800 bg-zinc-900/60 px-2 py-1.5 text-xs outline-none focus:border-violet-500/60" :disabled="isPublished" @change="touch">
                        <option value="bar">bar</option><option value="line">line</option><option value="area">area</option><option value="pie">pie</option><option value="donut">donut</option><option value="scatter">scatter</option>
                        <option value="funnel">funnel</option><option value="treemap">treemap</option><option value="gauge">gauge</option><option value="heatmap">heatmap</option><option value="radar">radar</option>
                      </select>
                      <select v-if="GROUPED_CHART_TYPES.includes(comp.chart_type)" v-model="comp.group_by" class="flex-1 rounded-lg border border-zinc-800 bg-zinc-900/60 px-2 py-1.5 text-xs outline-none focus:border-violet-500/60" :disabled="isPublished" @change="touch">
                        <option value="" disabled>group by…</option>
                        <option v-for="c in schema" :key="c.name" :value="c.name">{{ c.name }}</option>
                      </select>
                    </div>

                    <!-- scatter: x/y columns -->
                    <div v-if="comp.chart_type === 'scatter'" class="flex gap-2">
                      <select v-model="comp.x" class="flex-1 rounded-lg border border-zinc-800 bg-zinc-900/60 px-2 py-1.5 text-xs outline-none focus:border-violet-500/60" :disabled="isPublished" @change="touch">
                        <option value="" disabled>x column…</option>
                        <option v-for="c in schema" :key="c.name" :value="c.name">{{ c.name }}</option>
                      </select>
                      <select v-model="comp.y" class="flex-1 rounded-lg border border-zinc-800 bg-zinc-900/60 px-2 py-1.5 text-xs outline-none focus:border-violet-500/60" :disabled="isPublished" @change="touch">
                        <option value="" disabled>y (numeric)…</option>
                        <option v-for="c in schema.filter((c) => c.dtype === 'integer' || c.dtype === 'number')" :key="c.name" :value="c.name">{{ c.name }}</option>
                      </select>
                    </div>

                    <!-- heatmap: row + col dimensions -->
                    <div v-if="comp.chart_type === 'heatmap'" class="flex gap-2">
                      <select v-model="comp.row" class="flex-1 rounded-lg border border-zinc-800 bg-zinc-900/60 px-2 py-1.5 text-xs outline-none focus:border-violet-500/60" :disabled="isPublished" @change="touch">
                        <option value="" disabled>row column…</option>
                        <option v-for="c in schema" :key="c.name" :value="c.name">{{ c.name }}</option>
                      </select>
                      <select v-model="comp.col" class="flex-1 rounded-lg border border-zinc-800 bg-zinc-900/60 px-2 py-1.5 text-xs outline-none focus:border-violet-500/60" :disabled="isPublished" @change="touch">
                        <option value="" disabled>col column…</option>
                        <option v-for="c in schema" :key="c.name" :value="c.name">{{ c.name }}</option>
                      </select>
                    </div>

                    <!-- radar: pick 3+ numeric metrics as the axes -->
                    <div v-if="comp.chart_type === 'radar'" class="space-y-1 rounded-lg border border-zinc-800 bg-zinc-900/40 p-2">
                      <p class="text-[10px] text-zinc-500">metrics (pick 3 or more numeric columns)</p>
                      <label v-for="c in schema.filter((c) => c.dtype === 'integer' || c.dtype === 'number')" :key="c.name" class="flex cursor-pointer items-center gap-1.5 text-[11px] text-zinc-300">
                        <input
                          type="checkbox"
                          class="accent-violet-500"
                          :checked="(comp.metrics || []).includes(c.name)"
                          :disabled="isPublished"
                          @change="toggleRadarMetric(comp, c.name, ($event.target as HTMLInputElement).checked)"
                        />
                        {{ c.name }}
                      </label>
                    </div>

                    <!-- gauge: single aggregated value + optional max -->
                    <div v-if="comp.chart_type === 'gauge'" class="flex gap-2">
                      <select v-model="comp.agg" class="flex-1 rounded-lg border border-zinc-800 bg-zinc-900/60 px-2 py-1.5 text-xs outline-none focus:border-violet-500/60" :disabled="isPublished" @change="touch">
                        <option value="count">count</option><option value="count_distinct">count distinct</option><option value="sum">sum</option><option value="avg">avg</option><option value="median">median</option><option value="min">min</option><option value="max">max</option>
                      </select>
                      <select v-if="comp.agg !== 'count'" v-model="comp.column" class="flex-1 rounded-lg border border-zinc-800 bg-zinc-900/60 px-2 py-1.5 text-xs outline-none focus:border-violet-500/60" :disabled="isPublished" @change="touch">
                        <option value="" disabled>column…</option>
                        <option v-for="c in schema" :key="c.name" :value="c.name">{{ c.name }}</option>
                      </select>
                      <input v-model.number="comp.max" type="number" placeholder="max (auto)" class="w-24 rounded-lg border border-zinc-800 bg-zinc-900/60 px-2 py-1.5 text-xs outline-none focus:border-violet-500/60" :disabled="isPublished" @input="touch" />
                    </div>

                    <!-- group_by charts + heatmap + radar all need an agg/column pair -->
                    <div v-if="GROUPED_CHART_TYPES.includes(comp.chart_type) || comp.chart_type === 'heatmap'" class="flex gap-2">
                      <select v-model="comp.agg" class="flex-1 rounded-lg border border-zinc-800 bg-zinc-900/60 px-2 py-1.5 text-xs outline-none focus:border-violet-500/60" :disabled="isPublished" @change="touch">
                        <option value="count">count</option><option value="count_distinct">count distinct</option><option value="sum">sum</option><option value="avg">avg</option><option value="median">median</option><option value="min">min</option><option value="max">max</option>
                      </select>
                      <select v-if="comp.agg !== 'count'" v-model="comp.column" class="flex-1 rounded-lg border border-zinc-800 bg-zinc-900/60 px-2 py-1.5 text-xs outline-none focus:border-violet-500/60" :disabled="isPublished" @change="touch">
                        <option value="" disabled>column…</option>
                        <option v-for="c in schema" :key="c.name" :value="c.name">{{ c.name }}</option>
                      </select>
                    </div>
                    <div v-if="comp.chart_type === 'radar'" class="flex gap-2">
                      <select v-model="comp.agg" class="flex-1 rounded-lg border border-zinc-800 bg-zinc-900/60 px-2 py-1.5 text-xs outline-none focus:border-violet-500/60" :disabled="isPublished" @change="touch">
                        <option value="avg">avg</option><option value="sum">sum</option><option value="median">median</option><option value="min">min</option><option value="max">max</option>
                      </select>
                    </div>
                  </template>
                </div>
              </div>
            </div>

            <div v-if="!isPublished" class="mt-3 grid grid-cols-4 gap-1.5">
              <button v-for="t in (['stat', 'kpi', 'chart', 'table', 'form', 'markdown', 'filter', 'button'] as const)" :key="t"
                class="flex flex-col items-center gap-1 rounded-xl border border-dashed border-zinc-800 py-2 text-[10px] text-zinc-500 transition hover:border-violet-500/50 hover:text-violet-300"
                @click="addComponent(t)"
              >
                <component :is="TYPE_ICONS[t]" class="h-3.5 w-3.5" /> + {{ t }}
              </button>
            </div>
          </section>

          <!-- business rules (v30) -->
          <section class="rounded-2xl border border-zinc-800/80 bg-zinc-900/40 p-4">
            <div class="flex items-center justify-between">
              <h2 class="flex items-center gap-2 text-xs font-bold uppercase tracking-wide text-zinc-400">
                <ShieldCheck class="h-3.5 w-3.5 text-emerald-400" /> Rules ({{ rules.length }})
              </h2>
              <button
                v-if="bindId"
                class="flex items-center gap-1 rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-2 py-1 text-[10px] font-semibold text-emerald-400 transition hover:bg-emerald-500/20 disabled:opacity-40"
                :disabled="rulesSaving"
                @click="saveRules"
              >
                <Loader2 v-if="rulesSaving" class="h-3 w-3 animate-spin" />
                <Save v-else class="h-3 w-3" />
                {{ rulesDirty ? 'Save rules*' : 'Save rules' }}
              </button>
            </div>
            <p class="mt-1 text-[11px] leading-relaxed text-zinc-500">
              Server-side guards on every record - block rejects, warn flags, set computes. Rules stay editable while the app is published.
            </p>

            <div class="mt-3 space-y-3">
              <div
                v-for="(rule, ri) in rules"
                :key="rule.id || ri"
                class="rounded-xl border border-zinc-800 bg-zinc-950/50 p-3"
              >
                <div class="flex items-center gap-2">
                  <span class="rounded-md px-1.5 py-0.5 text-[9px] font-bold uppercase" :class="ACTION_COLORS[rule.action] || 'bg-zinc-800 text-zinc-400'">{{ rule.action }}</span>
                  <input
                    v-model="rule.name"
                    placeholder="Rule name"
                    class="min-w-0 flex-1 rounded-lg border border-transparent bg-transparent px-1 py-0.5 text-xs font-semibold text-zinc-200 outline-none transition hover:border-zinc-700 focus:border-violet-500/60"
                    @input="rulesDirty = true"
                  />
                  <button class="rounded p-1 text-zinc-600 transition hover:bg-red-500/10 hover:text-red-400" title="Remove rule" @click="removeRule(ri)">
                    <Trash2 class="h-3.5 w-3.5" />
                  </button>
                </div>

                <div class="mt-2 flex gap-2">
                  <select v-model="rule.event" class="flex-1 rounded-lg border border-zinc-800 bg-zinc-900/60 px-2 py-1.5 text-[11px] outline-none focus:border-violet-500/60" @change="rulesDirty = true">
                    <option value="create">on create</option>
                    <option value="update">on update</option>
                    <option value="always">always</option>
                  </select>
                  <select v-model="rule.action" class="flex-1 rounded-lg border border-zinc-800 bg-zinc-900/60 px-2 py-1.5 text-[11px] outline-none focus:border-violet-500/60" @change="rulesDirty = true">
                    <option value="block">block (reject)</option>
                    <option value="warn">warn (flag)</option>
                    <option value="set">set (compute)</option>
                  </select>
                </div>

                <!-- when clauses -->
                <div class="mt-2 space-y-1.5">
                  <p class="text-[10px] font-semibold uppercase tracking-wide text-zinc-600">When</p>
                  <div v-for="(clause, ci) in rule.when?.all || []" :key="ci" class="flex items-center gap-1.5">
                    <select v-model="clause.field" class="min-w-0 flex-1 rounded-lg border border-zinc-800 bg-zinc-900/60 px-1.5 py-1 text-[11px] outline-none focus:border-violet-500/60" @change="rulesDirty = true">
                      <option value="" disabled>field…</option>
                      <option v-for="c in schema" :key="c.name" :value="c.name">{{ c.name }}</option>
                    </select>
                    <select v-model="clause.op" class="w-24 shrink-0 rounded-lg border border-zinc-800 bg-zinc-900/60 px-1.5 py-1 text-[11px] outline-none focus:border-violet-500/60" @change="rulesDirty = true">
                      <option v-for="op in RULE_OPS" :key="op" :value="op">{{ op }}</option>
                    </select>
                    <input
                      v-if="!VALUELESS_OPS.has(clause.op)"
                      v-model="clause.value"
                      placeholder="value"
                      class="min-w-0 flex-1 rounded-lg border border-zinc-800 bg-zinc-900/60 px-2 py-1 text-[11px] outline-none focus:border-violet-500/60"
                      @input="rulesDirty = true"
                    />
                    <button class="shrink-0 rounded p-1 text-zinc-600 transition hover:bg-red-500/10 hover:text-red-400" title="Remove condition" @click="rule.when!.all.splice(ci, 1); rulesDirty = true">
                      <XCircle class="h-3.5 w-3.5" />
                    </button>
                  </div>
                  <button class="flex items-center gap-1 text-[10px] text-zinc-500 transition hover:text-violet-300" @click="addClause(rule)">
                    <PlusCircle class="h-3 w-3" /> add condition
                  </button>
                </div>

                <!-- then -->
                <div class="mt-2 space-y-1.5">
                  <p class="text-[10px] font-semibold uppercase tracking-wide text-zinc-600">Then</p>
                  <textarea
                    v-if="rule.action === 'block' || rule.action === 'warn'"
                    v-model="rule.message"
                    rows="2"
                    :placeholder="rule.action === 'block' ? 'Rejection message shown to the user' : 'Warning message attached to the record'"
                    class="w-full rounded-lg border border-zinc-800 bg-zinc-900/60 px-2.5 py-1.5 text-[11px] outline-none focus:border-violet-500/60"
                    @input="rulesDirty = true"
                  />
                  <template v-else>
                    <div class="flex gap-2">
                      <select v-model="rule.field" class="flex-1 rounded-lg border border-zinc-800 bg-zinc-900/60 px-2 py-1.5 text-[11px] outline-none focus:border-violet-500/60" @change="rulesDirty = true">
                        <option value="" disabled>field to set…</option>
                        <option v-for="c in schema" :key="c.name" :value="c.name">{{ c.name }}</option>
                      </select>
                      <select
                        :value="rule.formula ? 'formula' : 'value'"
                        class="w-24 shrink-0 rounded-lg border border-zinc-800 bg-zinc-900/60 px-1.5 py-1.5 text-[11px] outline-none focus:border-violet-500/60"
                        @change="($event.target as HTMLSelectElement).value === 'formula' ? (rule.formula = rule.formula || '', rule.value = undefined) : (rule.value = rule.value ?? '', rule.formula = undefined); rulesDirty = true"
                      >
                        <option value="value">constant</option>
                        <option value="formula">formula</option>
                      </select>
                    </div>
                    <input
                      v-if="rule.formula !== undefined"
                      v-model="rule.formula"
                      placeholder="formula e.g. ltv * 0.1"
                      class="w-full rounded-lg border border-zinc-800 bg-zinc-900/60 px-2.5 py-1.5 font-mono text-[11px] outline-none focus:border-violet-500/60"
                      @input="rulesDirty = true"
                    />
                    <input
                      v-else
                      v-model="rule.value"
                      placeholder="constant value"
                      class="w-full rounded-lg border border-zinc-800 bg-zinc-900/60 px-2.5 py-1.5 text-[11px] outline-none focus:border-violet-500/60"
                      @input="rulesDirty = true"
                    />
                  </template>
                </div>

                <p class="mt-2 truncate text-[10px] text-zinc-600">when {{ ruleSummary(rule) }}</p>
              </div>
            </div>

            <button
              v-if="bindId"
              class="mt-3 flex w-full items-center justify-center gap-1.5 rounded-xl border border-dashed border-zinc-800 py-2 text-[11px] text-zinc-500 transition hover:border-emerald-500/50 hover:text-emerald-300"
              @click="addRule"
            >
              <Plus class="h-3.5 w-3.5" /> add rule
            </button>
            <p v-else class="mt-3 text-[11px] text-zinc-600">Bind a dataset to add rules.</p>
          </section>
        </div>

        <!-- ------------------------------ right: live preview -->
        <div class="rounded-2xl border border-zinc-800/80 bg-zinc-900/40 p-4">
          <div class="flex items-center justify-between">
            <h2 class="text-xs font-bold uppercase tracking-wide text-zinc-400">Live preview</h2>
            <span class="text-[10px] text-zinc-600">{{ rows.length }} rows loaded{{ rows.length >= 1000 ? ' (capped)' : '' }}</span>
          </div>

          <div v-if="!bindId" class="mt-8 text-center text-sm text-zinc-500">
            <Database class="mx-auto h-8 w-8 text-zinc-700" />
            <p class="mt-2">Bind a dataset to see the live preview.</p>
          </div>
          <template v-else>
            <!-- v46: server-rendered preview - same compute path as the runtime -->
            <div class="mt-3 flex items-center justify-between">
              <span class="text-[10px] text-zinc-600">computed server-side (zero drift with the published app)</span>
              <button
                class="flex items-center gap-1 rounded-lg border border-zinc-800 bg-zinc-900/60 px-2 py-1 text-[10px] text-zinc-400 transition hover:border-violet-500/40 hover:text-violet-300"
                :disabled="previewLoading"
                @click="loadPreview"
              >
                <Loader2 v-if="previewLoading" class="h-3 w-3 animate-spin" />
                <RefreshCw v-else class="h-3 w-3" /> Refresh
              </button>
            </div>
            <p v-if="previewError" class="mt-2 rounded-lg border border-amber-500/30 bg-amber-500/10 px-2.5 py-1.5 text-[11px] text-amber-300">{{ previewError }}</p>

            <!-- v139: 12-col grid - each component claims full/half/third/two-thirds width -->
            <div class="mt-3 grid grid-cols-12" :class="gapClass()">
            <template v-for="c in previewComps" :key="c.id">
              <!-- stat / kpi -->
              <div v-if="c.type === 'stat' || c.type === 'kpi'" class="border border-zinc-800/80 bg-zinc-950/60" :class="[surfaceClass(), widthClass(c)]">
                <p class="text-[11px] uppercase tracking-wide text-zinc-500">{{ c.label || c.id }}</p>
                <p class="mt-1 text-2xl font-bold text-zinc-100">{{ c.value === null || c.value === undefined ? '-' : c.value }}</p>
              </div>

              <!-- markdown -->
              <div v-else-if="c.type === 'markdown'" class="border border-zinc-800/80 bg-zinc-950/60" :class="[surfaceClass(), widthClass(c)]">
                <p v-if="c.title" class="text-xs font-semibold text-zinc-300">{{ c.title }}</p>
                <!-- body is HTML-escaped server-side before markdown transforms -->
                <div class="mt-1 text-xs leading-relaxed text-zinc-400" v-html="c.html" />
              </div>

              <!-- filter -->
              <div v-else-if="c.type === 'filter'" class="border border-zinc-800/80 bg-zinc-950/60" :class="[surfaceClass(), widthClass(c)]">
                <p class="text-[11px] uppercase tracking-wide text-zinc-500">{{ c.label }} <span class="ml-1 text-zinc-600">· on {{ c.column }} (filters at runtime)</span></p>
                <div class="mt-2 flex flex-wrap gap-1">
                  <span v-for="o in c.options.slice(0, 12)" :key="o" class="rounded bg-zinc-800/80 px-1.5 py-0.5 text-[10px] text-zinc-400">{{ o }}</span>
                  <span v-if="c.options.length > 12" class="text-[10px] text-zinc-600">+{{ c.options.length - 12 }} more</span>
                </div>
              </div>

              <!-- button (v139: preview only - not clickable here) -->
              <div v-else-if="c.type === 'button'" class="flex items-center" :class="widthClass(c)">
                <span
                  class="inline-flex items-center gap-1.5 rounded-lg px-4 py-2 text-sm font-semibold"
                  :class="c.style === 'secondary' ? 'border border-zinc-700 text-zinc-200' : c.style === 'danger' ? 'bg-red-500/90 text-white' : 'bg-violet-500 text-white'"
                >{{ c.label }}</span>
              </div>

              <!-- chart (v138: same ECharts renderer as the published runtime - zero drift) -->
              <div v-else-if="c.type === 'chart'" class="border border-zinc-800/80 bg-zinc-950/60" :class="[surfaceClass(), widthClass(c)]">
                <div class="flex items-center justify-between">
                  <p class="text-xs font-semibold text-zinc-300">{{ c.title || 'Chart' }}</p>
                  <span class="rounded-full bg-zinc-800 px-2 py-0.5 text-[10px] uppercase text-zinc-400">{{ c.chart_type }}</span>
                </div>
                <p v-if="chartIsEmpty(c)" class="mt-2 text-[11px] text-zinc-600">No data to chart yet.</p>
                <ClientOnly v-else>
                  <EChart :option="chartOption(c)" :height="c.chart_type === 'gauge' ? 200 : 240" class="mt-2" />
                </ClientOnly>
              </div>

              <!-- table -->
              <div v-else-if="c.type === 'table'" class="overflow-hidden border border-zinc-800/80 bg-zinc-950/60" :class="[radiusClass(), widthClass(c)]">
                <div class="flex items-center justify-between border-b border-zinc-800/80 px-4 py-2.5">
                  <p class="text-xs font-semibold text-zinc-300">{{ c.title || 'Records' }}</p>
                  <span class="text-[10px] text-zinc-600">{{ c.row_count }} of {{ c.total }}</span>
                </div>
                <div v-if="c.rows.length" class="overflow-x-auto">
                  <table class="w-full text-left text-xs">
                    <thead>
                      <tr class="border-b border-zinc-800/60 text-[10px] uppercase text-zinc-500">
                        <th v-for="col in c.columns" :key="col" class="px-4 py-2 font-medium">{{ col }}</th>
                      </tr>
                    </thead>
                    <tbody>
                      <tr v-for="(row, ri) in c.rows" :key="ri" class="border-b border-zinc-900 text-zinc-300 last:border-0">
                        <td v-for="col in c.columns" :key="col" class="max-w-[220px] truncate px-4 py-2">{{ row[col] ?? '-' }}</td>
                      </tr>
                    </tbody>
                  </table>
                </div>
                <p v-else class="px-4 py-4 text-[11px] text-zinc-600">No records yet.</p>
              </div>

              <!-- form -->
              <div v-else-if="c.type === 'form'" class="border border-zinc-800/80 bg-zinc-950/60" :class="[surfaceClass(), widthClass(c)]">
                <p class="text-xs font-semibold text-zinc-300">{{ c.title || 'Add record' }}</p>
                <div class="mt-3 grid gap-2 sm:grid-cols-2">
                  <div v-for="f in c.fields" :key="f.name">
                    <label class="text-[10px] uppercase tracking-wide text-zinc-500">{{ f.label || f.name }}</label>
                    <div class="mt-1 flex items-center gap-1 rounded-lg border border-zinc-800 bg-zinc-900/40 px-2.5 py-1.5 text-xs text-zinc-600">
                      <Link2 v-if="f.relation" class="h-3 w-3 shrink-0 text-violet-400" />
                      <span v-if="f.relation">linked → {{ datasets.find((d) => d.id === f.relation.dataset_id)?.name || 'dataset' }} ({{ (f.relation_options || []).length }})</span>
                      <span v-else>{{ dtypeOf(f.name) === 'boolean' ? 'true / false' : dtypeOf(f.name) === 'integer' || dtypeOf(f.name) === 'number' ? 'number input' : 'text input' }}</span>
                    </div>
                  </div>
                </div>
                <span class="mt-3 inline-block rounded-lg bg-violet-500/20 px-3 py-1.5 text-xs font-medium text-violet-300">{{ c.submit_label || 'Create' }} →</span>
              </div>
            </template>
            </div>
          </template>
        </div>
      </div>
    </div>

    <!-- v47 share dialog -->
    <Teleport to="body">
      <div v-if="shareOpen" class="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4 backdrop-blur-sm" @click.self="shareOpen = false">
        <div class="w-full max-w-md rounded-2xl border border-zinc-800 bg-zinc-900 p-5 shadow-2xl">
          <div class="flex items-center justify-between">
            <h2 class="flex items-center gap-2 text-sm font-bold"><Share2 class="h-4 w-4 text-violet-400" /> Share app</h2>
            <button class="rounded-lg p-1 text-zinc-500 hover:text-zinc-200" @click="shareOpen = false"><X class="h-4 w-4" /></button>
          </div>

          <div class="mt-3 flex items-center gap-2">
            <span
              class="rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase"
              :class="shareProtected ? 'bg-emerald-500/15 text-emerald-400' : 'bg-zinc-800 text-zinc-400'"
            >{{ shareProtected ? 'share protection on' : 'share protection off' }}</span>
            <span class="text-[11px] text-zinc-500">{{ shareProtected ? 'runtime links need the token (?t=…)' : 'runtime is open to anyone with the link' }}</span>
          </div>

          <button
            class="mt-3 flex w-full items-center justify-center gap-1.5 rounded-xl py-2 text-xs font-semibold text-white transition disabled:opacity-40"
            :class="shareProtected ? 'bg-zinc-700 hover:bg-zinc-600' : 'bg-emerald-500 shadow-lg shadow-emerald-500/20 hover:bg-emerald-400'"
            :disabled="shareBusy"
            @click="toggleShare"
          >
            <Loader2 v-if="shareBusy" class="h-3.5 w-3.5 animate-spin" />
            {{ shareProtected ? 'Disable share link' : 'Enable share link' }}
          </button>

          <template v-if="shareProtected">
            <label class="mt-3 block text-[10px] font-bold uppercase tracking-wide text-zinc-500">Share URL</label>
            <div class="mt-1 flex gap-2">
              <input
                readonly
                :value="shareUrl()"
                class="min-w-0 flex-1 rounded-xl border border-zinc-800 bg-zinc-950/60 px-3 py-2 font-mono text-[11px] text-zinc-300 outline-none"
                @focus="($event.target as HTMLInputElement).select()"
              />
              <button
                class="flex shrink-0 items-center gap-1.5 rounded-xl border border-zinc-800 bg-zinc-950/60 px-3 py-2 text-xs font-medium text-zinc-300 transition hover:border-violet-500/40 hover:text-violet-300"
                @click="copyShare"
              >
                <Check v-if="shareCopied" class="h-3.5 w-3.5 text-emerald-400" />
                <Copy v-else class="h-3.5 w-3.5" />
                {{ shareCopied ? 'Copied' : 'Copy' }}
              </button>
            </div>
          </template>
          <p v-else-if="!isPublished" class="mt-2 text-[11px] text-zinc-600">Publish the app to open /run/{{ appRow?.slug }}.</p>

          <p class="mt-3 text-[11px] leading-relaxed text-zinc-500">Regenerating (disable + enable) revokes old links.</p>

          <!-- v48: row-level share grants -->
          <div class="mt-4 border-t border-zinc-800 pt-3">
            <div class="flex items-center gap-2">
              <KeyRound class="h-3.5 w-3.5 text-amber-400" />
              <span class="text-[10px] font-bold uppercase tracking-wide text-zinc-400">Row-level grants</span>
            </div>
            <p class="mt-1 text-[11px] leading-relaxed text-zinc-500">
              One door per viewer: a grant link shows only the rows matching its filter.
              <span class="text-zinc-400">= eq</span> viewers can also add rows (the scope value is stamped on them);
              <span class="text-zinc-400">in / !=</span> grants are read-only.
            </p>

            <div class="mt-2 space-y-1.5">
              <div v-if="grantsLoading" class="grid place-items-center py-3 text-zinc-600"><Loader2 class="h-4 w-4 animate-spin" /></div>
              <div
                v-for="g in grants"
                :key="g.id"
                class="flex items-center gap-2 rounded-xl border border-zinc-800 bg-zinc-950/60 px-2.5 py-2"
              >
                <div class="min-w-0 flex-1">
                  <div class="flex items-center gap-1.5">
                    <span class="truncate text-xs font-semibold">{{ g.name }}</span>
                    <span
                      class="rounded px-1 py-0.5 text-[9px] font-bold uppercase"
                      :class="g.enabled ? 'bg-emerald-500/15 text-emerald-400' : 'bg-zinc-800 text-zinc-500'"
                    >{{ g.enabled ? 'on' : 'off' }}</span>
                  </div>
                  <div class="truncate font-mono text-[10px] text-zinc-500">{{ grantSummary(g) }}</div>
                  <!-- v49: audit aggregates -->
                  <div v-if="(g.access_count || 0) > 0" class="text-[10px] text-zinc-500">
                    <span class="text-emerald-400/80">{{ g.access_count }} access{{ (g.access_count || 0) === 1 ? '' : 'es' }}</span>
                    <span v-if="g.last_access_at"> · last {{ timeAgo(g.last_access_at) }}</span>
                  </div>
                </div>
                <button
                  class="shrink-0 rounded-lg p-1.5 text-zinc-400 transition hover:border-zinc-600 hover:text-zinc-100"
                  title="Copy grant link"
                  @click="copyGrantUrl(g)"
                >
                  <Check v-if="grantCopied === g.id" class="h-3.5 w-3.5 text-emerald-400" />
                  <Copy v-else class="h-3.5 w-3.5" />
                </button>
                <button
                  class="shrink-0 rounded-lg p-1.5 text-zinc-400 transition hover:text-zinc-100"
                  :title="g.enabled ? 'Disable grant' : 'Enable grant'"
                  @click="toggleGrant(g)"
                >
                  <Power class="h-3.5 w-3.5" :class="g.enabled ? 'text-emerald-400' : ''" />
                </button>
                <button
                  class="shrink-0 rounded-lg p-1.5 text-zinc-400 transition hover:text-rose-300"
                  title="Revoke grant"
                  @click="revokeGrant(g)"
                >
                  <Trash2 class="h-3.5 w-3.5" />
                </button>
              </div>
              <p v-if="!grantsLoading && !grants.length" class="text-[11px] text-zinc-600">No grants yet - the full share link above opens every row.</p>
            </div>

            <!-- v49: share-surface audit trail -->
            <div v-if="grantEvents.length" class="mt-2">
              <button
                class="flex w-full items-center gap-1.5 text-[10px] font-bold uppercase tracking-wide text-zinc-500 transition hover:text-zinc-300"
                @click="showGrantActivity = !showGrantActivity"
              >
                <History class="h-3 w-3" />
                Recent grant activity ({{ grantEvents.length }})
                <ChevronDown class="h-3 w-3 transition-transform" :class="showGrantActivity && 'rotate-180'" />
              </button>
              <div v-if="showGrantActivity" class="mt-1.5 space-y-1">
                <div
                  v-for="ev in grantEvents"
                  :key="ev.id"
                  class="flex items-center gap-1.5 rounded-lg border border-zinc-800/60 bg-zinc-950/40 px-2 py-1"
                >
                  <span
                    class="h-1.5 w-1.5 shrink-0 rounded-full"
                    :class="ev.outcome === 'allowed' ? 'bg-emerald-400' : 'bg-rose-400'"
                    :title="ev.outcome"
                  />
                  <span class="min-w-0 flex-1 truncate text-[10px] text-zinc-400">
                    {{ eventLabel(ev) }}
                    <span v-if="ev.outcome === 'denied' && ev.detail" class="text-rose-300/70">({{ ev.detail }})</span>
                  </span>
                  <span class="shrink-0 text-[9px] text-zinc-600">{{ timeAgo(ev.created_at) }}</span>
                </div>
              </div>
            </div>

            <div class="mt-2 grid grid-cols-2 gap-1.5">
              <input
                v-model="grantName"
                placeholder="Name, e.g. EU team"
                class="col-span-2 rounded-xl border border-zinc-800 bg-zinc-950/60 px-2.5 py-1.5 text-xs outline-none placeholder:text-zinc-600 focus:border-amber-500/50"
              />
              <select
                v-model="grantColumn"
                class="rounded-xl border border-zinc-800 bg-zinc-950/60 px-2 py-1.5 text-xs outline-none focus:border-amber-500/50"
              >
                <option value="" disabled>Column</option>
                <option v-for="c in schemaColumns" :key="c" :value="c">{{ c }}</option>
              </select>
              <select
                v-model="grantOp"
                class="rounded-xl border border-zinc-800 bg-zinc-950/60 px-2 py-1.5 text-xs outline-none focus:border-amber-500/50"
              >
                <option value="eq">= (can write)</option>
                <option value="in">in list (read-only)</option>
                <option value="neq">≠ (read-only)</option>
              </select>
              <input
                v-model="grantValue"
                :placeholder="grantOp === 'in' ? 'eu, us' : 'eu'"
                class="col-span-2 rounded-xl border border-zinc-800 bg-zinc-950/60 px-2.5 py-1.5 text-xs outline-none placeholder:text-zinc-600 focus:border-amber-500/50"
                @keydown.enter="createGrant"
              />
              <button
                class="col-span-2 flex items-center justify-center gap-1.5 rounded-xl bg-amber-500/90 py-1.5 text-xs font-semibold text-black transition hover:bg-amber-400 disabled:opacity-40"
                :disabled="grantBusy || !grantName.trim() || !grantColumn || !grantValue.trim()"
                @click="createGrant"
              >
                <Loader2 v-if="grantBusy" class="h-3.5 w-3.5 animate-spin" />
                <Plus v-else class="h-3.5 w-3.5" />
                Create grant link
              </button>
            </div>
            <p v-if="grantError" class="mt-1.5 rounded-lg border border-rose-500/30 bg-rose-500/10 px-2 py-1 text-[11px] text-rose-300">{{ grantError }}</p>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

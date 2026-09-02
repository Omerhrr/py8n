<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue'
import {
  Loader2, Save, Rocket, ExternalLink, Database, Plus, Trash2, X, RefreshCw,
  Gauge, Table2, ClipboardList, BarChart3, ArrowLeft, Unlink, CircleAlert,
  ShieldCheck, Link2, TriangleAlert, PlusCircle, XCircle, TrendingUp, Filter,
  Share2, Copy, Check, KeyRound, Power,
} from 'lucide-vue-next'
import { useApi } from '~/composables/useApi'

const { api } = useApi()
const route = useRoute()

interface FormField {
  name: string
  label?: string | null
  required?: boolean
  options?: (string | number | boolean)[] | null
  default?: string | number | boolean | null
  placeholder?: string | null
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
  type: 'stat' | 'table' | 'form' | 'chart' | 'kpi' | 'markdown' | 'filter'
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
}

interface AppDetail {
  id: string
  name: string
  slug: string
  description: string
  dataset_id: string | null
  dataset_name: string | null
  config: { components?: AppComponent[]; rules?: AppRule[] }
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

// Current editor config - components are edited in place on appRow, rules
// live in the separate `rules` ref; the server stores both in one config.
function currentConfig() {
  return {
    components: appRow.value?.config?.components ?? [],
    rules: rules.value,
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
const TYPE_ICONS: Record<string, any> = { stat: Gauge, table: Table2, form: ClipboardList, chart: BarChart3, kpi: TrendingUp, markdown: FileText, filter: Filter }
const TYPE_COLORS: Record<string, string> = {
  stat: 'bg-sky-500/15 text-sky-400',
  table: 'bg-lime-500/15 text-lime-400',
  form: 'bg-amber-500/15 text-amber-400',
  chart: 'bg-violet-500/15 text-violet-400',
  kpi: 'bg-cyan-500/15 text-cyan-400',
  markdown: 'bg-zinc-500/15 text-zinc-300',
  filter: 'bg-rose-500/15 text-rose-400',
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
  } else {
    comps2.push({ id: genId('chart'), type, title: 'Breakdown', chart_type: 'bar', group_by: text[0] || cols[0], agg: 'count' })
  }
  touch()
}

function removeComponent(i: number) {
  if (!appRow.value) return
  appRow.value.config.components?.splice(i, 1)
  touch()
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
}

const grants = ref<ShareGrant[]>([])
const grantsLoading = ref(false)
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
    grants.value = await api.get<ShareGrant[]>(`/apps/${appRow.value.id}/grants`)
  } catch {
    grants.value = []
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

          <!-- components -->
          <section class="rounded-2xl border border-zinc-800/80 bg-zinc-900/40 p-4">
            <div class="flex items-center justify-between">
              <h2 class="text-xs font-bold uppercase tracking-wide text-zinc-400">Components ({{ comps.length }})</h2>
            </div>
            <div v-if="isPublished" class="mt-2 rounded-lg bg-amber-500/10 px-3 py-2 text-[11px] text-amber-300">
              Published apps are locked - unpublish to edit components.
            </div>

            <div class="mt-3 space-y-3">
              <div
                v-for="(comp, i) in comps"
                :key="comp.id"
                class="rounded-xl border border-zinc-800 bg-zinc-950/50 p-3"
              >
                <div class="flex items-center gap-2">
                  <span class="flex h-6 w-6 items-center justify-center rounded-lg" :class="TYPE_COLORS[comp.type]">
                    <component :is="TYPE_ICONS[comp.type]" class="h-3 w-3" />
                  </span>
                  <span class="flex-1 truncate text-xs font-semibold text-zinc-300">{{ comp.title || comp.label || comp.id }}</span>
                  <button class="rounded p-1 text-zinc-600 transition hover:bg-amber-500/10 hover:text-amber-400 disabled:opacity-30" :disabled="isPublished" @click="removeComponent(i)">
                    <Trash2 class="h-3.5 w-3.5" />
                  </button>
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
                          <label class="ml-auto flex cursor-pointer items-center gap-1 text-[10px] text-zinc-400">
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
                      </div>
                      <p class="text-[10px] text-zinc-600">options → dropdown in the form · default fills empty submissions · required is enforced server-side</p>
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

                  <!-- chart editors -->
                  <template v-else-if="comp.type === 'chart'">
                    <input v-model="comp.title" placeholder="Title" class="w-full rounded-lg border border-zinc-800 bg-zinc-900/60 px-2.5 py-1.5 text-xs outline-none focus:border-violet-500/60" :disabled="isPublished" @input="touch" />
                    <div class="flex gap-2">
                      <select v-model="comp.chart_type" class="flex-1 rounded-lg border border-zinc-800 bg-zinc-900/60 px-2 py-1.5 text-xs outline-none focus:border-violet-500/60" :disabled="isPublished" @change="touch">
                        <option value="bar">bar</option><option value="line">line</option><option value="area">area</option><option value="pie">pie</option><option value="donut">donut</option><option value="scatter">scatter</option>
                      </select>
                      <select v-if="comp.chart_type !== 'scatter'" v-model="comp.group_by" class="flex-1 rounded-lg border border-zinc-800 bg-zinc-900/60 px-2 py-1.5 text-xs outline-none focus:border-violet-500/60" :disabled="isPublished" @change="touch">
                        <option value="" disabled>group by…</option>
                        <option v-for="c in schema" :key="c.name" :value="c.name">{{ c.name }}</option>
                      </select>
                    </div>
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
                    <div v-if="comp.chart_type !== 'scatter'" class="flex gap-2">
                      <select v-model="comp.agg" class="flex-1 rounded-lg border border-zinc-800 bg-zinc-900/60 px-2 py-1.5 text-xs outline-none focus:border-violet-500/60" :disabled="isPublished" @change="touch">
                        <option value="count">count</option><option value="count_distinct">count distinct</option><option value="sum">sum</option><option value="avg">avg</option><option value="median">median</option><option value="min">min</option><option value="max">max</option>
                      </select>
                      <select v-if="comp.agg !== 'count'" v-model="comp.column" class="flex-1 rounded-lg border border-zinc-800 bg-zinc-900/60 px-2 py-1.5 text-xs outline-none focus:border-violet-500/60" :disabled="isPublished" @change="touch">
                        <option value="" disabled>column…</option>
                        <option v-for="c in schema" :key="c.name" :value="c.name">{{ c.name }}</option>
                      </select>
                    </div>
                  </template>
                </div>
              </div>
            </div>

            <div v-if="!isPublished" class="mt-3 grid grid-cols-4 gap-1.5">
              <button v-for="t in (['stat', 'kpi', 'chart', 'table', 'form', 'markdown', 'filter'] as const)" :key="t"
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

            <template v-for="c in previewComps" :key="c.id">
              <!-- stat / kpi -->
              <div v-if="c.type === 'stat' || c.type === 'kpi'" class="mt-3 rounded-2xl border border-zinc-800/80 bg-zinc-950/60 p-4">
                <p class="text-[11px] uppercase tracking-wide text-zinc-500">{{ c.label || c.id }}</p>
                <p class="mt-1 text-2xl font-bold text-zinc-100">{{ c.value === null || c.value === undefined ? '-' : c.value }}</p>
              </div>

              <!-- markdown -->
              <div v-else-if="c.type === 'markdown'" class="mt-3 rounded-2xl border border-zinc-800/80 bg-zinc-950/60 p-4">
                <p v-if="c.title" class="text-xs font-semibold text-zinc-300">{{ c.title }}</p>
                <!-- body is HTML-escaped server-side before markdown transforms -->
                <div class="mt-1 text-xs leading-relaxed text-zinc-400" v-html="c.html" />
              </div>

              <!-- filter -->
              <div v-else-if="c.type === 'filter'" class="mt-3 rounded-2xl border border-zinc-800/80 bg-zinc-950/60 p-4">
                <p class="text-[11px] uppercase tracking-wide text-zinc-500">{{ c.label }} <span class="ml-1 text-zinc-600">· on {{ c.column }} (filters at runtime)</span></p>
                <div class="mt-2 flex flex-wrap gap-1">
                  <span v-for="o in c.options.slice(0, 12)" :key="o" class="rounded bg-zinc-800/80 px-1.5 py-0.5 text-[10px] text-zinc-400">{{ o }}</span>
                  <span v-if="c.options.length > 12" class="text-[10px] text-zinc-600">+{{ c.options.length - 12 }} more</span>
                </div>
              </div>

              <!-- chart -->
              <div v-else-if="c.type === 'chart'" class="mt-3 rounded-2xl border border-zinc-800/80 bg-zinc-950/60 p-4">
                <div class="flex items-center justify-between">
                  <p class="text-xs font-semibold text-zinc-300">{{ c.title || 'Chart' }}</p>
                  <span class="rounded-full bg-zinc-800 px-2 py-0.5 text-[10px] uppercase text-zinc-400">{{ c.chart_type }}</span>
                </div>
                <!-- scatter -->
                <div v-if="c.chart_type === 'scatter'" class="mt-3">
                  <p v-if="!c.points?.length" class="text-[11px] text-zinc-600">No data to chart yet.</p>
                  <div v-else class="flex h-32 items-end gap-1">
                    <div
                      v-for="(pt, pi) in c.points.slice(0, 60)"
                      :key="pi"
                      class="w-2 rounded-t bg-gradient-to-t from-violet-500/60 to-violet-400"
                      :style="{ height: `${Math.max(6, (pt.y / Math.max(...c.points.map((p: any) => p.y || 1))) * 100)}%` }"
                      :title="`${c.x}: ${pt.x} · ${c.y}: ${pt.y}`"
                    />
                  </div>
                </div>
                <!-- labels/values -->
                <template v-else>
                  <div v-if="c.labels?.length" class="mt-3 space-y-2">
                    <div v-for="(label, i) in c.labels" :key="label" class="flex items-center gap-2">
                      <span class="w-28 shrink-0 truncate text-[11px] text-zinc-400">{{ label }}</span>
                      <div class="h-4 flex-1 overflow-hidden rounded-md bg-zinc-900">
                        <div
                          class="h-full rounded-md bg-gradient-to-r from-violet-500/80 to-violet-400/60"
                          :style="{ width: `${Math.max(4, (c.values[i] / Math.max(...c.values)) * 100)}%` }"
                        />
                      </div>
                      <span class="w-10 text-right text-[11px] tabular-nums text-zinc-400">{{ c.values[i] }}</span>
                    </div>
                  </div>
                  <p v-else class="mt-2 text-[11px] text-zinc-600">No data to group yet.</p>
                </template>
              </div>

              <!-- table -->
              <div v-else-if="c.type === 'table'" class="mt-3 overflow-hidden rounded-2xl border border-zinc-800/80 bg-zinc-950/60">
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
              <div v-else-if="c.type === 'form'" class="mt-3 rounded-2xl border border-zinc-800/80 bg-zinc-950/60 p-4">
                <p class="text-xs font-semibold text-zinc-300">{{ c.title || 'Add record' }}</p>
                <div class="mt-3 grid gap-2 sm:grid-cols-2">
                  <div v-for="f in c.fields" :key="f.name">
                    <label class="text-[10px] uppercase tracking-wide text-zinc-500">{{ f.label || f.name }}</label>
                    <div class="mt-1 rounded-lg border border-zinc-800 bg-zinc-900/40 px-2.5 py-1.5 text-xs text-zinc-600">
                      {{ dtypeOf(f.name) === 'boolean' ? 'true / false' : dtypeOf(f.name) === 'integer' || dtypeOf(f.name) === 'number' ? 'number input' : 'text input' }}
                    </div>
                  </div>
                </div>
                <span class="mt-3 inline-block rounded-lg bg-violet-500/20 px-3 py-1.5 text-xs font-medium text-violet-300">{{ c.submit_label || 'Create' }} →</span>
              </div>
            </template>
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
                class="col-span-2 flex items-center justify-center gap-1.5 rounded-xl bg-amber-500/90 py-1.5 text-xs font-semibold text-zinc-950 transition hover:bg-amber-400 disabled:opacity-40"
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

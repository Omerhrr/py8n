<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import {
  Loader2, Search, Plus, Pencil, Trash2, X, CircleAlert, Rocket, Database, RefreshCw, ChevronLeft, ChevronRight, TriangleAlert, Lock,
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
}

interface Runtime {
  app: { name: string; slug: string; description: string; config: { components?: AppComponent[] } }
  dataset: { id: string; name: string; schema_json: { name: string; dtype: string }[]; row_count: number } | null
  stats: Record<string, number | null>
  chart: { labels: string[]; values: number[]; title: string; chart_type: string } | null
  // v46: every component rendered server-side + active filters
  components?: any[]
  filters?: Record<string, string[]>
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

function tq(sep: '?' | '&' = '?'): string {
  return shareTok.value ? `${sep}t=${encodeURIComponent(shareTok.value)}` : ''
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
const statsComps = computed(() => comps.value.filter((c) => c.type === 'stat'))
const chartComp = computed(() => comps.value.find((c) => c.type === 'chart'))
const tableComp = computed(() => comps.value.find((c) => c.type === 'table'))
const formComp = computed(() => comps.value.find((c) => c.type === 'form'))
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
const renderedComps = computed<any[]>(() => rt.value?.components || [])
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

// v46: per-chart pie/donut style (any chart component, not just the first)
function compPieStyle(c: any) {
  const labels = c.labels || []
  const values = c.values || []
  const total = values.reduce((a: number, b: number) => a + b, 0) || 1
  const palette = ['#8b5cf6', '#0ea5e9', '#10b981', '#f59e0b', '#ef4444', '#14b8a6', '#a855f7', '#64748b']
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
  const palette = ['#8b5cf6', '#0ea5e9', '#10b981', '#f59e0b', '#ef4444', '#14b8a6', '#a855f7', '#64748b']
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

onMounted(loadRuntime)

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
  for (const f of formFields()) model[f.name] = f.default !== null && f.default !== undefined ? String(f.default) : ''
  formModel.value = model
  editIndex.value = null
  actionError.value = null
  lastWarnings.value = []
  showModal.value = true
}

function openEdit(index: number) {
  if (!formComp.value || !rows.value[index]) return
  const model: Record<string, any> = {}
  for (const f of formFields()) {
    const v = rows.value[index][f.name]
    model[f.name] = v === null || v === undefined ? '' : String(v)
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
  <div class="pb-16 text-zinc-100">
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
          <span class="flex h-9 w-9 items-center justify-center rounded-xl bg-violet-500/15">
            <Rocket class="h-4 w-4 text-violet-400" />
          </span>
          <div class="min-w-0 flex-1">
            <h1 class="truncate text-base font-bold leading-tight">{{ rt.app.name }}</h1>
            <p class="truncate text-xs text-zinc-500">{{ rt.app.description || rt.app.slug }}</p>
          </div>
          <span v-if="rt.dataset" class="hidden items-center gap-1.5 rounded-full border border-zinc-800 bg-zinc-900/60 px-2.5 py-1 text-[11px] text-zinc-400 sm:flex">
            <Database class="h-3 w-3 text-sky-400" /> {{ rt.dataset.name }} · {{ rt.dataset.row_count }} records
          </span>
          <button class="rounded-lg border border-zinc-800 bg-zinc-900/60 p-1.5 text-zinc-400 transition hover:text-zinc-200" title="Refresh" @click="refreshAll">
            <RefreshCw class="h-3.5 w-3.5" :class="loadingRows && 'animate-spin'" />
          </button>
          <button
            v-if="formComp"
            class="flex items-center gap-1.5 rounded-lg bg-violet-500 px-3 py-1.5 text-xs font-semibold text-white shadow-lg shadow-violet-500/20 transition hover:bg-violet-400"
            @click="openCreate"
          >
            <Plus class="h-3.5 w-3.5" /> {{ formComp.submit_label || 'Create' }}
          </button>
        </div>
      </header>

      <div class="mx-auto max-w-6xl px-4 lg:px-6">
        <p v-if="actionError" class="mt-4 flex items-center gap-2 rounded-xl border border-amber-500/30 bg-amber-500/10 px-4 py-2 text-xs text-amber-300">
          <CircleAlert class="h-3.5 w-3.5 shrink-0" /> {{ actionError }}
        </p>
        <p v-if="lastWarnings.length" class="mt-4 flex items-center gap-2 rounded-xl border border-yellow-500/40 bg-yellow-500/10 px-4 py-2 text-xs text-yellow-300">
          <TriangleAlert class="h-3.5 w-3.5 shrink-0" />
          <span><b>Saved with warnings:</b> {{ lastWarnings.join(' · ') }}</span>
        </p>

        <!-- filter bar (v46) -->
        <div v-if="filterComps.length" class="mt-5 flex flex-wrap items-end gap-3 rounded-2xl border border-zinc-800/80 bg-zinc-900/40 p-4">
          <div v-for="fc in filterComps" :key="fc.id">
            <label class="block text-[10px] uppercase tracking-wide text-zinc-500">{{ fc.label }}</label>
            <select
              class="mt-1 rounded-lg border border-zinc-800 bg-zinc-950/60 px-2.5 py-1.5 text-xs outline-none focus:border-violet-500/60"
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
        <template v-for="c in renderedComps" :key="c.id">
          <!-- stat / kpi -->
          <div v-if="c.type === 'stat' || c.type === 'kpi'" class="mt-5 rounded-2xl border border-zinc-800/80 bg-zinc-900/40 p-4">
            <p class="text-[11px] uppercase tracking-wide text-zinc-500">{{ c.label || c.id }}</p>
            <p class="mt-1 text-2xl font-bold">{{ c.value === null || c.value === undefined ? '-' : c.value }}</p>
          </div>

          <!-- markdown -->
          <div v-else-if="c.type === 'markdown'" class="mt-5 rounded-2xl border border-zinc-800/80 bg-zinc-900/40 p-4">
            <p v-if="c.title" class="text-sm font-semibold text-zinc-200">{{ c.title }}</p>
            <!-- body is HTML-escaped server-side before markdown transforms -->
            <div class="mt-1 text-xs leading-relaxed text-zinc-400 [&_a]:text-sky-400 [&_a]:underline [&_code]:rounded [&_code]:bg-zinc-800 [&_code]:px-1 [&_strong]:text-zinc-200" v-html="c.html" />
          </div>

          <!-- chart -->
          <div v-else-if="c.type === 'chart'" class="mt-5 rounded-2xl border border-zinc-800/80 bg-zinc-900/40 p-4">
            <div class="flex items-center justify-between gap-2">
              <p class="text-sm font-semibold text-zinc-200">{{ c.title || 'Chart' }}</p>
              <span class="rounded-full bg-zinc-800 px-2 py-0.5 text-[10px] uppercase text-zinc-400">{{ c.chart_type }}</span>
            </div>
            <template v-if="c.chart_type === 'scatter'">
              <p v-if="!c.points?.length" class="mt-2 text-[11px] text-zinc-600">No data to chart yet.</p>
              <div v-else class="mt-3 flex h-32 items-end gap-1">
                <div
                  v-for="(pt, pi) in c.points.slice(0, 60)"
                  :key="pi"
                  class="w-2 rounded-t bg-gradient-to-t from-violet-500/60 to-violet-400"
                  :style="{ height: `${Math.max(6, (pt.y / Math.max(...c.points.map((p: any) => p.y || 1))) * 100)}%` }"
                  :title="`${c.x}: ${pt.x} · ${c.y}: ${pt.y}`"
                />
              </div>
            </template>
            <div v-else-if="c.labels?.length" class="mt-3">
              <!-- bar / line / area -->
              <div v-if="c.chart_type !== 'pie' && c.chart_type !== 'donut'" class="space-y-2">
                <div v-for="(label, i) in c.labels" :key="label" class="flex items-center gap-2">
                  <span class="w-28 shrink-0 truncate text-[11px] text-zinc-400">{{ label }}</span>
                  <div class="h-4 flex-1 overflow-hidden rounded-md bg-zinc-900">
                    <div class="h-full rounded-md bg-gradient-to-r from-violet-500/80 to-violet-400/60" :style="{ width: `${Math.max(4, (c.values[i] / Math.max(...c.values)) * 100)}%` }" />
                  </div>
                  <span class="w-10 text-right text-[11px] tabular-nums text-zinc-400">{{ c.values[i] }}</span>
                </div>
              </div>
              <!-- pie / donut -->
              <div v-else class="flex flex-wrap items-center gap-6">
                <div class="h-36 w-36 shrink-0 rounded-full" :style="{ background: compPieStyle(c).background }" />
                <div class="space-y-1.5">
                  <div v-for="(label, i) in c.labels" :key="label" class="flex items-center gap-2 text-xs">
                    <span class="h-2.5 w-2.5 rounded-sm" :style="{ background: compPieStyle(c).palette[i % compPieStyle(c).palette.length] }" />
                    <span class="text-zinc-300">{{ label }}</span>
                    <span class="tabular-nums text-zinc-500">{{ c.values[i] }} ({{ Math.round((c.values[i] / (compPieStyle(c).total || 1)) * 100) }}%)</span>
                  </div>
                </div>
              </div>
            </div>
            <p v-else class="mt-2 text-[11px] text-zinc-600">No data to chart yet.</p>
          </div>
        </template>

        <!-- table -->
        <div v-if="tableComp" class="mt-4 overflow-hidden rounded-2xl border border-zinc-800/80 bg-zinc-900/40">
          <div class="flex items-center gap-3 border-b border-zinc-800/80 px-4 py-2.5">
            <p class="text-sm font-semibold text-zinc-200">{{ tableComp.title || 'Records' }}</p>
            <div class="relative ml-auto w-56">
              <Search class="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-zinc-500" />
              <input
                v-model="search"
                placeholder="Search records…"
                class="w-full rounded-lg border border-zinc-800 bg-zinc-950/60 py-1.5 pl-8 pr-2 text-xs outline-none focus:border-violet-500/60"
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
                    {{ row[col] ?? '-' }}
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
          This app has no components yet - ask the builder to add some.
        </p>
      </div>

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
                <!-- options → dropdown (v30) -->
                <select
                  v-if="f.options && f.options.length"
                  v-model="formModel[f.name]"
                  class="mt-1 w-full rounded-xl border border-zinc-800 bg-zinc-950/60 px-3 py-2 text-sm outline-none focus:border-violet-500/60"
                >
                  <option value="" disabled>choose…</option>
                  <option v-for="o in f.options" :key="String(o)" :value="String(o)">{{ o }}</option>
                </select>
                <select
                  v-else-if="dtypeOf(f.name) === 'boolean'"
                  v-model="formModel[f.name]"
                  class="mt-1 w-full rounded-xl border border-zinc-800 bg-zinc-950/60 px-3 py-2 text-sm outline-none focus:border-violet-500/60"
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
                  class="mt-1 w-full rounded-xl border border-zinc-800 bg-zinc-950/60 px-3 py-2 text-sm outline-none focus:border-violet-500/60"
                />
              </div>
            </div>
            <button
              class="mt-4 flex w-full items-center justify-center gap-1.5 rounded-xl bg-violet-500 py-2 text-sm font-semibold text-white transition hover:bg-violet-400 disabled:opacity-40"
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

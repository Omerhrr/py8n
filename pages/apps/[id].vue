<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import {
  Loader2, Save, Rocket, ExternalLink, Database, Plus, Trash2, X, RefreshCw,
  Gauge, Table2, ClipboardList, BarChart3, ArrowLeft, Unlink, CircleAlert,
} from 'lucide-vue-next'
import { useApi } from '~/composables/useApi'

const { api } = useApi()
const route = useRoute()

interface AppComponent {
  id: string
  type: 'stat' | 'table' | 'form' | 'chart'
  label?: string
  title?: string
  agg?: string
  column?: string
  columns?: string[]
  page_size?: number
  fields?: string[]
  submit_label?: string
  chart_type?: string
  group_by?: string
}

interface AppDetail {
  id: string
  name: string
  slug: string
  description: string
  dataset_id: string | null
  dataset_name: string | null
  config: { components?: AppComponent[] }
  status: string
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

function touch() { dirty.value = true }

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
    if (a.dataset_id) await loadBound(a.dataset_id)
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
  try {
    const updated = await api.patch<any>(`/apps/${appRow.value.id}`, { dataset_id: bindId.value })
    appRow.value = updated
    dirty.value = false
    await loadBound(bindId.value)
    notice.value = bindId.value ? 'Dataset bound' : 'Dataset unbound'
  } catch (e: any) {
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

async function save() {
  if (!appRow.value) return
  saving.value = true
  error.value = null
  try {
    const updated = await api.patch<any>(`/apps/${appRow.value.id}`, {
      name: editingName.value.trim() || appRow.value.name,
      description: editingDesc.value,
    })
    appRow.value = updated
    editingName.value = updated.name
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
  if (dirty.value) await save()
  publishing.value = true
  error.value = null
  try {
    if (isPublished.value) {
      appRow.value = await api.post<any>(`/apps/${appRow.value.id}/unpublish`)
      notice.value = 'Unpublished — back to draft'
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
const TYPE_ICONS: Record<string, any> = { stat: Gauge, table: Table2, form: ClipboardList, chart: BarChart3 }
const TYPE_COLORS: Record<string, string> = {
  stat: 'bg-sky-500/15 text-sky-400',
  table: 'bg-lime-500/15 text-lime-400',
  form: 'bg-amber-500/15 text-amber-400',
  chart: 'bg-violet-500/15 text-violet-400',
}

let uidCounter = 0
function addComponent(type: AppComponent['type']) {
  if (!appRow.value) return
  const comps2 = appRow.value.config.components || (appRow.value.config.components = [])
  if (comps2.length >= 24) { error.value = 'Too many components (max 24)'; return }
  uidCounter++
  const cols = schema.value.map((c) => c.name)
  const numeric = schema.value.filter((c) => c.dtype === 'integer' || c.dtype === 'number').map((c) => c.name)
  const text = schema.value.filter((c) => c.dtype === 'text').map((c) => c.name)
  if (type === 'stat') {
    comps2.push({ id: `stat_new${uidCounter}`, type, label: 'New stat', agg: numeric.length ? 'avg' : 'count', column: numeric[0] })
  } else if (type === 'table') {
    comps2.push({ id: `table_new${uidCounter}`, type, title: 'Records', columns: cols.slice(0, 8), page_size: 10 })
  } else if (type === 'form') {
    comps2.push({ id: `form_new${uidCounter}`, type, title: 'Add record', fields: cols.slice(0, 6), submit_label: 'Create' })
  } else {
    comps2.push({ id: `chart_new${uidCounter}`, type, title: 'Breakdown', chart_type: 'bar', group_by: text[0] || cols[0], agg: 'count' })
  }
  touch()
}

function removeComponent(i: number) {
  if (!appRow.value) return
  appRow.value.config.components?.splice(i, 1)
  touch()
}

function toggleInList(comp: AppComponent, key: 'columns' | 'fields', col: string) {
  const list = comp[key] || (comp[key] = [])
  const i = list.indexOf(col)
  if (i >= 0) list.splice(i, 1)
  else list.push(col)
  touch()
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
  if (!nums.length) return '—'
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
                <option value="">— no dataset —</option>
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
              Published apps are locked — unpublish to edit components.
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
                        :class="(comp.fields || []).includes(c.name) ? 'border-amber-500/50 bg-amber-500/10 text-amber-300' : 'border-zinc-800 text-zinc-500 hover:border-zinc-600'"
                        :disabled="isPublished"
                        @click="toggleInList(comp, 'fields', c.name)"
                      >{{ c.name }}</button>
                    </div>
                    <input v-model="comp.submit_label" placeholder="Submit button label" class="w-full rounded-lg border border-zinc-800 bg-zinc-900/60 px-2.5 py-1.5 text-xs outline-none focus:border-violet-500/60" :disabled="isPublished" @input="touch" />
                  </template>

                  <!-- chart editors -->
                  <template v-else>
                    <input v-model="comp.title" placeholder="Title" class="w-full rounded-lg border border-zinc-800 bg-zinc-900/60 px-2.5 py-1.5 text-xs outline-none focus:border-violet-500/60" :disabled="isPublished" @input="touch" />
                    <div class="flex gap-2">
                      <select v-model="comp.chart_type" class="flex-1 rounded-lg border border-zinc-800 bg-zinc-900/60 px-2 py-1.5 text-xs outline-none focus:border-violet-500/60" :disabled="isPublished" @change="touch">
                        <option value="bar">bar</option><option value="pie">pie</option>
                      </select>
                      <select v-model="comp.group_by" class="flex-1 rounded-lg border border-zinc-800 bg-zinc-900/60 px-2 py-1.5 text-xs outline-none focus:border-violet-500/60" :disabled="isPublished" @change="touch">
                        <option value="" disabled>group by…</option>
                        <option v-for="c in schema" :key="c.name" :value="c.name">{{ c.name }}</option>
                      </select>
                    </div>
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
                </div>
              </div>
            </div>

            <div v-if="!isPublished" class="mt-3 grid grid-cols-4 gap-1.5">
              <button v-for="t in (['stat', 'table', 'form', 'chart'] as const)" :key="t"
                class="flex flex-col items-center gap-1 rounded-xl border border-dashed border-zinc-800 py-2 text-[10px] text-zinc-500 transition hover:border-violet-500/50 hover:text-violet-300"
                @click="addComponent(t)"
              >
                <component :is="TYPE_ICONS[t]" class="h-3.5 w-3.5" /> + {{ t }}
              </button>
            </div>
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
            <!-- stats -->
            <div class="mt-3 grid gap-3 sm:grid-cols-3">
              <div
                v-for="comp in comps.filter((c) => c.type === 'stat')"
                :key="comp.id"
                class="rounded-2xl border border-zinc-800/80 bg-zinc-950/60 p-4"
              >
                <p class="text-[11px] uppercase tracking-wide text-zinc-500">{{ comp.label || comp.id }}</p>
                <p class="mt-1 text-2xl font-bold text-zinc-100">{{ statValue(comp) }}</p>
              </div>
            </div>

            <!-- chart -->
            <div v-if="chartComp" class="mt-4 rounded-2xl border border-zinc-800/80 bg-zinc-950/60 p-4">
              <p class="text-xs font-semibold text-zinc-300">{{ chartComp.title || 'Chart' }}</p>
              <div v-if="chartData.labels.length" class="mt-3 space-y-2">
                <div v-for="(label, i) in chartData.labels" :key="label" class="flex items-center gap-2">
                  <span class="w-28 shrink-0 truncate text-[11px] text-zinc-400">{{ label }}</span>
                  <div class="h-4 flex-1 overflow-hidden rounded-md bg-zinc-900">
                    <div
                      class="h-full rounded-md bg-gradient-to-r from-violet-500/80 to-violet-400/60"
                      :style="{ width: `${Math.max(4, (chartData.values[i] / Math.max(...chartData.values)) * 100)}%` }"
                    />
                  </div>
                  <span class="w-10 text-right text-[11px] tabular-nums text-zinc-400">{{ chartData.values[i] }}</span>
                </div>
              </div>
              <p v-else class="mt-2 text-[11px] text-zinc-600">No data to group yet.</p>
            </div>

            <!-- table -->
            <div v-if="tableComp" class="mt-4 overflow-hidden rounded-2xl border border-zinc-800/80 bg-zinc-950/60">
              <p class="border-b border-zinc-800/80 px-4 py-2.5 text-xs font-semibold text-zinc-300">{{ tableComp.title || 'Records' }}</p>
              <div class="overflow-x-auto">
                <table class="w-full text-left text-xs">
                  <thead>
                    <tr class="border-b border-zinc-800/80 text-zinc-500">
                      <th v-for="col in (tableComp.columns?.length ? tableComp.columns : schema.map((c) => c.name))" :key="col" class="px-4 py-2 font-medium">
                        {{ col }}
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="(row, ri) in tableRows" :key="ri" class="border-b border-zinc-900 text-zinc-300 last:border-0">
                      <td v-for="col in (tableComp.columns?.length ? tableComp.columns : schema.map((c) => c.name))" :key="col" class="max-w-[220px] truncate px-4 py-2">
                        {{ row[col] ?? '—' }}
                      </td>
                    </tr>
                    <tr v-if="!tableRows.length">
                      <td :colspan="schema.length" class="px-4 py-6 text-center text-zinc-600">No records yet.</td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </div>

            <!-- form -->
            <div v-if="formComp" class="mt-4 rounded-2xl border border-zinc-800/80 bg-zinc-950/60 p-4">
              <p class="text-xs font-semibold text-zinc-300">{{ formComp.title || 'Add record' }}</p>
              <div class="mt-3 grid gap-2 sm:grid-cols-2">
                <div v-for="field in formComp.fields || []" :key="field">
                  <label class="text-[10px] uppercase tracking-wide text-zinc-500">{{ field }}</label>
                  <div class="mt-1 rounded-lg border border-zinc-800 bg-zinc-900/40 px-2.5 py-1.5 text-xs text-zinc-600">
                    {{ dtypeOf(field) === 'boolean' ? 'true / false' : dtypeOf(field) === 'integer' || dtypeOf(field) === 'number' ? 'number input' : 'text input' }}
                  </div>
                </div>
              </div>
              <span class="mt-3 inline-block rounded-lg bg-violet-500/20 px-3 py-1.5 text-xs font-medium text-violet-300">{{ formComp.submit_label || 'Create' }} →</span>
            </div>
          </template>
        </div>
      </div>
    </div>
  </div>
</template>

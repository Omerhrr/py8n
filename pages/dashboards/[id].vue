<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import {
  Gauge, Loader2, X, Rocket, Square, Save, RefreshCw, Plus, Trash2,
  ChevronDown, ChevronUp, ArrowUp, ArrowDown, Wand2, ExternalLink, BarChart3, Database, Type,
} from 'lucide-vue-next'
import { useApi } from '~/composables/useApi'

const { api } = useApi()
const route = useRoute()
const boardId = route.params.id as string

interface ComponentDef {
  id: string
  type: 'stat' | 'chart' | 'table' | 'text'
  dataset_id?: string
  label?: string
  agg?: string
  column?: string
  title?: string
  chart_type?: string
  group_by?: string
  columns?: string[]
  limit?: number
  body?: string
}

interface Board {
  id: string
  name: string
  slug: string
  description: string
  config: { components?: ComponentDef[] }
  status: string
}

interface DatasetMeta {
  id: string
  name: string
  row_count: number
  schema_json: { name: string; dtype: string }[]
}

const board = ref<Board | null>(null)
const datasets = ref<DatasetMeta[]>([])
const comps = ref<ComponentDef[]>([])
const preview = ref<any[]>([])
const loading = ref(true)
const error = ref<string | null>(null)
const saving = ref(false)
const previewing = ref(false)
const publishing = ref(false)
const dirty = ref(false)
const openComp = ref<string | null>(null)

const AGGS = ['count', 'sum', 'avg', 'min', 'max']
const CHART_TYPES = ['bar', 'line', 'pie']

async function load() {
  loading.value = true
  try {
    const [b, d] = await Promise.all([
      api.get<Board>(`/dashboards/${boardId}`),
      api.get<DatasetMeta[]>('/datasets'),
    ])
    board.value = b
    datasets.value = d
    comps.value = JSON.parse(JSON.stringify(b.config?.components || []))
    dirty.value = false
    await refreshPreview()
  } catch (e: any) {
    error.value = e?.data?.detail || e?.message || 'Failed to load dashboard'
  } finally {
    loading.value = false
  }
}
onMounted(load)

const dsById = computed(() => Object.fromEntries(datasets.value.map((d) => [d.id, d])))
const isPublished = computed(() => board.value?.status === 'published')

function schemaOf(comp: ComponentDef) {
  const ds = dsById.value[comp.dataset_id || '']
  return ds?.schema_json || []
}
function colsOf(comp: ComponentDef, dtypes?: string[]) {
  let s = schemaOf(comp)
  if (dtypes) s = s.filter((c) => dtypes.includes(c.dtype))
  return s.map((c) => c.name)
}

// ---------- component editing ----------
let compSeq = 0
function uid(t: string) {
  compSeq += 1
  return `${t}_${Date.now().toString(36)}${compSeq}`
}

function addComp(type: ComponentDef['type']) {
  if (isPublished.value) return
  const base: any = { id: uid(type), type }
  if (type === 'stat') {
    base.dataset_id = datasets.value[0]?.id || ''
    base.label = 'New stat'
    base.agg = 'count'
  } else if (type === 'chart') {
    base.dataset_id = datasets.value[0]?.id || ''
    base.title = 'New chart'
    base.chart_type = 'bar'
    const s = schemaOf({ dataset_id: datasets.value[0]?.id } as ComponentDef)
    const g = s.find((c) => c.dtype === 'text')
    base.group_by = g?.name || s[0]?.name || ''
    base.agg = 'count'
  } else if (type === 'table') {
    base.dataset_id = datasets.value[0]?.id || ''
    base.title = 'New table'
    base.columns = colsOf({ dataset_id: datasets.value[0]?.id } as ComponentDef).slice(0, 4)
    base.limit = 8
  } else if (type === 'text') {
    base.title = 'Section note'
    base.body = ''
  }
  comps.value = [...comps.value, base]
  openComp.value = base.id
  dirty.value = true
}

function removeComp(i: number) {
  comps.value = comps.value.filter((_, idx) => idx !== i)
  dirty.value = true
}

function move(i: number, dir: -1 | 1) {
  const j = i + dir
  if (j < 0 || j >= comps.value.length) return
  const arr = [...comps.value]
  ;[arr[i], arr[j]] = [arr[j], arr[i]]
  comps.value = arr
  dirty.value = true
}

function onDatasetChange(c: ComponentDef, id: string) {
  c.dataset_id = id
  // reset column-ish params that no longer exist
  const names = new Set(colsOf(c))
  if (c.type === 'stat' && c.agg !== 'count' && !names.has(c.column || '')) c.column = names.values().next().value || ''
  if (c.type === 'chart') {
    if (!names.has(c.group_by || '')) c.group_by = names.values().next().value || ''
    if (c.agg !== 'count' && !names.has(c.column || '')) c.column = names.values().next().value || ''
  }
  if (c.type === 'table') c.columns = (c.columns || []).filter((x) => names.has(x))
  dirty.value = true
}

// ---------- save / preview / publish ----------
async function saveConfig() {
  if (!board.value) return
  saving.value = true
  error.value = null
  try {
    const updated = await api.patch<Board>(`/dashboards/${board.value.id}`, {
      config: { components: comps.value },
    })
    board.value = updated
    comps.value = JSON.parse(JSON.stringify(updated.config?.components || []))
    dirty.value = false
    await refreshPreview()
  } catch (e: any) {
    error.value = e?.data?.detail || e?.message || 'Save failed'
  } finally {
    saving.value = false
  }
}

async function refreshPreview() {
  if (!board.value) return
  previewing.value = true
  try {
    // save first if dirty - preview runs server-side on the saved config
    if (dirty.value) {
      await saveConfig()
      return // saveConfig already refreshes the preview
    }
    const res = await api.post<any>(`/dashboards/${board.value.id}/preview`)
    preview.value = res.components || []
  } catch (e: any) {
    error.value = e?.data?.detail || e?.message || 'Preview failed'
  } finally {
    previewing.value = false
  }
}

async function regenerate() {
  if (!board.value) return
  if (!confirm('Re-generate the layout from the referenced datasets? Your current components are replaced.')) return
  saving.value = true
  error.value = null
  try {
    const updated = await api.post<Board>(`/dashboards/${board.value.id}/generate`)
    board.value = updated
    comps.value = JSON.parse(JSON.stringify(updated.config?.components || []))
    dirty.value = false
    await refreshPreview()
  } catch (e: any) {
    error.value = e?.data?.detail || e?.message || 'Regenerate failed'
  } finally {
    saving.value = false
  }
}

async function togglePublish() {
  if (!board.value) return
  publishing.value = true
  error.value = null
  try {
    if (isPublished.value) {
      const updated = await api.post<Board>(`/dashboards/${board.value.id}/unpublish`)
      board.value = updated
    } else {
      if (dirty.value) {
        await saveConfig()
        // a failed save must not publish the stale server config
        if (error.value) return
      }
      const updated = await api.post<Board>(`/dashboards/${board.value.id}/publish`)
      board.value = updated
    }
  } catch (e: any) {
    error.value = e?.data?.detail || e?.message || 'Publish failed'
  } finally {
    publishing.value = false
  }
}

const typeIcon: Record<string, any> = { stat: Gauge, chart: BarChart3, table: Database, text: Type }
const typeColor: Record<string, string> = {
  stat: 'bg-cyan-500/10 text-cyan-400',
  chart: 'bg-violet-500/10 text-violet-400',
  table: 'bg-sky-500/10 text-sky-400',
  text: 'bg-amber-500/10 text-amber-400',
}
</script>

<template>
  <div class="min-h-screen pb-16 text-zinc-100">
    <header class="sticky top-0 z-20 border-b border-zinc-800/80 bg-zinc-950/90 backdrop-blur">
      <div class="mx-auto flex max-w-7xl items-center gap-3 px-4 py-3 lg:px-6">
        <button class="rounded-lg p-1.5 text-zinc-500 hover:text-zinc-200" title="Back" @click="navigateTo('/dashboards')">
          <X class="h-4 w-4" />
        </button>
        <span class="flex h-8 w-8 shrink-0 items-center justify-center rounded-xl bg-cyan-500/15">
          <Gauge class="h-4 w-4 text-cyan-400" />
        </span>
        <div class="min-w-0 flex-1">
          <div class="flex items-center gap-2">
            <h1 class="truncate text-sm font-bold">{{ board?.name || '…' }}</h1>
            <span
              class="rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase"
              :class="isPublished ? 'bg-emerald-500/15 text-emerald-400' : 'bg-amber-500/15 text-amber-400'"
            >{{ board?.status }}</span>
            <span v-if="dirty" class="rounded-full bg-zinc-800 px-2 py-0.5 text-[10px] text-zinc-400">unsaved</span>
          </div>
          <p class="truncate text-[11px] text-zinc-500">Dashboards are read-only analytics - components bind to datasets individually</p>
        </div>
        <button
          class="flex items-center gap-1.5 rounded-lg border border-zinc-800 bg-zinc-900/60 px-2.5 py-1.5 text-xs font-medium text-zinc-300 transition hover:border-zinc-600"
          title="Re-generate layout from referenced datasets"
          :disabled="isPublished || saving"
          @click="regenerate"
        >
          <Wand2 class="h-3.5 w-3.5" /> <span class="hidden sm:inline">Regenerate</span>
        </button>
        <button
          class="flex items-center gap-1.5 rounded-lg bg-cyan-500 px-3 py-1.5 text-xs font-semibold text-white transition hover:bg-cyan-400 disabled:opacity-40"
          :disabled="saving || (!dirty && preview.length)"
          @click="refreshPreview"
        >
          <Loader2 v-if="previewing" class="h-3.5 w-3.5 animate-spin" />
          <RefreshCw v-else class="h-3.5 w-3.5" /> Refresh preview
        </button>
        <button
          v-if="isPublished"
          class="flex items-center gap-1 rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-2.5 py-1.5 text-xs font-medium text-emerald-400 transition hover:bg-emerald-500/20"
          @click="navigateTo(`/d/${board?.slug}`)"
        >
          <ExternalLink class="h-3.5 w-3.5" /> /d/{{ board?.slug }}
        </button>
        <button
          class="flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-semibold text-white transition disabled:opacity-40"
          :class="isPublished ? 'bg-zinc-700 hover:bg-zinc-600' : 'bg-emerald-500 hover:bg-emerald-400'"
          :disabled="publishing"
          @click="togglePublish"
        >
          <Loader2 v-if="publishing" class="h-3.5 w-3.5 animate-spin" />
          <Rocket v-else-if="!isPublished" class="h-3.5 w-3.5" />
          <Square v-else class="h-3 w-3" />
          {{ isPublished ? 'Unpublish' : 'Publish' }}
        </button>
      </div>
    </header>

    <p v-if="error" class="mx-auto mt-4 max-w-7xl rounded-xl border border-amber-500/30 bg-amber-500/10 px-4 py-2.5 text-sm text-amber-300 lg:px-6">{{ error }}</p>

    <div v-if="loading" class="mt-16 flex justify-center text-zinc-500">
      <Loader2 class="h-6 w-6 animate-spin" />
    </div>

    <div v-else-if="board" class="mx-auto mt-5 grid max-w-7xl gap-5 px-4 lg:grid-cols-12 lg:px-6">
      <!-- LEFT: component editors -->
      <section class="lg:col-span-5">
        <div class="flex items-center justify-between">
          <h2 class="text-xs font-semibold uppercase tracking-wide text-zinc-500">Components ({{ comps.length }})</h2>
          <div class="flex items-center gap-1">
            <button class="flex items-center gap-1 rounded-lg border border-zinc-800 bg-zinc-900/60 px-2 py-1 text-[11px] text-zinc-300 hover:border-cyan-500/50" :disabled="isPublished" @click="addComp('stat')"><Gauge class="h-3 w-3" /> Stat</button>
            <button class="flex items-center gap-1 rounded-lg border border-zinc-800 bg-zinc-900/60 px-2 py-1 text-[11px] text-zinc-300 hover:border-violet-500/50" :disabled="isPublished" @click="addComp('chart')"><BarChart3 class="h-3 w-3" /> Chart</button>
            <button class="flex items-center gap-1 rounded-lg border border-zinc-800 bg-zinc-900/60 px-2 py-1 text-[11px] text-zinc-300 hover:border-sky-500/50" :disabled="isPublished" @click="addComp('table')"><Database class="h-3 w-3" /> Table</button>
            <button class="flex items-center gap-1 rounded-lg border border-zinc-800 bg-zinc-900/60 px-2 py-1 text-[11px] text-zinc-300 hover:border-amber-500/50" :disabled="isPublished" @click="addComp('text')"><Type class="h-3 w-3" /> Text</button>
          </div>
        </div>

        <div class="mt-3 space-y-2">
          <div
            v-for="(c, i) in comps"
            :key="c.id"
            class="rounded-2xl border bg-zinc-900/40"
            :class="openComp === c.id ? 'border-cyan-500/40' : 'border-zinc-800/80'"
          >
            <button class="flex w-full items-center gap-2.5 px-3 py-2.5 text-left" @click="openComp = openComp === c.id ? null : c.id">
              <span class="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg" :class="typeColor[c.type]">
                <component :is="typeIcon[c.type]" class="h-3.5 w-3.5" />
              </span>
              <span class="min-w-0 flex-1">
                <span class="block truncate text-xs font-semibold text-zinc-200">{{ c.type === 'stat' ? c.label : (c.type === 'text' ? (c.title || 'Text') : (c.title || c.type)) }}</span>
                <span class="block truncate text-[10px] text-zinc-500">
                  {{ c.type }} · {{ dsById[c.dataset_id || '']?.name || 'text' }}
                  <template v-if="c.type === 'chart'"> · {{ c.chart_type }} · {{ c.group_by }}</template>
                  <template v-if="c.type === 'stat' && c.agg !== 'count'"> · {{ c.agg }} {{ c.column }}</template>
                </span>
              </span>
              <span class="flex shrink-0 items-center gap-0.5">
                <span v-show="openComp === c.id && !isPublished" class="flex">
                  <span class="rounded p-1 text-zinc-500 hover:text-zinc-200" title="Move up" @click.stop="move(i, -1)"><ArrowUp class="h-3 w-3" /></span>
                  <span class="rounded p-1 text-zinc-500 hover:text-zinc-200" title="Move down" @click.stop="move(i, 1)"><ArrowDown class="h-3 w-3" /></span>
                </span>
                <ChevronUp v-if="openComp === c.id" class="h-3.5 w-3.5 text-zinc-500" />
                <ChevronDown v-else class="h-3.5 w-3.5 text-zinc-500" />
              </span>
            </button>

            <div v-if="openComp === c.id && !isPublished" class="border-t border-zinc-800/80 px-3 py-3">
              <!-- stat editor -->
              <template v-if="c.type === 'stat'">
                <label class="block text-[10px] font-medium uppercase tracking-wide text-zinc-500">Dataset</label>
                <select :value="c.dataset_id" class="mt-1 w-full rounded-lg border border-zinc-800 bg-zinc-950/60 px-2.5 py-1.5 text-xs outline-none focus:border-cyan-500/60" @change="onDatasetChange(c, ($event.target as HTMLSelectElement).value)">
                  <option value="" disabled>Pick a dataset…</option>
                  <option v-for="d in datasets" :key="d.id" :value="d.id">{{ d.name }} ({{ d.row_count }} rows)</option>
                </select>
                <label class="mt-2.5 block text-[10px] font-medium uppercase tracking-wide text-zinc-500">Label</label>
                <input v-model="c.label" class="mt-1 w-full rounded-lg border border-zinc-800 bg-zinc-950/60 px-2.5 py-1.5 text-xs outline-none focus:border-cyan-500/60" @input="dirty = true" />
                <div class="mt-2.5 grid grid-cols-2 gap-2">
                  <div>
                    <label class="block text-[10px] font-medium uppercase tracking-wide text-zinc-500">Aggregation</label>
                    <select v-model="c.agg" class="mt-1 w-full rounded-lg border border-zinc-800 bg-zinc-950/60 px-2.5 py-1.5 text-xs outline-none focus:border-cyan-500/60" @change="dirty = true">
                      <option v-for="a in AGGS" :key="a" :value="a">{{ a }}</option>
                    </select>
                  </div>
                  <div v-if="c.agg !== 'count'">
                    <label class="block text-[10px] font-medium uppercase tracking-wide text-zinc-500">Numeric column</label>
                    <select v-model="c.column" class="mt-1 w-full rounded-lg border border-zinc-800 bg-zinc-950/60 px-2.5 py-1.5 text-xs outline-none focus:border-cyan-500/60" @change="dirty = true">
                      <option v-for="col in colsOf(c, ['integer', 'number'])" :key="col" :value="col">{{ col }}</option>
                    </select>
                  </div>
                </div>
              </template>

              <!-- chart editor -->
              <template v-else-if="c.type === 'chart'">
                <label class="block text-[10px] font-medium uppercase tracking-wide text-zinc-500">Dataset</label>
                <select :value="c.dataset_id" class="mt-1 w-full rounded-lg border border-zinc-800 bg-zinc-950/60 px-2.5 py-1.5 text-xs outline-none focus:border-violet-500/60" @change="onDatasetChange(c, ($event.target as HTMLSelectElement).value)">
                  <option value="" disabled>Pick a dataset…</option>
                  <option v-for="d in datasets" :key="d.id" :value="d.id">{{ d.name }} ({{ d.row_count }} rows)</option>
                </select>
                <label class="mt-2.5 block text-[10px] font-medium uppercase tracking-wide text-zinc-500">Title</label>
                <input v-model="c.title" class="mt-1 w-full rounded-lg border border-zinc-800 bg-zinc-950/60 px-2.5 py-1.5 text-xs outline-none focus:border-violet-500/60" @input="dirty = true" />
                <div class="mt-2.5 grid grid-cols-2 gap-2">
                  <div>
                    <label class="block text-[10px] font-medium uppercase tracking-wide text-zinc-500">Chart type</label>
                    <select v-model="c.chart_type" class="mt-1 w-full rounded-lg border border-zinc-800 bg-zinc-950/60 px-2.5 py-1.5 text-xs outline-none focus:border-violet-500/60" @change="dirty = true">
                      <option v-for="t in CHART_TYPES" :key="t" :value="t">{{ t }}</option>
                    </select>
                  </div>
                  <div>
                    <label class="block text-[10px] font-medium uppercase tracking-wide text-zinc-500">Group by</label>
                    <select v-model="c.group_by" class="mt-1 w-full rounded-lg border border-zinc-800 bg-zinc-950/60 px-2.5 py-1.5 text-xs outline-none focus:border-violet-500/60" @change="dirty = true">
                      <option v-for="col in colsOf(c)" :key="col" :value="col">{{ col }}</option>
                    </select>
                  </div>
                </div>
                <div class="mt-2.5">
                  <label class="block text-[10px] font-medium uppercase tracking-wide text-zinc-500">Aggregation</label>
                  <div class="mt-1 grid grid-cols-2 gap-2">
                    <select v-model="c.agg" class="w-full rounded-lg border border-zinc-800 bg-zinc-950/60 px-2.5 py-1.5 text-xs outline-none focus:border-violet-500/60" @change="dirty = true">
                      <option v-for="a in AGGS" :key="a" :value="a">{{ a }}</option>
                    </select>
                    <select v-if="c.agg !== 'count'" v-model="c.column" class="w-full rounded-lg border border-zinc-800 bg-zinc-950/60 px-2.5 py-1.5 text-xs outline-none focus:border-violet-500/60" @change="dirty = true">
                      <option v-for="col in colsOf(c, ['integer', 'number'])" :key="col" :value="col">{{ col }}</option>
                    </select>
                  </div>
                </div>
              </template>

              <!-- table editor -->
              <template v-else-if="c.type === 'table'">
                <label class="block text-[10px] font-medium uppercase tracking-wide text-zinc-500">Dataset</label>
                <select :value="c.dataset_id" class="mt-1 w-full rounded-lg border border-zinc-800 bg-zinc-950/60 px-2.5 py-1.5 text-xs outline-none focus:border-sky-500/60" @change="onDatasetChange(c, ($event.target as HTMLSelectElement).value)">
                  <option value="" disabled>Pick a dataset…</option>
                  <option v-for="d in datasets" :key="d.id" :value="d.id">{{ d.name }} ({{ d.row_count }} rows)</option>
                </select>
                <label class="mt-2.5 block text-[10px] font-medium uppercase tracking-wide text-zinc-500">Title</label>
                <input v-model="c.title" class="mt-1 w-full rounded-lg border border-zinc-800 bg-zinc-950/60 px-2.5 py-1.5 text-xs outline-none focus:border-sky-500/60" @input="dirty = true" />
                <label class="mt-2.5 block text-[10px] font-medium uppercase tracking-wide text-zinc-500">Columns</label>
                <div class="mt-1 flex flex-wrap gap-1.5">
                  <button
                    v-for="col in colsOf(c)"
                    :key="col"
                    class="rounded-full border px-2 py-0.5 text-[10px] font-medium transition"
                    :class="(c.columns || []).includes(col) ? 'border-sky-500/60 bg-sky-500/10 text-sky-300' : 'border-zinc-800 bg-zinc-950/60 text-zinc-500 hover:border-zinc-600'"
                    @click="c.columns = (c.columns || []).includes(col) ? (c.columns || []).filter((x) => x !== col) : [...(c.columns || []), col]; dirty = true"
                  >{{ col }}</button>
                </div>
                <label class="mt-2.5 block text-[10px] font-medium uppercase tracking-wide text-zinc-500">Row limit (1-100)</label>
                <input type="number" min="1" max="100" :value="c.limit" class="mt-1 w-24 rounded-lg border border-zinc-800 bg-zinc-950/60 px-2.5 py-1.5 text-xs outline-none focus:border-sky-500/60" @change="(e: any) => { c.limit = Math.max(1, Math.min(100, Number(e.target.value) || 8)); dirty = true }" />
              </template>

              <!-- text editor -->
              <template v-else>
                <label class="block text-[10px] font-medium uppercase tracking-wide text-zinc-500">Title</label>
                <input v-model="c.title" class="mt-1 w-full rounded-lg border border-zinc-800 bg-zinc-950/60 px-2.5 py-1.5 text-xs outline-none focus:border-amber-500/60" @input="dirty = true" />
                <label class="mt-2.5 block text-[10px] font-medium uppercase tracking-wide text-zinc-500">Body</label>
                <textarea v-model="c.body" rows="3" class="mt-1 w-full resize-y rounded-lg border border-zinc-800 bg-zinc-950/60 px-2.5 py-1.5 text-xs outline-none focus:border-amber-500/60" @input="dirty = true" />
              </template>

              <div class="mt-3 flex items-center justify-between">
                <button class="flex items-center gap-1 text-[11px] text-zinc-500 transition hover:text-amber-400" @click="removeComp(i)">
                  <Trash2 class="h-3 w-3" /> Remove
                </button>
              </div>
            </div>
          </div>

          <p v-if="comps.length === 0" class="rounded-2xl border border-dashed border-zinc-800 px-4 py-8 text-center text-xs text-zinc-600">
            Empty board - add a stat, chart, table or text component above.
          </p>
        </div>

        <!-- save bar -->
        <button
          class="mt-4 flex w-full items-center justify-center gap-1.5 rounded-xl bg-cyan-500 py-2 text-sm font-semibold text-white transition hover:bg-cyan-400 disabled:opacity-40"
          :disabled="saving || !dirty"
          @click="saveConfig"
        >
          <Loader2 v-if="saving" class="h-4 w-4 animate-spin" />
          <Save v-else class="h-4 w-4" />
          {{ dirty ? 'Save changes' : 'All changes saved' }}
        </button>
      </section>

      <!-- RIGHT: live preview -->
      <section class="lg:col-span-7">
        <div class="flex items-center justify-between">
          <h2 class="text-xs font-semibold uppercase tracking-wide text-zinc-500">Live preview (server-computed)</h2>
        </div>
        <div class="mt-3 grid gap-3 sm:grid-cols-2">
          <DashboardBoard :components="preview" />
        </div>
        <p v-if="preview.length === 0" class="mt-6 rounded-2xl border border-dashed border-zinc-800 px-4 py-10 text-center text-xs text-zinc-600">
          Nothing to preview yet - add components and save.
        </p>
      </section>
    </div>
  </div>
</template>

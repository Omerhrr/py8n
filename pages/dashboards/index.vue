<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import {
  Gauge, Plus, Trash2, Loader2, Search, ExternalLink, Settings2,
  Database, X, Rocket, FileSpreadsheet, Square, BarChart3,
} from 'lucide-vue-next'
import { useApi } from '~/composables/useApi'

const { api } = useApi()

interface DashMeta {
  id: string
  name: string
  slug: string
  description: string
  config: { components?: any[] }
  status: string
  created_at: string | null
  updated_at: string | null
}

interface DatasetMeta {
  id: string
  name: string
  row_count: number
  schema_json: any[]
}

const loading = ref(true)
const boards = ref<DashMeta[]>([])
const datasets = ref<DatasetMeta[]>([])
const search = ref('')
const error = ref<string | null>(null)

// create modal
const showCreate = ref(false)
const creating = ref(false)
const crName = ref('')
const crDesc = ref('')
const crFromDataset = ref(true)
const crPicked = ref<string[]>([])

const deleting = ref<string | null>(null)

async function load() {
  loading.value = true
  try {
    const [b, d] = await Promise.all([
      api.get<DashMeta[]>('/dashboards'),
      api.get<DatasetMeta[]>('/datasets'),
    ])
    boards.value = b
    datasets.value = d
  } catch (e: any) {
    error.value = e?.data?.detail || e?.message || 'Failed to load dashboards'
  } finally {
    loading.value = false
  }
}
onMounted(load)

const filtered = computed(() => {
  const q = search.value.trim().toLowerCase()
  if (!q) return boards.value
  return boards.value.filter(
    (b) => b.name.toLowerCase().includes(q) || (b.description || '').toLowerCase().includes(q),
  )
})

const stats = computed(() => ({
  total: boards.value.length,
  published: boards.value.filter((b) => b.status === 'published').length,
}))

function togglePick(id: string) {
  if (crPicked.value.includes(id)) crPicked.value = crPicked.value.filter((x) => x !== id)
  else crPicked.value = [...crPicked.value, id]
}

async function doCreate() {
  if (!crName.value.trim()) return
  if (crFromDataset.value && crPicked.value.length === 0) return
  creating.value = true
  error.value = null
  try {
    const created = await api.post<any>('/dashboards', {
      name: crName.value.trim(),
      description: crDesc.value.trim(),
      dataset_ids: crFromDataset.value ? crPicked.value : [],
      generate: crFromDataset.value,
    })
    showCreate.value = false
    navigateTo(`/dashboards/${created.id}`)
  } catch (e: any) {
    error.value = e?.data?.detail || e?.message || 'Create failed'
  } finally {
    creating.value = false
  }
}

async function remove(b: DashMeta) {
  if (!confirm(`Delete dashboard "${b.name}"? Datasets are NOT deleted.`)) return
  deleting.value = b.id
  try {
    await api.del(`/dashboards/${b.id}`)
    boards.value = boards.value.filter((r) => r.id !== b.id)
  } catch (e: any) {
    alert(e?.data?.detail || e?.message || 'Delete failed')
  } finally {
    deleting.value = null
  }
}

function compCount(b: DashMeta) {
  return (b.config?.components || []).length
}
function compSummary(b: DashMeta) {
  const c = b.config?.components || []
  const s = ['stat', 'chart', 'table', 'text'].map((t) => `${c.filter((x: any) => x.type === t).length} ${t}`).join(' · ')
  return s || 'empty board'
}

function fmtDate(iso: string | null) {
  if (!iso) return '-'
  return new Date(iso).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}
</script>

<template>
  <div class="pb-10 text-zinc-100">
    <header class="sticky top-0 z-20 border-b border-zinc-800/80 bg-zinc-950/90 backdrop-blur">
      <div class="mx-auto flex max-w-6xl items-center gap-3 px-4 py-3.5 lg:px-6">
        <span class="flex h-9 w-9 items-center justify-center rounded-xl bg-cyan-500/15">
          <Gauge class="h-4 w-4 text-cyan-400" />
        </span>
        <div class="min-w-0 flex-1">
          <h1 class="truncate text-base font-bold leading-tight">Dashboards</h1>
          <p class="text-xs text-zinc-500">Read-only analytics over many datasets - KPIs, breakdown charts and tables on one wall</p>
        </div>
        <button
          class="flex items-center gap-1.5 rounded-lg bg-cyan-500 px-3 py-1.5 text-xs font-semibold text-white shadow-lg shadow-cyan-500/20 transition hover:bg-cyan-400"
          @click="showCreate = true; crFromDataset = datasets.length > 0"
        >
          <Plus class="h-3.5 w-3.5" /> New dashboard
        </button>
      </div>
    </header>

    <div class="mx-auto max-w-6xl px-4 lg:px-6">
      <!-- stats + search -->
      <div class="mt-5 flex flex-wrap items-center gap-3">
        <div class="flex items-center gap-4 rounded-xl border border-zinc-800/80 bg-zinc-900/40 px-4 py-2.5 text-sm">
          <span class="flex items-center gap-1.5 text-zinc-400"><Gauge class="h-3.5 w-3.5 text-cyan-400" /> <b class="text-zinc-100">{{ stats.total }}</b> boards</span>
          <span class="flex items-center gap-1.5 text-zinc-400"><Rocket class="h-3.5 w-3.5 text-emerald-400" /> <b class="text-zinc-100">{{ stats.published }}</b> published</span>
        </div>
        <div class="relative min-w-[220px] flex-1">
          <Search class="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-zinc-500" />
          <input
            v-model="search"
            placeholder="Search dashboards…"
            class="w-full rounded-xl border border-zinc-800 bg-zinc-900/60 py-2 pl-9 pr-3 text-sm text-zinc-200 placeholder-zinc-500 outline-none transition focus:border-cyan-500/60"
          />
        </div>
      </div>

      <p v-if="error" class="mt-4 rounded-xl border border-amber-500/30 bg-amber-500/10 px-4 py-2.5 text-sm text-amber-300">{{ error }}</p>

      <!-- list -->
      <div v-if="loading" class="mt-10 flex justify-center text-zinc-500">
        <Loader2 class="h-6 w-6 animate-spin" />
      </div>
      <div v-else-if="filtered.length === 0" class="mt-16 text-center">
        <span class="mx-auto flex h-14 w-14 items-center justify-center rounded-2xl bg-zinc-900">
          <Gauge class="h-6 w-6 text-zinc-600" />
        </span>
        <p class="mt-4 text-sm font-medium text-zinc-300">No dashboards yet</p>
        <p class="mx-auto mt-1 max-w-md text-xs text-zinc-500">Pick a few datasets and generate a board in one click - KPI stat cards, breakdown charts and a live table, publishable to /d/{slug}.</p>
        <button class="mt-4 inline-flex items-center gap-1.5 rounded-lg bg-cyan-500 px-3 py-1.5 text-xs font-semibold text-white hover:bg-cyan-400" @click="showCreate = true; crFromDataset = datasets.length > 0">
          <Plus class="h-3.5 w-3.5" /> New dashboard
        </button>
      </div>
      <div v-else class="mt-5 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        <div
          v-for="b in filtered"
          :key="b.id"
          class="group relative cursor-pointer rounded-2xl border border-zinc-800/80 bg-zinc-900/40 p-4 transition hover:border-cyan-500/40 hover:bg-zinc-900/70"
          @click="navigateTo(`/dashboards/${b.id}`)"
        >
          <div class="flex items-start gap-3">
            <span class="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-cyan-500/10">
              <Rocket v-if="b.status === 'published'" class="h-4 w-4 text-emerald-400" />
              <Settings2 v-else class="h-4 w-4 text-cyan-400" />
            </span>
            <div class="min-w-0 flex-1">
              <p class="truncate text-sm font-semibold text-zinc-100 group-hover:text-cyan-300">{{ b.name }}</p>
              <p class="mt-0.5 line-clamp-1 text-xs text-zinc-500">{{ b.description || 'No description' }}</p>
            </div>
            <span
              class="rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase"
              :class="b.status === 'published' ? 'bg-emerald-500/15 text-emerald-400' : 'bg-amber-500/15 text-amber-400'"
            >{{ b.status }}</span>
          </div>
          <div class="mt-3 flex items-center gap-3 text-[11px] text-zinc-500">
            <span><b class="text-zinc-300">{{ compCount(b) }}</b> components</span>
            <span class="truncate text-zinc-600">{{ compSummary(b) }}</span>
            <span class="ml-auto shrink-0">{{ fmtDate(b.updated_at) }}</span>
          </div>
          <div class="mt-3 flex items-center gap-2">
            <button
              v-if="b.status === 'published'"
              class="flex items-center gap-1 rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-2 py-1 text-[11px] font-medium text-emerald-400 transition hover:bg-emerald-500/20"
              @click.stop="navigateTo(`/d/${b.slug}`)"
            >
              <ExternalLink class="h-3 w-3" /> Open /d/{{ b.slug }}
            </button>
            <span v-else class="flex items-center gap-1 text-[11px] text-zinc-600"><Square class="h-2.5 w-2.5" /> draft - publish in builder</span>
          </div>
          <button
            class="absolute right-3 top-3 rounded-lg p-1.5 text-zinc-600 opacity-0 transition hover:bg-amber-500/10 hover:text-amber-400 group-hover:opacity-100"
            title="Delete dashboard"
            @click.stop="remove(b)"
          >
            <Loader2 v-if="deleting === b.id" class="h-3.5 w-3.5 animate-spin" />
            <Trash2 v-else class="h-3.5 w-3.5" />
          </button>
        </div>
      </div>
    </div>

    <!-- create modal -->
    <Teleport to="body">
      <div v-if="showCreate" class="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4 backdrop-blur-sm" @click.self="showCreate = false">
        <div class="w-full max-w-md rounded-2xl border border-zinc-800 bg-zinc-900 p-5 shadow-2xl">
          <div class="flex items-center justify-between">
            <h2 class="text-sm font-bold">New dashboard</h2>
            <button class="rounded-lg p-1 text-zinc-500 hover:text-zinc-200" @click="showCreate = false"><X class="h-4 w-4" /></button>
          </div>

          <div class="mt-4 grid grid-cols-2 gap-2">
            <button
              class="flex items-center justify-center gap-1.5 rounded-xl border px-3 py-2.5 text-xs font-medium transition"
              :class="crFromDataset ? 'border-cyan-500/60 bg-cyan-500/10 text-cyan-300' : 'border-zinc-800 bg-zinc-950/60 text-zinc-400 hover:border-zinc-600'"
              :disabled="!datasets.length"
              @click="crFromDataset = true"
            >
              <FileSpreadsheet class="h-3.5 w-3.5" /> From datasets
            </button>
            <button
              class="flex items-center justify-center gap-1.5 rounded-xl border px-3 py-2.5 text-xs font-medium transition"
              :class="!crFromDataset ? 'border-cyan-500/60 bg-cyan-500/10 text-cyan-300' : 'border-zinc-800 bg-zinc-950/60 text-zinc-400 hover:border-zinc-600'"
              @click="crFromDataset = false"
            >
              <Square class="h-3.5 w-3.5" /> Blank first
            </button>
          </div>

          <template v-if="crFromDataset">
            <p class="mt-3 text-[11px] text-zinc-500">Pick one or more datasets - stats, charts and a table are laid out automatically.</p>
            <div class="mt-2 max-h-44 space-y-1.5 overflow-y-auto pr-1">
              <button
                v-for="d in datasets"
                :key="d.id"
                class="flex w-full items-center gap-2 rounded-xl border px-3 py-2 text-left text-xs transition"
                :class="crPicked.includes(d.id) ? 'border-cyan-500/60 bg-cyan-500/10 text-cyan-200' : 'border-zinc-800 bg-zinc-950/60 text-zinc-400 hover:border-zinc-600'"
                @click="togglePick(d.id)"
              >
                <Database class="h-3.5 w-3.5 shrink-0 text-sky-400" />
                <span class="min-w-0 flex-1 truncate">{{ d.name }}</span>
                <span class="shrink-0 text-[10px] text-zinc-500">{{ d.row_count }} rows · {{ d.schema_json.length }} cols</span>
              </button>
            </div>
          </template>
          <p v-else class="mt-3 rounded-xl border border-zinc-800 bg-zinc-950/60 px-3 py-2.5 text-[11px] text-zinc-500">
            Blank-first: design the board now, add components bound to datasets in the builder.
          </p>

          <input
            v-model="crName"
            placeholder="Dashboard name"
            class="mt-3 w-full rounded-xl border border-zinc-800 bg-zinc-950/60 px-3 py-2 text-sm outline-none focus:border-cyan-500/60"
          />
          <input
            v-model="crDesc"
            placeholder="Description (optional)"
            class="mt-2 w-full rounded-xl border border-zinc-800 bg-zinc-950/60 px-3 py-2 text-sm outline-none focus:border-cyan-500/60"
          />
          <button
            class="mt-4 flex w-full items-center justify-center gap-1.5 rounded-xl bg-cyan-500 py-2 text-sm font-semibold text-white transition hover:bg-cyan-400 disabled:opacity-40"
            :disabled="!crName.trim() || creating || (crFromDataset && crPicked.length === 0)"
            @click="doCreate"
          >
            <Loader2 v-if="creating" class="h-4 w-4 animate-spin" />
            {{ crFromDataset ? 'Generate dashboard' : 'Create blank dashboard' }}
          </button>
        </div>
      </div>
    </Teleport>
  </div>
</template>

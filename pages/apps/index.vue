<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import {
  LayoutGrid, Plus, Trash2, Loader2, Search, ExternalLink, Settings2,
  Database, X, Rocket, FileSpreadsheet, Square,
} from 'lucide-vue-next'
import { useApi } from '~/composables/useApi'

const { api } = useApi()

interface AppMeta {
  id: string
  name: string
  slug: string
  description: string
  dataset_id: string | null
  dataset_name: string | null
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
const apps = ref<AppMeta[]>([])
const datasets = ref<DatasetMeta[]>([])
const search = ref('')
const error = ref<string | null>(null)

// create modal
const showCreate = ref(false)
const creating = ref(false)
const crName = ref('')
const crDesc = ref('')
const crFromDataset = ref(true)
const crDatasetId = ref('')

const deleting = ref<string | null>(null)

async function load() {
  loading.value = true
  try {
    const [a, d] = await Promise.all([
      api.get<AppMeta[]>('/apps'),
      api.get<DatasetMeta[]>('/datasets'),
    ])
    apps.value = a
    datasets.value = d
  } catch (e: any) {
    error.value = e?.data?.detail || e?.message || 'Failed to load apps'
  } finally {
    loading.value = false
  }
}
onMounted(load)

const filtered = computed(() => {
  const q = search.value.trim().toLowerCase()
  if (!q) return apps.value
  return apps.value.filter(
    (a) => a.name.toLowerCase().includes(q) || (a.description || '').toLowerCase().includes(q),
  )
})

const stats = computed(() => ({
  total: apps.value.length,
  published: apps.value.filter((a) => a.status === 'published').length,
}))

async function doCreate() {
  if (!crName.value.trim()) return
  if (crFromDataset.value && !crDatasetId.value) return
  creating.value = true
  error.value = null
  try {
    const created = await api.post<any>('/apps', {
      name: crName.value.trim(),
      description: crDesc.value.trim(),
      dataset_id: crFromDataset.value ? crDatasetId.value : null,
      generate: true,
    })
    showCreate.value = false
    navigateTo(`/apps/${created.id}`)
  } catch (e: any) {
    error.value = e?.data?.detail || e?.message || 'Create failed'
  } finally {
    creating.value = false
  }
}

function pickDataset(id: string) {
  crDatasetId.value = id
  if (!crName.value.trim()) {
    const ds = datasets.value.find((d) => d.id === id)
    if (ds) crName.value = `${ds.name} App`
  }
}

async function remove(a: AppMeta) {
  if (!confirm(`Delete app "${a.name}"? The bound dataset is NOT deleted.`)) return
  deleting.value = a.id
  try {
    await api.del(`/apps/${a.id}`)
    apps.value = apps.value.filter((r) => r.id !== a.id)
  } catch (e: any) {
    alert(e?.data?.detail || e?.message || 'Delete failed')
  } finally {
    deleting.value = null
  }
}

function compCount(a: AppMeta) {
  return (a.config?.components || []).length
}

function fmtDate(iso: string | null) {
  if (!iso) return '—'
  return new Date(iso).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}
</script>

<template>
  <div class="pb-10 text-zinc-100">
    <header class="sticky top-0 z-20 border-b border-zinc-800/80 bg-zinc-950/90 backdrop-blur">
      <div class="mx-auto flex max-w-6xl items-center gap-3 px-4 py-3.5 lg:px-6">
        <span class="flex h-9 w-9 items-center justify-center rounded-xl bg-violet-500/15">
          <LayoutGrid class="h-4 w-4 text-violet-400" />
        </span>
        <div class="min-w-0 flex-1">
          <h1 class="truncate text-base font-bold leading-tight">Apps</h1>
          <p class="text-xs text-zinc-500">Excel → App — turn a dataset into a usable CRM, tracker or dashboard</p>
        </div>
        <button
          class="flex items-center gap-1.5 rounded-lg bg-violet-500 px-3 py-1.5 text-xs font-semibold text-white shadow-lg shadow-violet-500/20 transition hover:bg-violet-400"
          @click="showCreate = true; crFromDataset = datasets.length > 0"
        >
          <Plus class="h-3.5 w-3.5" /> New app
        </button>
      </div>
    </header>

    <div class="mx-auto max-w-6xl px-4 lg:px-6">
      <!-- stats + search -->
      <div class="mt-5 flex flex-wrap items-center gap-3">
        <div class="flex items-center gap-4 rounded-xl border border-zinc-800/80 bg-zinc-900/40 px-4 py-2.5 text-sm">
          <span class="flex items-center gap-1.5 text-zinc-400"><LayoutGrid class="h-3.5 w-3.5 text-violet-400" /> <b class="text-zinc-100">{{ stats.total }}</b> apps</span>
          <span class="flex items-center gap-1.5 text-zinc-400"><Rocket class="h-3.5 w-3.5 text-emerald-400" /> <b class="text-zinc-100">{{ stats.published }}</b> published</span>
        </div>
        <div class="relative min-w-[220px] flex-1">
          <Search class="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-zinc-500" />
          <input
            v-model="search"
            placeholder="Search apps…"
            class="w-full rounded-xl border border-zinc-800 bg-zinc-900/60 py-2 pl-9 pr-3 text-sm text-zinc-200 placeholder-zinc-500 outline-none transition focus:border-violet-500/60"
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
          <LayoutGrid class="h-6 w-6 text-zinc-600" />
        </span>
        <p class="mt-4 text-sm font-medium text-zinc-300">No apps yet</p>
        <p class="mx-auto mt-1 max-w-md text-xs text-zinc-500">Upload an Excel workbook as a dataset, then generate an app over it in one click — table, stats, chart and a create form, ready to publish.</p>
        <button class="mt-4 inline-flex items-center gap-1.5 rounded-lg bg-violet-500 px-3 py-1.5 text-xs font-semibold text-white hover:bg-violet-400" @click="showCreate = true; crFromDataset = datasets.length > 0">
          <Plus class="h-3.5 w-3.5" /> New app
        </button>
      </div>
      <div v-else class="mt-5 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        <div
          v-for="a in filtered"
          :key="a.id"
          class="group relative cursor-pointer rounded-2xl border border-zinc-800/80 bg-zinc-900/40 p-4 transition hover:border-violet-500/40 hover:bg-zinc-900/70"
          @click="navigateTo(`/apps/${a.id}`)"
        >
          <div class="flex items-start gap-3">
            <span class="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-violet-500/10">
              <Rocket v-if="a.status === 'published'" class="h-4 w-4 text-emerald-400" />
              <Settings2 v-else class="h-4 w-4 text-violet-400" />
            </span>
            <div class="min-w-0 flex-1">
              <p class="truncate text-sm font-semibold text-zinc-100 group-hover:text-violet-300">{{ a.name }}</p>
              <p class="mt-0.5 line-clamp-1 text-xs text-zinc-500">{{ a.description || 'No description' }}</p>
            </div>
            <span
              class="rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase"
              :class="a.status === 'published' ? 'bg-emerald-500/15 text-emerald-400' : 'bg-amber-500/15 text-amber-400'"
            >{{ a.status }}</span>
          </div>
          <div class="mt-3 flex items-center gap-3 text-[11px] text-zinc-500">
            <span class="flex min-w-0 items-center gap-1">
              <Database class="h-3 w-3 shrink-0 text-sky-400" />
              <span class="truncate">{{ a.dataset_name || 'no dataset' }}</span>
            </span>
            <span><b class="text-zinc-300">{{ compCount(a) }}</b> components</span>
            <span class="ml-auto">{{ fmtDate(a.updated_at) }}</span>
          </div>
          <div class="mt-3 flex items-center gap-2">
            <button
              v-if="a.status === 'published'"
              class="flex items-center gap-1 rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-2 py-1 text-[11px] font-medium text-emerald-400 transition hover:bg-emerald-500/20"
              @click.stop="navigateTo(`/run/${a.slug}`)"
            >
              <ExternalLink class="h-3 w-3" /> Open /run/{{ a.slug }}
            </button>
            <span v-else class="flex items-center gap-1 text-[11px] text-zinc-600"><Square class="h-2.5 w-2.5" /> draft — publish in builder</span>
          </div>
          <button
            class="absolute right-3 top-3 rounded-lg p-1.5 text-zinc-600 opacity-0 transition hover:bg-amber-500/10 hover:text-amber-400 group-hover:opacity-100"
            title="Delete app"
            @click.stop="remove(a)"
          >
            <Loader2 v-if="deleting === a.id" class="h-3.5 w-3.5 animate-spin" />
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
            <h2 class="text-sm font-bold">New app</h2>
            <button class="rounded-lg p-1 text-zinc-500 hover:text-zinc-200" @click="showCreate = false"><X class="h-4 w-4" /></button>
          </div>

          <div class="mt-4 grid grid-cols-2 gap-2">
            <button
              class="flex items-center justify-center gap-1.5 rounded-xl border px-3 py-2.5 text-xs font-medium transition"
              :class="crFromDataset ? 'border-violet-500/60 bg-violet-500/10 text-violet-300' : 'border-zinc-800 bg-zinc-950/60 text-zinc-400 hover:border-zinc-600'"
              :disabled="!datasets.length"
              @click="crFromDataset = true"
            >
              <FileSpreadsheet class="h-3.5 w-3.5" /> From dataset
            </button>
            <button
              class="flex items-center justify-center gap-1.5 rounded-xl border px-3 py-2.5 text-xs font-medium transition"
              :class="!crFromDataset ? 'border-violet-500/60 bg-violet-500/10 text-violet-300' : 'border-zinc-800 bg-zinc-950/60 text-zinc-400 hover:border-zinc-600'"
              @click="crFromDataset = false"
            >
              <Square class="h-3.5 w-3.5" /> Blank first
            </button>
          </div>

          <template v-if="crFromDataset">
            <select
              :value="crDatasetId"
              class="mt-3 w-full rounded-xl border border-zinc-800 bg-zinc-950/60 px-3 py-2 text-sm outline-none focus:border-violet-500/60"
              @change="pickDataset(($event.target as HTMLSelectElement).value)"
            >
              <option value="" disabled>Pick a dataset…</option>
              <option v-for="d in datasets" :key="d.id" :value="d.id">
                {{ d.name }} ({{ d.row_count }} rows, {{ d.schema_json.length }} cols)
              </option>
            </select>
            <p class="mt-1.5 text-[11px] text-zinc-600">Components are auto-laid-out from the data — stats, chart, table and form.</p>
          </template>
          <p v-else class="mt-3 rounded-xl border border-zinc-800 bg-zinc-950/60 px-3 py-2.5 text-[11px] text-zinc-500">
            Blank-first: design the app now, bind a dataset later in the builder.
          </p>

          <input
            v-model="crName"
            placeholder="App name"
            class="mt-3 w-full rounded-xl border border-zinc-800 bg-zinc-950/60 px-3 py-2 text-sm outline-none focus:border-violet-500/60"
          />
          <input
            v-model="crDesc"
            placeholder="Description (optional)"
            class="mt-2 w-full rounded-xl border border-zinc-800 bg-zinc-950/60 px-3 py-2 text-sm outline-none focus:border-violet-500/60"
          />
          <button
            class="mt-4 flex w-full items-center justify-center gap-1.5 rounded-xl bg-violet-500 py-2 text-sm font-semibold text-white transition hover:bg-violet-400 disabled:opacity-40"
            :disabled="!crName.trim() || creating || (crFromDataset && !crDatasetId)"
            @click="doCreate"
          >
            <Loader2 v-if="creating" class="h-4 w-4 animate-spin" />
            {{ crFromDataset ? 'Generate app' : 'Create blank app' }}
          </button>
        </div>
      </div>
    </Teleport>
  </div>
</template>

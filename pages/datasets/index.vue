<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import {
  Database, Upload, Plus, Trash2, Loader2, Search, ArrowRight,
  FileSpreadsheet, Braces, Rows3, X,
} from 'lucide-vue-next'
import { useApi } from '~/composables/useApi'

const { api } = useApi()

interface DatasetMeta {
  id: string
  name: string
  description: string
  schema_json: { name: string; dtype: string }[]
  row_count: number
  source: string
  created_at: string | null
  updated_at: string | null
}

const loading = ref(true)
const rows = ref<DatasetMeta[]>([])
const search = ref('')
const error = ref<string | null>(null)

// upload modal
const showUpload = ref(false)
const uploading = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)
const upName = ref('')
const upDesc = ref('')
const upFile = ref<File | null>(null)

// json create modal
const showCreate = ref(false)
const creating = ref(false)
const crName = ref('')
const crDesc = ref('')
const crRows = ref('[\n  { "name": "Ada", "age": 36 },\n  { "name": "Grace", "age": 45 }\n]')

const deleting = ref<string | null>(null)

async function load() {
  loading.value = true
  try {
    rows.value = await api.get<DatasetMeta[]>('/datasets')
  } catch (e: any) {
    error.value = e?.data?.detail || e?.message || 'Failed to load datasets'
  } finally {
    loading.value = false
  }
}
onMounted(load)

const filtered = computed(() => {
  const q = search.value.trim().toLowerCase()
  if (!q) return rows.value
  return rows.value.filter(
    (d) => d.name.toLowerCase().includes(q) || (d.description || '').toLowerCase().includes(q),
  )
})

const stats = computed(() => ({
  datasets: rows.value.length,
  rows: rows.value.reduce((acc: number, d) => acc + (d.row_count || 0), 0),
}))

function onPickFile(ev: Event) {
  const files = (ev.target as HTMLInputElement).files
  upFile.value = files && files.length ? files[0] : null
  if (upFile.value && !upName.value) {
    upName.value = upFile.value.name.replace(/\.[^.]+$/, '').replace(/[^\w .-]/g, '')
  }
}

function onDrop(ev: DragEvent) {
  const dt = ev.dataTransfer
  if (dt?.files?.length) {
    upFile.value = dt.files[0]
    if (!upName.value) upName.value = upFile.value.name.replace(/\.[^.]+$/, '').replace(/[^\w .-]/g, '')
  }
}

async function doUpload() {
  if (!upFile.value || !upName.value.trim()) return
  uploading.value = true
  error.value = null
  try {
    // multipart: let the browser set the content-type boundary — bypass the
    // json content-type that useApi.request injects
    const config = useRuntimeConfig()
    const mode = (config.public.gatewayMode as string) || 'gateway'
    const apiPort = (config.public.apiPort as string) || '8000'
    const url = mode === 'gateway'
      ? `/api/v1/datasets/upload?XTransformPort=${apiPort}`
      : '/api/v1/datasets/upload'
    const form = new FormData()
    form.append('file', upFile.value)
    form.append('name', upName.value.trim())
    form.append('description', upDesc.value.trim())
    const created = await $fetch<any>(url, { method: 'POST', body: form })
    showUpload.value = false
    navigateTo(`/datasets/${created.id}`)
  } catch (e: any) {
    error.value = e?.data?.detail || e?.message || 'Upload failed'
  } finally {
    uploading.value = false
  }
}

async function doCreate() {
  if (!crName.value.trim()) return
  creating.value = true
  error.value = null
  try {
    let parsed: any[] = []
    try {
      parsed = JSON.parse(crRows.value || '[]')
    } catch {
      throw new Error('Rows must be a valid JSON array of objects')
    }
    const created = await api.post<any>('/datasets', {
      name: crName.value.trim(),
      description: crDesc.value.trim(),
      rows: parsed,
    })
    showCreate.value = false
    navigateTo(`/datasets/${created.id}`)
  } catch (e: any) {
    error.value = e?.data?.detail || e?.message || 'Create failed'
  } finally {
    creating.value = false
  }
}

async function remove(d: DatasetMeta) {
  if (!confirm(`Delete dataset "${d.name}" and its ${d.row_count} rows? Workflows reading it will error.`)) return
  deleting.value = d.id
  try {
    await api.del(`/datasets/${d.id}`)
    rows.value = rows.value.filter((r) => r.id !== d.id)
  } catch (e: any) {
    alert(e?.data?.detail || e?.message || 'Delete failed')
  } finally {
    deleting.value = null
  }
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
        <span class="flex h-9 w-9 items-center justify-center rounded-xl bg-sky-500/15">
          <Database class="h-4 w-4 text-sky-400" />
        </span>
        <div class="min-w-0 flex-1">
          <h1 class="truncate text-base font-bold leading-tight">Datasets</h1>
          <p class="text-xs text-zinc-500">First-class tabular data — upload Excel/CSV, query with SQL, feed workflows</p>
        </div>
        <button
          class="hidden items-center gap-1.5 rounded-lg border border-zinc-800 bg-zinc-900/60 px-3 py-1.5 text-xs font-medium text-zinc-300 transition hover:border-zinc-600 sm:flex"
          @click="showCreate = true"
        >
          <Braces class="h-3.5 w-3.5" /> New JSON
        </button>
        <button
          class="flex items-center gap-1.5 rounded-lg bg-sky-500 px-3 py-1.5 text-xs font-semibold text-white shadow-lg shadow-sky-500/20 transition hover:bg-sky-400"
          @click="showUpload = true"
        >
          <Upload class="h-3.5 w-3.5" /> Upload data
        </button>
      </div>
    </header>

    <div class="mx-auto max-w-6xl px-4 lg:px-6">
      <!-- stats + search -->
      <div class="mt-5 flex flex-wrap items-center gap-3">
        <div class="flex items-center gap-4 rounded-xl border border-zinc-800/80 bg-zinc-900/40 px-4 py-2.5 text-sm">
          <span class="flex items-center gap-1.5 text-zinc-400"><Database class="h-3.5 w-3.5 text-sky-400" /> <b class="text-zinc-100">{{ stats.datasets }}</b> datasets</span>
          <span class="flex items-center gap-1.5 text-zinc-400"><Rows3 class="h-3.5 w-3.5 text-emerald-400" /> <b class="text-zinc-100">{{ stats.rows.toLocaleString() }}</b> rows</span>
        </div>
        <div class="relative min-w-[220px] flex-1">
          <Search class="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-zinc-500" />
          <input
            v-model="search"
            placeholder="Search datasets…"
            class="w-full rounded-xl border border-zinc-800 bg-zinc-900/60 py-2 pl-9 pr-3 text-sm text-zinc-200 placeholder-zinc-500 outline-none transition focus:border-sky-500/60"
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
          <Database class="h-6 w-6 text-zinc-600" />
        </span>
        <p class="mt-4 text-sm font-medium text-zinc-300">No datasets yet</p>
        <p class="mt-1 text-xs text-zinc-500">Upload an Excel workbook or CSV, or create one from JSON — then read, write and query it from any workflow.</p>
        <button class="mt-4 inline-flex items-center gap-1.5 rounded-lg bg-sky-500 px-3 py-1.5 text-xs font-semibold text-white hover:bg-sky-400" @click="showUpload = true">
          <Upload class="h-3.5 w-3.5" /> Upload data
        </button>
      </div>
      <div v-else class="mt-5 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        <NuxtLink
          v-for="d in filtered"
          :key="d.id"
          :to="`/datasets/${d.id}`"
          class="group relative rounded-2xl border border-zinc-800/80 bg-zinc-900/40 p-4 transition hover:border-sky-500/40 hover:bg-zinc-900/70"
        >
          <div class="flex items-start gap-3">
            <span class="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-sky-500/10">
              <FileSpreadsheet v-if="d.source === 'upload'" class="h-4 w-4 text-sky-400" />
              <Braces v-else-if="d.source === 'api'" class="h-4 w-4 text-sky-400" />
              <Database v-else class="h-4 w-4 text-emerald-400" />
            </span>
            <div class="min-w-0 flex-1">
              <p class="truncate text-sm font-semibold text-zinc-100 group-hover:text-sky-300">{{ d.name }}</p>
              <p class="mt-0.5 line-clamp-1 text-xs text-zinc-500">{{ d.description || 'No description' }}</p>
            </div>
          </div>
          <div class="mt-3 flex items-center gap-3 text-[11px] text-zinc-500">
            <span><b class="text-zinc-300">{{ d.row_count.toLocaleString() }}</b> rows</span>
            <span><b class="text-zinc-300">{{ d.schema_json.length }}</b> cols</span>
            <span class="uppercase">{{ d.source }}</span>
            <span class="ml-auto">{{ fmtDate(d.updated_at) }}</span>
          </div>
          <button
            class="absolute right-3 top-3 rounded-lg p-1.5 text-zinc-600 opacity-0 transition hover:bg-amber-500/10 hover:text-amber-400 group-hover:opacity-100"
            title="Delete dataset"
            @click.prevent="remove(d)"
          >
            <Loader2 v-if="deleting === d.id" class="h-3.5 w-3.5 animate-spin" />
            <Trash2 v-else class="h-3.5 w-3.5" />
          </button>
          <ArrowRight class="absolute bottom-4 right-4 h-3.5 w-3.5 text-zinc-700 transition group-hover:text-sky-400" />
        </NuxtLink>
      </div>
    </div>

    <!-- upload modal -->
    <Teleport to="body">
      <div v-if="showUpload" class="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4 backdrop-blur-sm" @click.self="showUpload = false">
        <div class="w-full max-w-md rounded-2xl border border-zinc-800 bg-zinc-900 p-5 shadow-2xl">
          <div class="flex items-center justify-between">
            <h2 class="text-sm font-bold">Upload a data file</h2>
            <button class="rounded-lg p-1 text-zinc-500 hover:text-zinc-200" @click="showUpload = false"><X class="h-4 w-4" /></button>
          </div>
          <label
            class="mt-4 flex cursor-pointer flex-col items-center justify-center gap-2 rounded-xl border border-dashed border-zinc-700 bg-zinc-950/50 px-4 py-8 text-center transition hover:border-sky-500/50"
            @dragover.prevent
            @drop.prevent="onDrop"
          >
            <Upload class="h-5 w-5 text-sky-400" />
            <span class="text-xs text-zinc-400">{{ upFile ? upFile.name : 'Click or drop .xlsx / .csv / .json' }}</span>
            <span class="text-[10px] text-zinc-600">First sheet is used · max 25 MB</span>
            <input ref="fileInput" type="file" accept=".xlsx,.xls,.csv,.json,.txt" class="hidden" @change="onPickFile" />
          </label>
          <input
            v-model="upName"
            placeholder="Dataset name"
            class="mt-3 w-full rounded-xl border border-zinc-800 bg-zinc-950/60 px-3 py-2 text-sm outline-none focus:border-sky-500/60"
          />
          <input
            v-model="upDesc"
            placeholder="Description (optional)"
            class="mt-2 w-full rounded-xl border border-zinc-800 bg-zinc-950/60 px-3 py-2 text-sm outline-none focus:border-sky-500/60"
          />
          <button
            class="mt-4 flex w-full items-center justify-center gap-1.5 rounded-xl bg-sky-500 py-2 text-sm font-semibold text-white transition hover:bg-sky-400 disabled:opacity-40"
            :disabled="!upFile || !upName.trim() || uploading"
            @click="doUpload"
          >
            <Loader2 v-if="uploading" class="h-4 w-4 animate-spin" />
            Create dataset
          </button>
        </div>
      </div>

      <!-- json create modal -->
      <div v-if="showCreate" class="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4 backdrop-blur-sm" @click.self="showCreate = false">
        <div class="w-full max-w-lg rounded-2xl border border-zinc-800 bg-zinc-900 p-5 shadow-2xl">
          <div class="flex items-center justify-between">
            <h2 class="text-sm font-bold">New dataset from JSON</h2>
            <button class="rounded-lg p-1 text-zinc-500 hover:text-zinc-200" @click="showCreate = false"><X class="h-4 w-4" /></button>
          </div>
          <input
            v-model="crName"
            placeholder="Dataset name"
            class="mt-4 w-full rounded-xl border border-zinc-800 bg-zinc-950/60 px-3 py-2 text-sm outline-none focus:border-sky-500/60"
          />
          <input
            v-model="crDesc"
            placeholder="Description (optional)"
            class="mt-2 w-full rounded-xl border border-zinc-800 bg-zinc-950/60 px-3 py-2 text-sm outline-none focus:border-sky-500/60"
          />
          <textarea
            v-model="crRows"
            rows="8"
            spellcheck="false"
            class="mt-2 w-full rounded-xl border border-zinc-800 bg-zinc-950/60 px-3 py-2 font-mono text-xs text-zinc-300 outline-none focus:border-sky-500/60"
          />
          <button
            class="mt-3 flex w-full items-center justify-center gap-1.5 rounded-xl bg-sky-500 py-2 text-sm font-semibold text-white transition hover:bg-sky-400 disabled:opacity-40"
            :disabled="!crName.trim() || creating"
            @click="doCreate"
          >
            <Loader2 v-if="creating" class="h-4 w-4 animate-spin" />
            Create dataset
          </button>
        </div>
      </div>
    </Teleport>
  </div>
</template>

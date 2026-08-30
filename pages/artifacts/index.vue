<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import {
  Image as ImageIcon, Trash2, Loader2, Search, Network, BarChart3, FileCode2, X, ExternalLink,
} from 'lucide-vue-next'
import { useApi } from '~/composables/useApi'

const { api, srcUrl } = useApi()

interface ArtifactMeta {
  id: string
  kind: string
  filename: string
  content_type: string
  size_bytes: number
  meta: Record<string, any>
  workflow_id: string | null
  execution_id: string | null
  created_at: string | null
  url: string
}

const loading = ref(true)
const rows = ref<ArtifactMeta[]>([])
const search = ref('')
const error = ref<string | null>(null)
const deleting = ref<string | null>(null)
const preview = ref<ArtifactMeta | null>(null)

async function load() {
  loading.value = true
  try {
    rows.value = await api.get<ArtifactMeta[]>('/artifacts')
  } catch (e: any) {
    error.value = e?.data?.detail || e?.message || 'Failed to load artifacts'
  } finally {
    loading.value = false
  }
}
onMounted(load)

const filtered = computed(() => {
  const q = search.value.trim().toLowerCase()
  if (!q) return rows.value
  return rows.value.filter((a) =>
    (a.meta?.title || '').toLowerCase().includes(q)
    || a.kind.includes(q)
    || (a.meta?.model || '').toLowerCase().includes(q)
    || (a.meta?.chart_type || '').toLowerCase().includes(q),
  )
})

const stats = computed(() => ({
  total: rows.value.length,
  charts: rows.value.filter((a) => a.kind === 'chart').length,
  models: rows.value.filter((a) => a.kind === 'model').length,
}))

async function remove(a: ArtifactMeta) {
  if (!confirm(`Delete ${a.kind} artifact (${a.filename})?`)) return
  deleting.value = a.id
  try {
    await api.del(`/artifacts/${a.id}`)
    rows.value = rows.value.filter((r) => r.id !== a.id)
  } catch (e: any) {
    alert(e?.data?.detail || e?.message || 'Delete failed')
  } finally {
    deleting.value = null
  }
}

function fmtSize(n: number) {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
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
        <span class="flex h-9 w-9 items-center justify-center rounded-xl bg-orange-500/15">
          <ImageIcon class="h-4 w-4 text-orange-400" />
        </span>
        <div class="min-w-0 flex-1">
          <h1 class="truncate text-base font-bold leading-tight">Artifacts</h1>
          <p class="text-xs text-zinc-500">Charts, models and files produced by your workflow runs</p>
        </div>
      </div>
    </header>

    <div class="mx-auto max-w-6xl px-4 lg:px-6">
      <div class="mt-5 flex flex-wrap items-center gap-3">
        <div class="flex items-center gap-4 rounded-xl border border-zinc-800/80 bg-zinc-900/40 px-4 py-2.5 text-sm">
          <span class="flex items-center gap-1.5 text-zinc-400"><BarChart3 class="h-3.5 w-3.5 text-orange-400" /> <b class="text-zinc-100">{{ stats.charts }}</b> charts</span>
          <span class="flex items-center gap-1.5 text-zinc-400"><Network class="h-3.5 w-3.5 text-indigo-400" /> <b class="text-zinc-100">{{ stats.models }}</b> models</span>
        </div>
        <div class="relative min-w-[220px] flex-1">
          <Search class="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-zinc-500" />
          <input
            v-model="search"
            placeholder="Search by title, kind, model…"
            class="w-full rounded-xl border border-zinc-800 bg-zinc-900/60 py-2 pl-9 pr-3 text-sm text-zinc-200 placeholder-zinc-500 outline-none transition focus:border-orange-500/50"
          />
        </div>
      </div>

      <p v-if="error" class="mt-4 rounded-xl border border-amber-500/30 bg-amber-500/10 px-4 py-2.5 text-sm text-amber-300">{{ error }}</p>

      <div v-if="loading" class="mt-10 flex justify-center text-zinc-500">
        <Loader2 class="h-6 w-6 animate-spin" />
      </div>
      <div v-else-if="filtered.length === 0" class="mt-16 text-center">
        <span class="mx-auto flex h-14 w-14 items-center justify-center rounded-2xl bg-zinc-900">
          <ImageIcon class="h-6 w-6 text-zinc-600" />
        </span>
        <p class="mt-4 text-sm font-medium text-zinc-300">No artifacts yet</p>
        <p class="mt-1 text-xs text-zinc-500">Add a Chart or Model Train node to a workflow — every run's outputs are collected here.</p>
      </div>
      <div v-else class="mt-5 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        <div
          v-for="a in filtered"
          :key="a.id"
          class="group relative cursor-pointer overflow-hidden rounded-2xl border border-zinc-800/80 bg-zinc-900/40 transition hover:border-orange-500/40"
          @click="preview = a"
        >
          <!-- chart thumbnail -->
          <div v-if="a.kind === 'chart'" class="flex h-36 items-center justify-center bg-white/95 px-2">
            <img :src="srcUrl(a.url)" :alt="a.meta?.title || a.filename" class="max-h-32 object-contain" />
          </div>
          <!-- model / file card -->
          <div v-else class="flex h-36 items-center justify-center bg-zinc-950/40">
            <span class="flex h-12 w-12 items-center justify-center rounded-2xl bg-indigo-500/15">
              <Network class="h-5 w-5 text-indigo-400" />
            </span>
          </div>
          <div class="border-t border-zinc-800/80 p-3">
            <p class="truncate text-xs font-semibold text-zinc-100">{{ a.meta?.title || a.meta?.model || a.filename }}</p>
            <p class="mt-1 flex items-center gap-2 text-[11px] text-zinc-500">
              <span class="rounded bg-zinc-800 px-1.5 py-0.5 uppercase">{{ a.kind }}</span>
              <span v-if="a.meta?.chart_type" class="rounded bg-zinc-800 px-1.5 py-0.5">{{ a.meta.chart_type }}</span>
              <span>{{ fmtSize(a.size_bytes) }}</span>
              <span class="ml-auto">{{ fmtDate(a.created_at) }}</span>
            </p>
          </div>
          <button
            class="absolute right-2.5 top-2.5 rounded-lg bg-zinc-950/70 p-1.5 text-zinc-500 opacity-0 transition hover:text-amber-400 group-hover:opacity-100"
            title="Delete artifact"
            @click.stop="remove(a)"
          >
            <Loader2 v-if="deleting === a.id" class="h-3.5 w-3.5 animate-spin" />
            <Trash2 v-else class="h-3.5 w-3.5" />
          </button>
        </div>
      </div>
    </div>

    <!-- preview modal -->
    <Teleport to="body">
      <div v-if="preview" class="fixed inset-0 z-50 flex items-center justify-center bg-black/80 p-4 backdrop-blur-sm" @click.self="preview = null">
        <div class="max-h-[90vh] w-full max-w-3xl overflow-auto rounded-2xl border border-zinc-800 bg-zinc-900 p-4 shadow-2xl">
          <div class="mb-3 flex items-center gap-3">
            <div class="min-w-0 flex-1">
              <p class="truncate text-sm font-bold">{{ preview.meta?.title || preview.meta?.model || preview.filename }}</p>
              <p class="text-[11px] text-zinc-500">
                {{ preview.kind }} · {{ fmtSize(preview.size_bytes) }} · {{ fmtDate(preview.created_at) }}
                <template v-if="preview.meta?.metrics">
                  · <span class="font-mono text-zinc-400">{{ Object.entries(preview.meta.metrics).map(([k, v]) => `${k}=${v}`).slice(0, 4).join(' · ') }}</span>
                </template>
              </p>
            </div>
            <a :href="srcUrl(preview.url)" target="_blank" class="rounded-lg border border-zinc-800 p-1.5 text-zinc-400 hover:text-zinc-100" title="Open raw">
              <ExternalLink class="h-4 w-4" />
            </a>
            <button class="rounded-lg p-1 text-zinc-500 hover:text-zinc-200" @click="preview = null"><X class="h-4 w-4" /></button>
          </div>
          <img v-if="preview.kind === 'chart'" :src="srcUrl(preview.url)" class="mx-auto max-h-[70vh] rounded-xl border border-zinc-800 bg-white" />
          <div v-else class="flex h-64 items-center justify-center rounded-xl border border-zinc-800 bg-zinc-950/60">
            <div class="text-center">
              <FileCode2 class="mx-auto h-8 w-8 text-indigo-400" />
              <p class="mt-2 font-mono text-xs text-zinc-400">{{ preview.filename }}</p>
              <p class="mt-1 text-[11px] text-zinc-600">Binary model artifact — load it in code via python_transform</p>
            </div>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

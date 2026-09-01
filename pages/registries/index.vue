<script setup lang="ts">
import { ref, onMounted } from 'vue'
import {
  Globe, Plus, Loader2, Trash2, RefreshCw, SearchCheck, CheckCircle2,
  XCircle, Workflow, Database, CloudDownload, AlertTriangle,
} from 'lucide-vue-next'
import { useApi } from '~/composables/useApi'

// v43: pack registries - point Py8n at a URL serving a py8n-pack and pull
// gallery packs from other instances, static hosts or CI artifacts.
interface Registry {
  id: string
  name: string
  url: string
  created_at: string | null
  last_sync_at: string | null
  last_status: string | null
  last_summary: Record<string, any> | null
}

interface PackPreview {
  url: string
  generated_at: string | null
  py8n_version: string | null
  workflow_count: number
  dataset_count: number
  workflows: { name: string; node_count: number; valid: boolean; error: string | null; exists: boolean }[]
  datasets: { name: string; rows: number; rename_to: string | null; invalid_name: boolean }[]
  warnings: string[]
}

interface SyncResult {
  registry: Registry
  import: {
    workflows: { id: string; name: string; node_count: number }[]
    datasets: { id: string; name: string; row_count: number }[]
    skipped: { name: string; reason: string }[]
    warnings: string[]
  }
}

const { api } = useApi()
const loading = ref(true)
const registries = ref<Registry[]>([])

const newName = ref('')
const newUrl = ref('')
const adding = ref(false)
const addError = ref('')

const checking = ref<string | null>(null)
const syncing = ref<string | null>(null)
const deleting = ref<string | null>(null)
const pageError = ref('')

const preview = ref<PackPreview | null>(null)
const syncResult = ref<SyncResult | null>(null)

async function loadRegistries() {
  loading.value = true
  try {
    registries.value = await api.get<Registry[]>('/registries')
  } finally {
    loading.value = false
  }
}

onMounted(loadRegistries)

async function addRegistry() {
  if (!newName.value.trim() || !newUrl.value.trim()) return
  adding.value = true
  addError.value = ''
  try {
    await api.post('/registries', { name: newName.value.trim(), url: newUrl.value.trim() })
    newName.value = ''
    newUrl.value = ''
    await loadRegistries()
  } catch (e: any) {
    addError.value = e?.data?.detail || e?.message || 'Could not add the registry'
  } finally {
    adding.value = false
  }
}

async function checkRegistry(reg: Registry) {
  checking.value = reg.id
  pageError.value = ''
  try {
    preview.value = await api.post<PackPreview>(`/registries/${reg.id}/check`)
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'Check failed'
  } finally {
    checking.value = null
  }
}

async function syncRegistry(reg: Registry) {
  syncing.value = reg.id
  pageError.value = ''
  try {
    syncResult.value = await api.post<SyncResult>(`/registries/${reg.id}/sync`)
    await loadRegistries()
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'Sync failed'
    await loadRegistries()
  } finally {
    syncing.value = null
  }
}

async function removeRegistry(reg: Registry) {
  if (!confirm(`Forget registry "${reg.name}"? Workflows and datasets already imported stay.`)) return
  deleting.value = reg.id
  try {
    await api.del(`/registries/${reg.id}`)
    await loadRegistries()
  } finally {
    deleting.value = null
  }
}

function summaryLine(reg: Registry): string {
  const s = reg.last_summary
  if (!s) return ''
  if (s.error) return s.error
  const parts: string[] = []
  if (typeof s.workflows_created === 'number') parts.push(`${s.workflows_created} workflow(s)`)
  if (typeof s.datasets_created === 'number') parts.push(`${s.datasets_created} dataset(s)`)
  if (Array.isArray(s.skipped) && s.skipped.length) parts.push(`${s.skipped.length} skipped`)
  return parts.join(' + ')
}

function fmtDate(iso: string | null) {
  if (!iso) return 'never'
  return new Date(iso).toLocaleString(undefined, {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
  })
}

function closePreview() { preview.value = null }
function closeSync() { syncResult.value = null }
</script>

<template>
  <div class="text-zinc-100">
    <header class="sticky top-0 z-20 border-b border-zinc-800/80 bg-zinc-950/90 backdrop-blur">
      <div class="mx-auto max-w-7xl px-4 py-3.5 sm:px-6">
        <div class="flex items-center gap-3">
          <div class="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-orange-500 to-rose-500 shadow-lg shadow-orange-500/20">
            <CloudDownload class="h-4 w-4 text-white" />
          </div>
          <div>
            <h1 class="text-lg font-bold tracking-tight">Pack registries</h1>
            <p class="-mt-0.5 text-[11px] text-zinc-500">Point Py8n at a URL to check and sync gallery packs</p>
          </div>
        </div>
      </div>
    </header>

    <main class="mx-auto max-w-7xl px-4 py-6 sm:px-6">
      <!-- how it works -->
      <div class="mb-6 flex items-start gap-3 rounded-2xl border border-zinc-800 bg-zinc-900/40 p-4">
        <Globe class="mt-0.5 h-4 w-4 shrink-0 text-orange-400" />
        <p class="text-xs leading-relaxed text-zinc-400">
          A registry is any URL serving a <code class="rounded bg-zinc-800 px-1 font-mono text-[11px] text-orange-300">py8n-pack</code>
          document - another instance's <code class="rounded bg-zinc-800 px-1 font-mono text-[11px] text-zinc-300">/api/v1/templates/gallery/pack</code>,
          a shared pack file on a static host, a CI artifact. <span class="text-zinc-200">Check</span> dry-runs the pack
          against your estate without writing anything; <span class="text-zinc-200">Sync</span> imports it through the
          ordinary pack pipeline (workflows arrive inactive, name collisions get numbered suffixes, broken entries are
          skipped with reasons).
        </p>
      </div>

      <!-- add form -->
      <div class="mb-6 rounded-2xl border border-zinc-800 bg-zinc-900/50 p-4">
        <div class="grid gap-3 sm:grid-cols-[220px_1fr_auto]">
          <input
            v-model="newName"
            class="rounded-xl border border-zinc-800 bg-zinc-950 px-3 py-2 text-sm outline-none transition placeholder:text-zinc-600 focus:border-orange-500/60"
            placeholder="Registry name"
            @keyup.enter="addRegistry"
          />
          <input
            v-model="newUrl"
            class="rounded-xl border border-zinc-800 bg-zinc-950 px-3 py-2 font-mono text-xs outline-none transition placeholder:text-zinc-600 focus:border-orange-500/60"
            placeholder="https://host/api/v1/templates/gallery/pack"
            @keyup.enter="addRegistry"
          />
          <button
            class="flex items-center justify-center gap-1.5 rounded-xl bg-orange-500 px-4 py-2 text-sm font-semibold text-white shadow-lg shadow-orange-500/20 transition hover:bg-orange-400 disabled:opacity-50"
            :disabled="adding || !newName.trim() || !newUrl.trim()"
            @click="addRegistry"
          >
            <Loader2 v-if="adding" class="h-4 w-4 animate-spin" />
            <Plus v-else class="h-4 w-4" />
            Add registry
          </button>
        </div>
        <p v-if="addError" class="mt-2.5 rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-xs text-rose-300">{{ addError }}</p>
      </div>

      <!-- page-level error -->
      <div v-if="pageError" class="mb-4 flex items-start gap-2 rounded-xl border border-rose-500/40 bg-rose-500/10 px-4 py-3 text-sm text-rose-300">
        <AlertTriangle class="mt-0.5 h-4 w-4 shrink-0" /> {{ pageError }}
      </div>

      <!-- list -->
      <div v-if="loading" class="grid place-items-center py-16 text-zinc-600">
        <Loader2 class="h-6 w-6 animate-spin" />
      </div>

      <div v-else-if="!registries.length" class="grid place-items-center rounded-2xl border border-dashed border-zinc-800 py-16 text-center">
        <Globe class="mb-3 h-8 w-8 text-zinc-700" />
        <p class="text-sm font-medium text-zinc-400">No registries yet</p>
        <p class="mt-1 max-w-md text-xs text-zinc-600">
          Add a URL above to pull readymade automations and dataset packs from another Py8n instance or any static host.
        </p>
      </div>

      <div v-else class="space-y-2.5">
        <div
          v-for="reg in registries"
          :key="reg.id"
          class="rounded-2xl border border-zinc-800/80 bg-zinc-900/50 p-4 transition hover:border-zinc-700"
        >
          <div class="flex flex-wrap items-center gap-3">
            <div class="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl border border-orange-500/30 bg-orange-500/10 text-orange-400">
              <Globe class="h-4 w-4" />
            </div>
            <div class="min-w-0 flex-1">
              <div class="flex flex-wrap items-center gap-2">
                <span class="truncate text-sm font-semibold">{{ reg.name }}</span>
                <span
                  v-if="reg.last_status"
                  class="rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide"
                  :class="reg.last_status === 'ok' ? 'bg-emerald-500/15 text-emerald-400' : 'bg-rose-500/15 text-rose-400'"
                >
                  last sync {{ reg.last_status }}
                </span>
                <span v-else class="rounded-full bg-zinc-800 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-zinc-500">
                  never synced
                </span>
              </div>
              <div class="mt-0.5 flex flex-wrap items-center gap-2 text-[11px] text-zinc-500">
                <span class="max-w-full truncate font-mono text-[10px]">{{ reg.url }}</span>
                <span>·</span>
                <span>synced {{ fmtDate(reg.last_sync_at) }}</span>
              </div>
              <p v-if="summaryLine(reg)" class="mt-1 truncate text-[11px]" :class="reg.last_status === 'error' ? 'text-rose-300' : 'text-zinc-400'">
                {{ summaryLine(reg) }}
              </p>
            </div>

            <div class="flex shrink-0 items-center gap-1.5">
              <button
                class="flex items-center gap-1.5 rounded-xl border border-zinc-800 bg-zinc-900 px-3 py-1.5 text-xs font-medium text-zinc-300 transition hover:border-cyan-500/40 hover:text-cyan-300 disabled:opacity-50"
                :disabled="checking === reg.id"
                title="Dry-run the remote pack: validity, collisions, renames. Nothing is written."
                @click="checkRegistry(reg)"
              >
                <Loader2 v-if="checking === reg.id" class="h-3.5 w-3.5 animate-spin" />
                <SearchCheck v-else class="h-3.5 w-3.5" />
                Check
              </button>
              <button
                class="flex items-center gap-1.5 rounded-xl bg-orange-500 px-3 py-1.5 text-xs font-semibold text-white shadow-lg shadow-orange-500/20 transition hover:bg-orange-400 disabled:opacity-50"
                :disabled="syncing === reg.id"
                title="Fetch and import the pack now"
                @click="syncRegistry(reg)"
              >
                <Loader2 v-if="syncing === reg.id" class="h-3.5 w-3.5 animate-spin" />
                <RefreshCw v-else class="h-3.5 w-3.5" />
                Sync now
              </button>
              <button
                class="rounded-xl border border-zinc-800 bg-zinc-900 p-2 text-zinc-400 transition hover:border-rose-500/40 hover:text-rose-300 disabled:opacity-50"
                :disabled="deleting === reg.id"
                title="Forget this registry"
                @click="removeRegistry(reg)"
              >
                <Loader2 v-if="deleting === reg.id" class="h-3.5 w-3.5 animate-spin" />
                <Trash2 v-else class="h-3.5 w-3.5" />
              </button>
            </div>
          </div>
        </div>
      </div>
    </main>

    <!-- check preview modal -->
    <Teleport to="body">
      <div
        v-if="preview"
        class="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4 backdrop-blur-sm"
        @click.self="closePreview"
      >
        <div class="max-h-[85vh] w-full max-w-2xl overflow-y-auto rounded-2xl border border-zinc-800 bg-zinc-950 shadow-2xl">
          <div class="sticky top-0 border-b border-zinc-800/80 bg-zinc-950 px-5 py-4">
            <h2 class="flex items-center gap-2 text-sm font-bold">
              <SearchCheck class="h-4 w-4 text-cyan-400" />
              Pack preview (dry run)
            </h2>
            <p class="mt-0.5 truncate text-[11px] text-zinc-500">
              {{ preview.url }}
              <span v-if="preview.py8n_version"> · built on Py8n {{ preview.py8n_version }}</span>
            </p>
          </div>

          <div class="space-y-4 px-5 py-4">
            <div class="flex gap-4 text-xs text-zinc-400">
              <span class="flex items-center gap-1.5"><Workflow class="h-3.5 w-3.5 text-orange-400" /> {{ preview.workflow_count }} workflow(s)</span>
              <span class="flex items-center gap-1.5"><Database class="h-3.5 w-3.5 text-emerald-400" /> {{ preview.dataset_count }} dataset(s)</span>
            </div>

            <div v-if="preview.workflows.length" class="space-y-1.5">
              <div class="text-[10px] font-semibold uppercase tracking-wider text-zinc-500">Workflows</div>
              <div v-for="w in preview.workflows" :key="w.name" class="flex items-center gap-2 rounded-xl border border-zinc-800 bg-zinc-900/60 px-3 py-2 text-xs">
                <component :is="w.valid ? CheckCircle2 : XCircle" class="h-3.5 w-3.5 shrink-0" :class="w.valid ? 'text-emerald-400' : 'text-rose-400'" />
                <span class="min-w-0 flex-1 truncate">{{ w.name }}</span>
                <span class="shrink-0 text-zinc-500">{{ w.node_count }} nodes</span>
                <span v-if="w.exists" class="shrink-0 rounded-md bg-amber-500/10 px-1.5 py-0.5 text-[10px] text-amber-400">already exists</span>
                <span v-if="!w.valid" class="shrink-0 rounded-md bg-rose-500/10 px-1.5 py-0.5 text-[10px] text-rose-400" :title="w.error || ''">invalid</span>
              </div>
            </div>

            <div v-if="preview.datasets.length" class="space-y-1.5">
              <div class="text-[10px] font-semibold uppercase tracking-wider text-zinc-500">Datasets</div>
              <div v-for="d in preview.datasets" :key="d.name" class="flex items-center gap-2 rounded-xl border border-zinc-800 bg-zinc-900/60 px-3 py-2 text-xs">
                <component :is="d.invalid_name ? XCircle : Database" class="h-3.5 w-3.5 shrink-0" :class="d.invalid_name ? 'text-rose-400' : 'text-emerald-400'" />
                <span class="min-w-0 flex-1 truncate">{{ d.name }}</span>
                <span class="shrink-0 text-zinc-500">{{ d.rows }} rows</span>
                <span v-if="d.rename_to" class="shrink-0 rounded-md bg-amber-500/10 px-1.5 py-0.5 text-[10px] text-amber-400">will import as "{{ d.rename_to }}"</span>
              </div>
            </div>

            <div v-if="preview.warnings.length" class="rounded-xl border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-300">
              <p v-for="(w, i) in preview.warnings" :key="i">{{ w }}</p>
            </div>
          </div>

          <div class="sticky bottom-0 flex justify-end gap-2 border-t border-zinc-800/80 bg-zinc-950 px-5 py-3.5">
            <button
              class="rounded-xl border border-zinc-800 px-3.5 py-2 text-sm font-medium text-zinc-400 transition hover:text-zinc-100"
              @click="closePreview"
            >
              Close
            </button>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- sync result modal -->
    <Teleport to="body">
      <div
        v-if="syncResult"
        class="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4 backdrop-blur-sm"
        @click.self="closeSync"
      >
        <div class="max-h-[85vh] w-full max-w-2xl overflow-y-auto rounded-2xl border border-zinc-800 bg-zinc-950 shadow-2xl">
          <div class="border-b border-zinc-800/80 px-5 py-4">
            <h2 class="flex items-center gap-2 text-sm font-bold">
              <CheckCircle2 class="h-4 w-4 text-emerald-400" />
              Sync complete
            </h2>
            <p class="mt-0.5 text-[11px] text-zinc-500">
              {{ syncResult.import.workflows.length }} workflow(s) and {{ syncResult.import.datasets.length }} dataset(s) created from {{ syncResult.registry.name }}
            </p>
          </div>

          <div class="space-y-4 px-5 py-4">
            <div v-if="syncResult.import.workflows.length" class="space-y-1.5">
              <div class="text-[10px] font-semibold uppercase tracking-wider text-zinc-500">Created workflows (inactive)</div>
              <div v-for="w in syncResult.import.workflows" :key="w.id" class="flex items-center gap-2 rounded-xl border border-zinc-800 bg-zinc-900/60 px-3 py-2 text-xs">
                <Workflow class="h-3.5 w-3.5 shrink-0 text-orange-400" />
                <span class="min-w-0 flex-1 truncate">{{ w.name }}</span>
                <span class="shrink-0 text-zinc-500">{{ w.node_count }} nodes</span>
              </div>
            </div>

            <div v-if="syncResult.import.datasets.length" class="space-y-1.5">
              <div class="text-[10px] font-semibold uppercase tracking-wider text-zinc-500">Created datasets</div>
              <div v-for="d in syncResult.import.datasets" :key="d.id" class="flex items-center gap-2 rounded-xl border border-zinc-800 bg-zinc-900/60 px-3 py-2 text-xs">
                <Database class="h-3.5 w-3.5 shrink-0 text-emerald-400" />
                <span class="min-w-0 flex-1 truncate">{{ d.name }}</span>
                <span class="shrink-0 text-zinc-500">{{ d.row_count }} rows</span>
              </div>
            </div>

            <div v-if="syncResult.import.skipped.length" class="space-y-1.5">
              <div class="text-[10px] font-semibold uppercase tracking-wider text-zinc-500">Skipped</div>
              <div v-for="(s, i) in syncResult.import.skipped" :key="i" class="flex items-center gap-2 rounded-xl border border-rose-500/30 bg-rose-500/5 px-3 py-2 text-xs">
                <XCircle class="h-3.5 w-3.5 shrink-0 text-rose-400" />
                <span class="min-w-0 flex-1 truncate">{{ s.name }}</span>
                <span class="shrink-0 text-rose-300">{{ s.reason }}</span>
              </div>
            </div>

            <div v-if="syncResult.import.warnings.length" class="rounded-xl border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-300">
              <p v-for="(w, i) in syncResult.import.warnings" :key="i">{{ w }}</p>
            </div>
          </div>

          <div class="sticky bottom-0 flex justify-end border-t border-zinc-800/80 bg-zinc-950 px-5 py-3.5">
            <button
              class="rounded-xl bg-orange-500 px-3.5 py-2 text-sm font-semibold text-white transition hover:bg-orange-400"
              @click="closeSync"
            >
              Done
            </button>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import {
  Workflow as WorkflowIcon, Play, Plus, CheckCircle2, XCircle, Check,
  Clock, Webhook, ChevronRight, Activity, Sparkles, Trash2, Upload, Loader2,
  ShieldAlert, Search, Tag as TagIcon, X, Folder as FolderIcon, FolderPlus,
  FolderInput, Pencil, Download,
} from 'lucide-vue-next'
import { useApi } from '~/composables/useApi'
import type { Workflow, WorkflowListItem, WorkflowScheduleInfo, ExecutionSummary, Folder } from '~/types/node'

const { api } = useApi()
const store = usePy8nStore()
const route = useRoute()

const workflows = ref<WorkflowListItem[]>([])
const recentRuns = ref<ExecutionSummary[]>([])
const folders = ref<Folder[]>([])
const loading = ref(true)
const creating = ref(false)
const importing = ref(false)
const showCreate = ref(false)
const newName = ref('')
const newDesc = ref('')
const newFolderId = ref('')
const importInput = ref<HTMLInputElement | null>(null)

// ------------------------------------------------------------------ v39 template packs
interface PackDatasetMeta { id: string; name: string; row_count: number }
interface PackInspection {
  workflow_count: number
  dataset_count: number
  workflows: { name: string; node_count: number; valid: boolean; error: string | null; exists: boolean }[]
  datasets: { name: string; rows: number; rename_to: string | null; invalid_name: boolean }[]
  warnings: string[]
}
interface PackImportSummary {
  workflows: { id: string; name: string; node_count: number }[]
  datasets: { id: string; name: string; row_count: number }[]
  skipped: { name: string; reason: string }[]
  warnings: string[]
}

const datasets = ref<PackDatasetMeta[]>([])
const packBusy = ref(false)
const showExportPack = ref(false)
const packWfSel = ref<Record<string, boolean>>({})
const packDsSel = ref<Record<string, boolean>>({})
const pendingPack = ref<Record<string, unknown> | null>(null)
const packPreview = ref<PackInspection | null>(null)
const packImporting = ref(false)
const packResult = ref<PackImportSummary | null>(null)

function downloadJson(doc: unknown, filename: string) {
  const blob = new Blob([JSON.stringify(doc, null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
}

function slugify(s: string) {
  return s.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '') || 'workflow'
}

async function exportOne(wf: WorkflowListItem) {
  packBusy.value = true
  try {
    const pack = await api.post<Record<string, unknown>>('/packs/export', { workflow_ids: [wf.id] })
    downloadJson(pack, `${slugify(wf.name)}.py8n.json`)
  } catch (e: any) {
    alert(e?.data?.detail || e?.message || 'Export failed')
  } finally {
    packBusy.value = false
  }
}

function openExportPack() {
  const wsel: Record<string, boolean> = {}
  for (const wf of workflows.value) wsel[wf.id] = true
  const dsel: Record<string, boolean> = {}
  for (const ds of datasets.value) dsel[ds.id] = true
  packWfSel.value = wsel
  packDsSel.value = dsel
  showExportPack.value = true
}

const packSelCounts = computed(() => {
  const w = Object.values(packWfSel.value).filter(Boolean).length
  const d = Object.values(packDsSel.value).filter(Boolean).length
  return { w, d, total: w + d }
})

async function downloadExportPack() {
  if (!packSelCounts.value.total) return
  packBusy.value = true
  try {
    const pack = await api.post<Record<string, unknown>>('/packs/export', {
      workflow_ids: Object.keys(packWfSel.value).filter((id) => packWfSel.value[id]),
      dataset_ids: Object.keys(packDsSel.value).filter((id) => packDsSel.value[id]),
    })
    const stamp = new Date().toISOString().slice(0, 16).replace(/[:T]/g, '-')
    downloadJson(pack, `py8n-pack-${stamp}.json`)
    showExportPack.value = false
  } catch (e: any) {
    alert(e?.data?.detail || e?.message || 'Export failed')
  } finally {
    packBusy.value = false
  }
}

async function confirmPackImport() {
  if (!pendingPack.value) return
  packImporting.value = true
  try {
    packResult.value = await api.post<PackImportSummary>('/packs/import', pendingPack.value)
    await loadAll()
  } catch (e: any) {
    alert(e?.data?.detail || e?.message || 'Pack import failed')
  } finally {
    packImporting.value = false
  }
}

function closePackPreview() {
  packPreview.value = null
  pendingPack.value = null
  packResult.value = null
}

async function loadAll() {
  loading.value = true
  try {
    workflows.value = await api.get<WorkflowListItem[]>('/workflows')
    recentRuns.value = await api.get<ExecutionSummary[]>('/executions?limit=8')
    folders.value = await api.get<Folder[]>('/folders')
    datasets.value = await api.get<PackDatasetMeta[]>('/datasets')
  } finally {
    loading.value = false
  }
}

async function createWorkflow() {
  if (!newName.value.trim()) return
  creating.value = true
  try {
    const wf = await api.post<Workflow>('/workflows', {
      name: newName.value.trim(),
      description: newDesc.value.trim(),
      folder_id: newFolderId.value || null,
      graph: {
        nodes: [
          {
            id: 'trigger_1',
            type: 'manual_trigger',
            name: 'Manual Trigger',
            position: { x: 0, y: 0 },
            parameters: { payload: {} },
          },
        ],
        edges: [],
      },
    })
    showCreate.value = false
    newName.value = ''
    newDesc.value = ''
    newFolderId.value = ''
    navigateTo(`/workflows/${wf.id}`)
  } finally {
    creating.value = false
  }
}

async function toggleActive(wf: WorkflowListItem) {
  try {
    const info = await api.post<WorkflowScheduleInfo>(
      `/workflows/${wf.id}/${wf.is_active ? 'deactivate' : 'activate'}`,
    )
    wf.is_active = info.is_active
    wf.next_run_at = info.next_run_at ?? null
  } catch (e: any) {
    alert(e?.data?.detail || e?.message || 'Activation failed')
  }
}

async function removeWorkflow(wf: WorkflowListItem) {
  if (!confirm(`Delete workflow "${wf.name}"? This cannot be undone.`)) return
  await api.del(`/workflows/${wf.id}`)
  await loadAll()
}

async function onImportFile(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = '' // allow re-selecting the same file
  if (!file) return
  importing.value = true
  try {
    const text = await file.text()
    const doc = JSON.parse(text)
    if (doc && doc.format === 'py8n-pack') {
      // v39: multi-resource pack - preview first, the dialog drives the import
      packPreview.value = await api.post<PackInspection>('/packs/inspect', doc)
      pendingPack.value = doc
      packResult.value = null
      return
    }
    const wf = await api.post<Workflow>('/workflows/import', { data: doc })
    navigateTo(`/workflows/${wf.id}`)
  } catch (e: any) {
    alert(e?.data?.detail || e?.message || 'Import failed - is this a Py8n export file?')
  } finally {
    importing.value = false
  }
}

function fmtDate(iso: string) {
  return new Date(iso).toLocaleString(undefined, {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
  })
}

function relTime(iso: string) {
  const diff = new Date(iso).getTime() - Date.now()
  if (diff <= 0) return 'now'
  const mins = Math.round(diff / 60000)
  if (mins < 1) return 'in <1m'
  if (mins < 60) return `in ${mins}m`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `in ${hours}h ${mins % 60}m`
  return `in ${Math.round(hours / 24)}d`
}

const stats = computed(() => {
  const total = recentRuns.value.length
  const ok = recentRuns.value.filter((r) => r.status === 'success').length
  const failed = recentRuns.value.filter((r) => r.status === 'error').length
  return { total, ok, failed, rate: total ? Math.round((ok / total) * 100) : 100 }
})

const triggerBadge: Record<string, string> = {
  manual_trigger: 'Manual',
  webhook_trigger: 'Webhook',
  schedule_trigger: 'Schedule',
}

// ------------------------------------------------------------------ v12 tags + search
const search = ref('')
const activeTag = ref<string | null>(null)

const allTags = computed(() => {
  const counts = new Map<string, number>()
  for (const wf of workflows.value) {
    for (const t of wf.tags || []) counts.set(t, (counts.get(t) || 0) + 1)
  }
  return Array.from(counts.entries())
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .map(([tag, count]) => ({ tag, count }))
})

const filteredWorkflows = computed(() => {
  let list = workflows.value
  // v16: folder scope - a folder chip includes its descendant folders
  if (activeFolder.value === 'none') {
    list = list.filter((w) => !w.folder_id)
  } else if (activeFolder.value) {
    const scope = descendantsOf(activeFolder.value)
    list = list.filter((w) => w.folder_id != null && scope.has(w.folder_id))
  }
  if (activeTag.value) {
    const want = activeTag.value.toLowerCase()
    list = list.filter((w) => (w.tags || []).some((t) => t.toLowerCase() === want))
  }
  const q = search.value.trim().toLowerCase()
  if (q) {
    list = list.filter(
      (w) => w.name.toLowerCase().includes(q) || (w.description || '').toLowerCase().includes(q),
    )
  }
  return list
})

// deterministic chip color per tag word
function tagColor(tag: string) {
  let h = 0
  for (let i = 0; i < tag.length; i++) h = (h * 31 + tag.charCodeAt(i)) >>> 0
  const palette = [
    'bg-orange-500/15 text-orange-300 border-orange-500/30',
    'bg-sky-500/15 text-sky-300 border-sky-500/30',
    'bg-emerald-500/15 text-emerald-300 border-emerald-500/30',
    'bg-violet-500/15 text-violet-300 border-violet-500/30',
    'bg-amber-500/15 text-amber-300 border-amber-500/30',
    'bg-rose-500/15 text-rose-300 border-rose-500/30',
  ]
  return palette[h % palette.length]
}

function toggleTag(tag: string) {
  activeTag.value = activeTag.value === tag ? null : tag
}

// ------------------------------------------------------------------ v16 folders
// null = All · 'none' = Unfiled · folder id = that folder (incl. descendants)
const activeFolder = ref<string | null>(null)
const showFolderModal = ref(false)
const folderModalMode = ref<'create' | 'rename'>('create')
const folderName = ref('')
const folderParentId = ref('')
const renamingFolder = ref<Folder | null>(null)
const moveMenuFor = ref<string | null>(null) // workflow id with an open move menu

const folderChildren = computed(() => {
  const m = new Map<string | null, string[]>()
  for (const f of folders.value) {
    const arr = m.get(f.parent_id) || []
    arr.push(f.id)
    m.set(f.parent_id, arr)
  }
  return m
})

function descendantsOf(id: string): Set<string> {
  const out = new Set<string>([id])
  const stack = [id]
  while (stack.length) {
    const cur = stack.pop()!
    for (const k of folderChildren.value.get(cur) || []) {
      if (!out.has(k)) {
        out.add(k)
        stack.push(k)
      }
    }
  }
  return out
}

function depthOf(id: string): number {
  const byId = new Map(folders.value.map((f) => [f.id, f]))
  let d = 1
  let cur = byId.get(id)
  while (cur?.parent_id && d < 10) {
    cur = byId.get(cur.parent_id)
    d++
  }
  return d
}

function folderPath(id: string): string {
  const byId = new Map(folders.value.map((f) => [f.id, f]))
  const names: string[] = []
  let cur = byId.get(id)
  let guard = 0
  while (cur && guard < 10) {
    names.unshift(cur.name)
    cur = cur.parent_id ? byId.get(cur.parent_id) : undefined
    guard++
  }
  return names.join(' / ')
}

const unfiledCount = computed(() => workflows.value.filter((w) => !w.folder_id).length)

function openCreateFolder() {
  folderModalMode.value = 'create'
  renamingFolder.value = null
  folderName.value = ''
  // default the parent to the folder currently being viewed (if any)
  folderParentId.value = activeFolder.value && activeFolder.value !== 'none' ? activeFolder.value : ''
  showFolderModal.value = true
}

function openRenameFolder(f: Folder) {
  folderModalMode.value = 'rename'
  renamingFolder.value = f
  folderName.value = f.name
  showFolderModal.value = true
}

function closeFolderModal() {
  showFolderModal.value = false
  folderName.value = ''
  folderParentId.value = ''
  renamingFolder.value = null
}

async function createFolder() {
  if (!folderName.value.trim()) return
  try {
    const body: Record<string, unknown> = { name: folderName.value.trim() }
    if (folderParentId.value) body.parent_id = folderParentId.value
    const f = await api.post<Folder>('/folders', body)
    folders.value.push(f)
    folders.value.sort((a, b) => a.name.localeCompare(b.name))
    closeFolderModal()
  } catch (e: any) {
    alert(e?.data?.detail || e?.message || 'Could not create the folder')
  }
}

async function renameFolder() {
  if (!renamingFolder.value || !folderName.value.trim()) return
  try {
    const updated = await api.patch<Folder>(`/folders/${renamingFolder.value.id}`, {
      name: folderName.value.trim(),
    })
    const i = folders.value.findIndex((x) => x.id === updated.id)
    if (i >= 0) folders.value[i] = updated
    closeFolderModal()
  } catch (e: any) {
    alert(e?.data?.detail || e?.message || 'Rename failed')
  }
}

async function removeFolder(f: Folder) {
  if (!confirm(`Delete folder "${f.name}"? Workflows inside move back to Unfiled.`)) return
  try {
    await api.del(`/folders/${f.id}`)
  } catch (e: any) {
    alert(e?.data?.detail || e?.message || 'Delete failed - subfolders must be moved first')
    return
  }
  folders.value = folders.value.filter((x) => x.id !== f.id)
  if (activeFolder.value === f.id) activeFolder.value = null
  workflows.value = await api.get<WorkflowListItem[]>('/workflows')
}

async function moveWorkflow(wf: WorkflowListItem, folderId: string) {
  moveMenuFor.value = null
  try {
    const updated = await api.put<Workflow>(`/workflows/${wf.id}`, { folder_id: folderId })
    wf.folder_id = updated.folder_id ?? null
    wf.folder_name = folders.value.find((f) => f.id === wf.folder_id)?.name || null
    folders.value = await api.get<Folder[]>('/folders') // keep chip counts honest
  } catch (e: any) {
    alert(e?.data?.detail || e?.message || 'Move failed')
  }
}

function onDocClick() {
  moveMenuFor.value = null
}

function openCreateWorkflow() {
  // when a folder is being viewed, file the new workflow right there
  newFolderId.value = activeFolder.value && activeFolder.value !== 'none' ? activeFolder.value : ''
  showCreate.value = true
}

onMounted(() => {
  document.addEventListener('click', onDocClick)
  loadAll()
})

// sidebar / palette "New workflow" lands on /?new=1 → open the create dialog.
// A watch (not just onMounted) covers the case where the dashboard is ALREADY
// the current page - a query-only change re-runs this without a remount.
watch(
  () => route.query.new,
  (v) => {
    if (v === '1') {
      openCreateWorkflow()
      navigateTo({ path: '/', query: {} }, { replace: true })
    }
  },
  { immediate: true },
)
</script>

<template>
  <div class="text-zinc-100">
    <!-- page header (app nav lives in the sidebar) -->
    <header class="border-b border-zinc-800/80 bg-zinc-950/90 backdrop-blur sticky top-0 z-20">
      <div class="mx-auto flex max-w-7xl items-center justify-between gap-3 px-4 py-3.5 sm:px-6">
        <div class="min-w-0">
          <h1 class="text-lg font-bold tracking-tight">Dashboard</h1>
          <p class="-mt-0.5 text-[11px] text-zinc-500">Python-native workflow automation</p>
        </div>
        <div class="flex items-center gap-2">
          <input
            ref="importInput"
            type="file"
            accept="application/json,.json"
            class="hidden"
            @change="onImportFile"
          />
          <button
            class="inline-flex items-center gap-2 rounded-xl border border-zinc-700 px-3.5 py-2 text-sm font-medium text-zinc-300 transition hover:border-zinc-500 hover:text-white disabled:opacity-50"
            :disabled="importing"
            title="Import a workflow JSON export or a .py8n.json template pack"
            @click="importInput?.click()"
          >
            <Loader2 v-if="importing" class="h-4 w-4 animate-spin" />
            <Upload v-else class="h-4 w-4" />
            <span class="hidden sm:inline">Import</span>
          </button>
          <button
            class="inline-flex items-center gap-2 rounded-xl border border-zinc-700 px-3.5 py-2 text-sm font-medium text-zinc-300 transition hover:border-zinc-500 hover:text-white disabled:opacity-50"
            :disabled="packBusy"
            title="Bundle workflows and datasets into one shareable pack"
            @click="openExportPack"
          >
            <Loader2 v-if="packBusy" class="h-4 w-4 animate-spin" />
            <Download v-else class="h-4 w-4" />
            <span class="hidden sm:inline">Export pack</span>
          </button>
          <NuxtLink
            to="/templates"
            class="inline-flex items-center gap-2 rounded-xl border border-zinc-700 px-3.5 py-2 text-sm font-medium text-zinc-300 transition hover:border-zinc-500 hover:text-white"
            title="Start from a ready-made template"
          >
            <Sparkles class="h-4 w-4" />
            <span class="hidden sm:inline">Templates</span>
          </NuxtLink>
          <button
            class="inline-flex items-center gap-2 rounded-xl bg-orange-500 px-4 py-2 text-sm font-semibold text-white shadow-lg shadow-orange-500/25 transition hover:bg-orange-400 active:scale-95"
            @click="openCreateWorkflow"
          >
            <Plus class="h-4 w-4" /> New Workflow
          </button>
        </div>
      </div>
    </header>

    <main class="mx-auto max-w-7xl px-4 py-8 sm:px-6">
      <!-- hero -->
      <section class="mb-8 rounded-2xl border border-zinc-800 bg-gradient-to-br from-zinc-900 via-zinc-900 to-zinc-950 p-6 sm:p-8">
        <div class="flex flex-wrap items-center justify-between gap-6">
          <div class="max-w-xl">
            <h2 class="text-2xl font-bold tracking-tight sm:text-3xl">
              Build automations like blocks.
              <span class="bg-gradient-to-r from-orange-400 to-rose-400 bg-clip-text text-transparent">Run them in Python.</span>
            </h2>
            <p class="mt-2 text-sm leading-relaxed text-zinc-400">
              Drag nodes onto the canvas, wire them up, and Py8n executes the graph
              topologically with a Jinja2-templated context - webhook, schedule and AI triggers included.
            </p>
          </div>
          <div class="grid grid-cols-3 gap-3 sm:gap-4">
            <div class="rounded-xl border border-zinc-800 bg-zinc-950/60 px-4 py-3 text-center">
              <p class="text-2xl font-bold text-orange-400">{{ stats.total }}</p>
              <p class="text-[11px] uppercase tracking-wide text-zinc-500">Runs</p>
            </div>
            <div class="rounded-xl border border-zinc-800 bg-zinc-950/60 px-4 py-3 text-center">
              <p class="text-2xl font-bold text-emerald-400">{{ stats.ok }}</p>
              <p class="text-[11px] uppercase tracking-wide text-zinc-500">Passed</p>
            </div>
            <div class="rounded-xl border border-zinc-800 bg-zinc-950/60 px-4 py-3 text-center">
              <p class="text-2xl font-bold text-rose-400">{{ stats.failed }}</p>
              <p class="text-[11px] uppercase tracking-wide text-zinc-500">Failed</p>
            </div>
          </div>
        </div>
      </section>

      <!-- workflows grid -->
      <section>
        <div class="mb-4 flex flex-wrap items-center justify-between gap-3">
          <h3 class="flex items-center gap-2 text-sm font-semibold uppercase tracking-wider text-zinc-400">
            <WorkflowIcon class="h-4 w-4" /> Workflows
            <span class="rounded-full bg-zinc-800 px-2 py-0.5 text-[11px] tabular-nums text-zinc-400">
              {{ filteredWorkflows.length }}{{ workflows.length !== filteredWorkflows.length ? ` / ${workflows.length}` : '' }}
            </span>
          </h3>
          <div class="relative">
            <Search class="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-zinc-500" />
            <input
              v-model="search"
              class="w-56 rounded-xl border border-zinc-800 bg-zinc-900/60 py-2 pl-9 pr-8 text-sm outline-none transition placeholder:text-zinc-600 focus:border-orange-500"
              placeholder="Search workflows…"
            />
            <button
              v-if="search"
              class="absolute right-2 top-1/2 -translate-y-1/2 rounded p-0.5 text-zinc-500 hover:text-zinc-200"
              title="Clear search"
              @click="search = ''"
            >
              <X class="h-3.5 w-3.5" />
            </button>
          </div>
        </div>

        <!-- v16 folder filter chips -->
        <div class="mb-3 flex flex-wrap items-center gap-1.5">
          <button
            class="inline-flex items-center gap-1 rounded-full border px-2.5 py-1 text-[11px] font-medium transition"
            :class="!activeFolder
              ? 'border-orange-500/50 bg-orange-500/15 text-orange-300'
              : 'border-zinc-800 bg-zinc-900/60 text-zinc-400 hover:border-zinc-600 hover:text-zinc-200'"
            @click="activeFolder = null"
          >
            <FolderIcon class="h-3 w-3" />
            All
            <span class="tabular-nums opacity-60">{{ workflows.length }}</span>
          </button>
          <div
            v-for="f in folders"
            :key="f.id"
            class="inline-flex items-stretch overflow-hidden rounded-full border transition"
            :class="activeFolder === f.id
              ? 'border-orange-500/50 bg-orange-500/15'
              : 'border-zinc-800 bg-zinc-900/60 hover:border-zinc-600'"
          >
            <button
              class="inline-flex items-center gap-1 py-1 pl-2.5 pr-2 text-[11px] font-medium"
              :class="activeFolder === f.id ? 'text-orange-300' : 'text-zinc-400 hover:text-zinc-200'"
              :title="folderPath(f.id)"
              @click="activeFolder = activeFolder === f.id ? null : f.id"
            >
              <FolderIcon class="h-3 w-3 shrink-0" />
              <span class="max-w-[10rem] truncate">{{ folderPath(f.id) }}</span>
              <span class="tabular-nums opacity-60">{{ f.total_count }}</span>
            </button>
            <template v-if="activeFolder === f.id">
              <button
                class="border-l border-orange-500/20 px-1.5 text-orange-300 transition hover:bg-orange-500/25"
                title="Rename folder"
                @click.stop="openRenameFolder(f)"
              >
                <Pencil class="h-3 w-3" />
              </button>
              <button
                class="border-l border-orange-500/20 px-1.5 text-orange-300 transition hover:bg-rose-500/25 hover:text-rose-300"
                title="Delete folder"
                @click.stop="removeFolder(f)"
              >
                <Trash2 class="h-3 w-3" />
              </button>
            </template>
          </div>
          <button
            class="inline-flex items-center gap-1 rounded-full border px-2.5 py-1 text-[11px] font-medium transition"
            :class="activeFolder === 'none'
              ? 'border-orange-500/50 bg-orange-500/15 text-orange-300'
              : 'border-zinc-800 bg-zinc-900/60 text-zinc-400 hover:border-zinc-600 hover:text-zinc-200'"
            title="Workflows not in any folder"
            @click="activeFolder = activeFolder === 'none' ? null : 'none'"
          >
            <FolderIcon class="h-3 w-3 opacity-50" />
            Unfiled
            <span class="tabular-nums opacity-60">{{ unfiledCount }}</span>
          </button>
          <button
            class="inline-flex items-center gap-1 rounded-full border border-dashed border-zinc-700 px-2.5 py-1 text-[11px] font-medium text-zinc-500 transition hover:border-orange-500/50 hover:text-orange-300"
            @click="openCreateFolder"
          >
            <FolderPlus class="h-3 w-3" />
            New folder
          </button>
        </div>

        <!-- tag filter chips -->
        <div v-if="allTags.length" class="mb-4 flex flex-wrap items-center gap-1.5">
          <button
            class="inline-flex items-center gap-1 rounded-full border px-2.5 py-1 text-[11px] font-medium transition"
            :class="!activeTag
              ? 'border-orange-500/50 bg-orange-500/15 text-orange-300'
              : 'border-zinc-800 bg-zinc-900/60 text-zinc-400 hover:border-zinc-600 hover:text-zinc-200'"
            @click="activeTag = null"
          >
            All
          </button>
          <button
            v-for="{ tag, count } in allTags"
            :key="tag"
            class="inline-flex items-center gap-1 rounded-full border px-2.5 py-1 text-[11px] font-medium transition"
            :class="activeTag === tag
              ? 'border-orange-500/50 bg-orange-500/15 text-orange-300'
              : tagColor(tag) + ' hover:brightness-125'"
            :title="`Filter by “${tag}”`"
            @click="toggleTag(tag)"
          >
            <TagIcon class="h-3 w-3" />
            {{ tag }}
            <span class="tabular-nums opacity-60">{{ count }}</span>
          </button>
        </div>

        <div v-if="loading" class="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          <div v-for="i in 3" :key="i" class="h-40 animate-pulse rounded-2xl border border-zinc-800 bg-zinc-900/50" />
        </div>

        <div v-else-if="workflows.length === 0" class="rounded-2xl border border-dashed border-zinc-800 p-12 text-center">
          <Sparkles class="mx-auto mb-3 h-8 w-8 text-zinc-600" />
          <p class="text-zinc-400">No workflows yet - create your first one.</p>
        </div>

        <div v-else-if="filteredWorkflows.length === 0" class="rounded-2xl border border-dashed border-zinc-800 p-12 text-center">
          <Search class="mx-auto mb-3 h-8 w-8 text-zinc-600" />
          <p class="text-zinc-400">No workflows match your filters.</p>
          <button
            class="mt-3 rounded-lg border border-zinc-700 px-3 py-1.5 text-xs text-zinc-300 transition hover:border-zinc-500 hover:text-white"
            @click="search = ''; activeTag = null; activeFolder = null"
          >
            Clear filters
          </button>
        </div>

        <div v-else class="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          <article
            v-for="wf in filteredWorkflows"
            :key="wf.id"
            class="group relative flex flex-col rounded-2xl border border-zinc-800 bg-zinc-900/40 p-5 transition hover:border-orange-500/40 hover:bg-zinc-900/70"
          >
            <div class="mb-3 flex items-start justify-between gap-3">
              <h4 class="font-semibold leading-snug">{{ wf.name }}</h4>
              <button
                class="shrink-0 rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide transition"
                :class="wf.is_active
                  ? 'bg-emerald-500/15 text-emerald-400 hover:bg-emerald-500/25'
                  : 'bg-zinc-800 text-zinc-500 hover:bg-zinc-700'"
                :title="wf.is_active ? 'Active - triggers enabled' : 'Inactive - click to activate'"
                @click.stop="toggleActive(wf)"
              >
                {{ wf.is_active ? 'Active' : 'Paused' }}
              </button>
            </div>
            <p class="mb-4 line-clamp-2 min-h-[2.4rem] text-xs leading-relaxed text-zinc-500">
              {{ wf.description || 'No description yet.' }}
            </p>
            <div class="mb-4 flex flex-wrap items-center gap-1.5">
              <span
                v-for="t in wf.trigger_types"
                :key="t"
                class="inline-flex items-center gap-1 rounded-md bg-zinc-800/80 px-1.5 py-0.5 text-[10px] text-zinc-400"
              >
                <Webhook v-if="t === 'webhook_trigger'" class="h-3 w-3 text-orange-400" />
                <Clock v-else-if="t === 'schedule_trigger'" class="h-3 w-3 text-yellow-400" />
                <Play v-else class="h-3 w-3 text-violet-400" />
                {{ triggerBadge[t] || t }}
              </span>
              <span class="rounded-md bg-zinc-800/80 px-1.5 py-0.5 text-[10px] text-zinc-500">{{ wf.node_count }} nodes</span>
              <!-- v16 folder badge -->
              <span
                v-if="wf.folder_name"
                class="inline-flex items-center gap-0.5 rounded-md bg-zinc-800/80 px-1.5 py-0.5 text-[10px] text-zinc-400"
                :title="`In folder “${wf.folder_name}”`"
              >
                <FolderIcon class="h-2.5 w-2.5 text-orange-400/80" />
                {{ wf.folder_name }}
              </span>
              <!-- v12 tags -->
              <button
                v-for="tag in wf.tags || []"
                :key="tag"
                class="inline-flex items-center gap-0.5 rounded-md border px-1.5 py-0.5 text-[10px] transition hover:brightness-125"
                :class="tagColor(tag)"
                :title="`Filter by “${tag}”`"
                @click.stop="toggleTag(tag)"
              >
                <TagIcon class="h-2.5 w-2.5" />
                {{ tag }}
              </button>
            </div>
            <div
              v-if="wf.schedule_summary"
              class="mb-4 flex items-center gap-1.5 text-[11px] text-zinc-500"
              :title="wf.next_run_at ? `Next run ${fmtDate(wf.next_run_at)}` : 'Activate to schedule runs'"
            >
              <Clock class="h-3 w-3 shrink-0 text-yellow-500/80" />
              <span class="font-mono">{{ wf.schedule_summary }}</span>
              <span v-if="wf.next_run_at" class="text-zinc-600">· next {{ relTime(wf.next_run_at) }}</span>
              <span v-else-if="!wf.is_active" class="text-zinc-700">· paused</span>
            </div>
            <!-- v8: error workflow binding -->
            <div
              v-if="wf.error_workflow_name"
              class="mb-4 flex items-center gap-1.5 text-[11px] text-rose-300/80"
              title="Runs with a structured error payload when this workflow fails"
            >
              <ShieldAlert class="h-3 w-3 shrink-0 text-rose-400" />
              <span>on error →</span>
              <span class="truncate font-medium">{{ wf.error_workflow_name }}</span>
            </div>
            <div class="mt-auto flex items-center justify-between border-t border-zinc-800/70 pt-3">
              <span class="text-[11px] text-zinc-600">Updated {{ fmtDate(wf.updated_at) }}</span>
              <div class="flex items-center gap-1">
                <!-- v16 move-to-folder -->
                <div class="relative">
                  <button
                    class="rounded-lg p-1.5 text-zinc-600 transition hover:bg-zinc-800 hover:text-zinc-300 group-hover:opacity-100"
                    :class="moveMenuFor === wf.id && 'opacity-100 text-orange-400'"
                    title="Move to folder"
                    @click.stop="moveMenuFor = moveMenuFor === wf.id ? null : wf.id"
                  >
                    <FolderInput class="h-4 w-4" />
                  </button>
                  <div
                    v-if="moveMenuFor === wf.id"
                    class="absolute bottom-full right-0 z-30 mb-1.5 w-60 overflow-hidden rounded-xl border border-zinc-700 bg-zinc-900 py-1 shadow-2xl"
                  >
                    <p class="px-3 py-1.5 text-[10px] font-semibold uppercase tracking-wider text-zinc-500">Move to folder</p>
                    <button
                      class="flex w-full items-center justify-between gap-2 px-3 py-1.5 text-left text-xs transition hover:bg-zinc-800"
                      :class="!wf.folder_id ? 'text-orange-300' : 'text-zinc-300'"
                      @click.stop="moveWorkflow(wf, '')"
                    >
                      No folder
                      <Check v-if="!wf.folder_id" class="h-3 w-3 shrink-0" />
                    </button>
                    <button
                      v-for="f in folders"
                      :key="f.id"
                      class="flex w-full items-center justify-between gap-2 px-3 py-1.5 text-left text-xs transition hover:bg-zinc-800"
                      :class="wf.folder_id === f.id ? 'text-orange-300' : 'text-zinc-300'"
                      @click.stop="moveWorkflow(wf, f.id)"
                    >
                      <span class="truncate">{{ folderPath(f.id) }}</span>
                      <Check v-if="wf.folder_id === f.id" class="h-3 w-3 shrink-0" />
                    </button>
                    <p v-if="!folders.length" class="px-3 py-2 text-xs text-zinc-600">No folders yet - create one from the bar above.</p>
                  </div>
                </div>
                <button
                  class="rounded-lg p-1.5 text-zinc-600 opacity-0 transition hover:bg-zinc-800 hover:text-zinc-300 group-hover:opacity-100"
                  title="Export as template pack (.py8n.json)"
                  :disabled="packBusy"
                  @click.stop="exportOne(wf)"
                >
                  <Download class="h-4 w-4" />
                </button>
                <button
                  class="rounded-lg p-1.5 text-zinc-600 opacity-0 transition hover:bg-rose-500/10 hover:text-rose-400 group-hover:opacity-100"
                  title="Delete workflow"
                  @click.stop="removeWorkflow(wf)"
                >
                  <Trash2 class="h-4 w-4" />
                </button>
                <NuxtLink
                  :to="`/workflows/${wf.id}`"
                  class="inline-flex items-center gap-1 rounded-lg bg-zinc-800 px-3 py-1.5 text-xs font-medium text-zinc-200 transition hover:bg-orange-500 hover:text-white"
                >
                  Open <ChevronRight class="h-3.5 w-3.5" />
                </NuxtLink>
              </div>
            </div>
          </article>
        </div>
      </section>

      <!-- recent runs -->
      <section v-if="recentRuns.length" class="mt-10">
        <h3 class="mb-4 flex items-center gap-2 text-sm font-semibold uppercase tracking-wider text-zinc-400">
          <Activity class="h-4 w-4" /> Recent runs
        </h3>
        <div class="overflow-hidden rounded-2xl border border-zinc-800">
          <table class="w-full text-left text-sm">
            <tbody>
              <tr
                v-for="run in recentRuns"
                :key="run.id"
                class="border-b border-zinc-800/60 bg-zinc-900/30 last:border-0"
              >
                <td class="px-4 py-2.5">
                  <span class="inline-flex items-center gap-1.5">
                    <CheckCircle2 v-if="run.status === 'success'" class="h-4 w-4 text-emerald-400" />
                    <XCircle v-else-if="run.status === 'error'" class="h-4 w-4 text-rose-400" />
                    <Clock v-else class="h-4 w-4 animate-pulse text-amber-400" />
                    <span class="capitalize">{{ run.status }}</span>
                  </span>
                </td>
                <td class="px-4 py-2.5 text-xs text-zinc-500">
                  {{ workflows.find(w => w.id === run.workflow_id)?.name || run.workflow_id.slice(0, 8) }}
                </td>
                <td class="px-4 py-2.5 text-xs capitalize text-zinc-500">{{ run.trigger_type }}</td>
                <td class="px-4 py-2.5 text-xs text-zinc-500">{{ run.duration_ms != null ? run.duration_ms + ' ms' : '-' }}</td>
                <td class="px-4 py-2.5 text-right text-xs text-zinc-600">{{ fmtDate(run.started_at || '') }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
    </main>

    <!-- create modal -->
    <div
      v-if="showCreate"
      class="fixed inset-0 z-50 grid place-items-center bg-black/70 p-4 backdrop-blur-sm"
      @click.self="showCreate = false"
    >
      <div class="w-full max-w-md rounded-2xl border border-zinc-800 bg-zinc-900 p-6 shadow-2xl">
        <h3 class="mb-1 text-lg font-bold">New workflow</h3>
        <p class="mb-5 text-xs text-zinc-500">Starts with a Manual Trigger node you can build on.</p>
        <label class="mb-1 block text-xs font-medium uppercase tracking-wide text-zinc-500">Name</label>
        <input
          v-model="newName"
          class="mb-4 w-full rounded-xl border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm outline-none transition focus:border-orange-500"
          placeholder="e.g. Nightly report generator"
          @keyup.enter="createWorkflow"
        />
        <label class="mb-1 block text-xs font-medium uppercase tracking-wide text-zinc-500">Description</label>
        <textarea
          v-model="newDesc"
          rows="2"
          class="mb-4 w-full resize-none rounded-xl border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm outline-none transition focus:border-orange-500"
          placeholder="What does this automation do?"
        />
        <!-- v16: file the new workflow right away -->
        <label class="mb-1 block text-xs font-medium uppercase tracking-wide text-zinc-500">Folder (optional)</label>
        <select
          v-model="newFolderId"
          class="mb-5 w-full rounded-xl border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm outline-none transition focus:border-orange-500"
        >
          <option value="">No folder</option>
          <option v-for="f in folders" :key="f.id" :value="f.id">{{ folderPath(f.id) }}</option>
        </select>
        <div class="flex justify-end gap-2">
          <button class="rounded-xl px-4 py-2 text-sm text-zinc-400 transition hover:text-zinc-200" @click="showCreate = false">
            Cancel
          </button>
          <button
            class="rounded-xl bg-orange-500 px-4 py-2 text-sm font-semibold text-white transition hover:bg-orange-400 disabled:opacity-50"
            :disabled="creating || !newName.trim()"
            @click="createWorkflow"
          >
            {{ creating ? 'Creating…' : 'Create workflow' }}
          </button>
        </div>
      </div>
    </div>

    <!-- v16 folder create / rename modal -->
    <div
      v-if="showFolderModal"
      class="fixed inset-0 z-50 grid place-items-center bg-black/70 p-4 backdrop-blur-sm"
      @click.self="closeFolderModal"
    >
      <div class="w-full max-w-md rounded-2xl border border-zinc-800 bg-zinc-900 p-6 shadow-2xl">
        <h3 class="mb-1 text-lg font-bold">{{ folderModalMode === 'create' ? 'New folder' : 'Rename folder' }}</h3>
        <p class="mb-5 text-xs text-zinc-500">
          {{ folderModalMode === 'create'
            ? 'Group related workflows. Nesting up to 3 levels.'
            : 'Rename without touching the workflows inside.' }}
        </p>
        <label class="mb-1 block text-xs font-medium uppercase tracking-wide text-zinc-500">Name</label>
        <input
          v-model="folderName"
          class="mb-4 w-full rounded-xl border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm outline-none transition focus:border-orange-500"
          placeholder="e.g. Marketing automations"
          @keyup.enter="folderModalMode === 'create' ? createFolder() : renameFolder()"
        />
        <template v-if="folderModalMode === 'create'">
          <label class="mb-1 block text-xs font-medium uppercase tracking-wide text-zinc-500">Parent folder (optional)</label>
          <select
            v-model="folderParentId"
            class="mb-5 w-full rounded-xl border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm outline-none transition focus:border-orange-500"
          >
            <option value="">Top level</option>
            <option v-for="f in folders.filter((x) => depthOf(x.id) <= 2)" :key="f.id" :value="f.id">
              {{ folderPath(f.id) }}
            </option>
          </select>
        </template>
        <div class="flex justify-end gap-2">
          <button class="rounded-xl px-4 py-2 text-sm text-zinc-400 transition hover:text-zinc-200" @click="closeFolderModal">
            Cancel
          </button>
          <button
            class="rounded-xl bg-orange-500 px-4 py-2 text-sm font-semibold text-white transition hover:bg-orange-400 disabled:opacity-50"
            :disabled="!folderName.trim()"
            @click="folderModalMode === 'create' ? createFolder() : renameFolder()"
          >
            {{ folderModalMode === 'create' ? 'Create folder' : 'Save' }}
          </button>
        </div>
      </div>
    </div>

    <!-- v39 export pack modal: pick workflows + datasets, download one bundle -->
    <div
      v-if="showExportPack"
      class="fixed inset-0 z-50 grid place-items-center bg-black/70 p-4 backdrop-blur-sm"
      @click.self="showExportPack = false"
    >
      <div class="flex max-h-[85vh] w-full max-w-lg flex-col rounded-2xl border border-zinc-800 bg-zinc-900 p-6 shadow-2xl">
        <h3 class="mb-1 text-lg font-bold">Export template pack</h3>
        <p class="mb-5 text-xs text-zinc-500">
          Bundles the selected workflows and dataset snapshots into a single .json file you can
          re-import on any Py8n instance.
        </p>
        <div class="mb-4 min-h-0 flex-1 space-y-5 overflow-y-auto pr-1">
          <div>
            <p class="mb-2 text-xs font-semibold uppercase tracking-wider text-zinc-500">
              Workflows ({{ workflows.length }})
            </p>
            <p v-if="!workflows.length" class="text-xs text-zinc-600">No workflows yet.</p>
            <label
              v-for="wf in workflows"
              :key="wf.id"
              class="flex cursor-pointer items-center justify-between gap-3 rounded-xl px-2 py-1.5 text-sm transition hover:bg-zinc-800/60"
            >
              <span class="flex min-w-0 items-center gap-2">
                <input v-model="packWfSel[wf.id]" type="checkbox" class="accent-orange-500" />
                <span class="truncate">{{ wf.name }}</span>
              </span>
              <span class="shrink-0 text-[11px] text-zinc-600">{{ wf.node_count }} nodes</span>
            </label>
          </div>
          <div>
            <p class="mb-2 text-xs font-semibold uppercase tracking-wider text-zinc-500">
              Datasets ({{ datasets.length }})
            </p>
            <p v-if="!datasets.length" class="text-xs text-zinc-600">No datasets yet.</p>
            <label
              v-for="ds in datasets"
              :key="ds.id"
              class="flex cursor-pointer items-center justify-between gap-3 rounded-xl px-2 py-1.5 text-sm transition hover:bg-zinc-800/60"
            >
              <span class="flex min-w-0 items-center gap-2">
                <input v-model="packDsSel[ds.id]" type="checkbox" class="accent-orange-500" />
                <span class="truncate">{{ ds.name }}</span>
              </span>
              <span class="shrink-0 text-[11px] text-zinc-600">{{ ds.row_count }} rows</span>
            </label>
          </div>
        </div>
        <div class="mt-4 flex items-center justify-between gap-2 border-t border-zinc-800 pt-4">
          <span class="text-xs text-zinc-500">
            {{ packSelCounts.total ? `${packSelCounts.w} workflow(s) + ${packSelCounts.d} dataset(s) selected` : 'Nothing selected' }}
          </span>
          <div class="flex gap-2">
            <button class="rounded-xl px-4 py-2 text-sm text-zinc-400 transition hover:text-zinc-200" @click="showExportPack = false">
              Cancel
            </button>
            <button
              class="inline-flex items-center gap-2 rounded-xl bg-orange-500 px-4 py-2 text-sm font-semibold text-white transition hover:bg-orange-400 disabled:opacity-50"
              :disabled="packBusy || !packSelCounts.total"
              @click="downloadExportPack"
            >
              <Loader2 v-if="packBusy" class="h-4 w-4 animate-spin" />
              <Download v-else class="h-4 w-4" />
              Download pack
            </button>
          </div>
        </div>
      </div>
    </div>

    <!-- v39 import preview modal: what a pack import would do -->
    <div
      v-if="packPreview"
      class="fixed inset-0 z-50 grid place-items-center bg-black/70 p-4 backdrop-blur-sm"
      @click.self="closePackPreview"
    >
      <div class="flex max-h-[85vh] w-full max-w-lg flex-col rounded-2xl border border-zinc-800 bg-zinc-900 p-6 shadow-2xl">
        <template v-if="!packResult">
          <h3 class="mb-1 text-lg font-bold">Import template pack</h3>
          <p class="mb-5 text-xs text-zinc-500">
            {{ packPreview.workflow_count }} workflow(s) and {{ packPreview.dataset_count }} dataset(s) inside.
            Review before anything is created.
          </p>
          <div class="mb-4 min-h-0 flex-1 space-y-5 overflow-y-auto pr-1">
            <div>
              <p class="mb-2 text-xs font-semibold uppercase tracking-wider text-zinc-500">Workflows</p>
              <div
                v-for="w in packPreview.workflows"
                :key="w.name"
                class="mb-1.5 rounded-xl border border-zinc-800 px-3 py-2"
                :class="w.valid ? '' : 'border-rose-500/40 bg-rose-500/5'"
              >
                <div class="flex items-center justify-between gap-2 text-sm">
                  <span class="truncate">{{ w.name }}</span>
                  <span class="shrink-0 text-[11px]" :class="w.valid ? 'text-zinc-600' : 'text-rose-400'">
                    {{ w.valid ? `${w.node_count} nodes` : 'invalid graph' }}
                  </span>
                </div>
                <p v-if="!w.valid" class="mt-1 text-[11px] text-rose-300/80">{{ w.error }}</p>
                <p v-else-if="w.exists" class="mt-1 text-[11px] text-zinc-600">A workflow with this name already exists - it will be imported again.</p>
              </div>
            </div>
            <div>
              <p class="mb-2 text-xs font-semibold uppercase tracking-wider text-zinc-500">Datasets</p>
              <div
                v-for="d in packPreview.datasets"
                :key="d.name"
                class="mb-1.5 rounded-xl border border-zinc-800 px-3 py-2"
                :class="d.invalid_name ? 'border-rose-500/40 bg-rose-500/5' : ''"
              >
                <div class="flex items-center justify-between gap-2 text-sm">
                  <span class="truncate">{{ d.name }}</span>
                  <span class="shrink-0 text-[11px]" :class="d.invalid_name ? 'text-rose-400' : 'text-zinc-600'">
                    {{ d.invalid_name ? 'invalid name' : `${d.rows} rows` }}
                  </span>
                </div>
                <p v-if="d.rename_to" class="mt-1 text-[11px] text-amber-300/80">Name taken - will be created as "{{ d.rename_to }}".</p>
              </div>
            </div>
            <p v-for="wn in packPreview.warnings" :key="wn" class="text-[11px] text-amber-300/80">{{ wn }}</p>
          </div>
          <div class="mt-4 flex justify-end gap-2 border-t border-zinc-800 pt-4">
            <button class="rounded-xl px-4 py-2 text-sm text-zinc-400 transition hover:text-zinc-200" @click="closePackPreview">
              Cancel
            </button>
            <button
              class="inline-flex items-center gap-2 rounded-xl bg-orange-500 px-4 py-2 text-sm font-semibold text-white transition hover:bg-orange-400 disabled:opacity-50"
              :disabled="packImporting"
              @click="confirmPackImport"
            >
              <Loader2 v-if="packImporting" class="h-4 w-4 animate-spin" />
              Import pack
            </button>
          </div>
        </template>
        <template v-else>
          <h3 class="mb-1 text-lg font-bold">Pack imported</h3>
          <p class="mb-5 text-xs text-zinc-500">Imported as inactive; datasets landed through the normal parquet pipeline.</p>
          <div class="mb-4 min-h-0 flex-1 space-y-4 overflow-y-auto pr-1 text-sm">
            <div v-if="packResult.workflows.length">
              <p class="mb-2 text-xs font-semibold uppercase tracking-wider text-emerald-400">Created {{ packResult.workflows.length }} workflow(s)</p>
              <p v-for="w in packResult.workflows" :key="w.id" class="truncate text-zinc-300">{{ w.name }}</p>
            </div>
            <div v-if="packResult.datasets.length">
              <p class="mb-2 text-xs font-semibold uppercase tracking-wider text-emerald-400">Created {{ packResult.datasets.length }} dataset(s)</p>
              <p v-for="d in packResult.datasets" :key="d.id" class="truncate text-zinc-300">{{ d.name }} ({{ d.row_count }} rows)</p>
            </div>
            <div v-if="packResult.skipped.length">
              <p class="mb-2 text-xs font-semibold uppercase tracking-wider text-rose-400">Skipped {{ packResult.skipped.length }} item(s)</p>
              <p v-for="s in packResult.skipped" :key="s.name" class="truncate text-zinc-400" :title="s.reason">{{ s.name }}: {{ s.reason }}</p>
            </div>
          </div>
          <div class="mt-4 flex justify-end border-t border-zinc-800 pt-4">
            <button class="rounded-xl bg-zinc-800 px-4 py-2 text-sm font-medium text-zinc-200 transition hover:bg-zinc-700" @click="closePackPreview">
              Done
            </button>
          </div>
        </template>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue'
import {
  Database, Trash2, Loader2, ArrowLeft, Rows3, ChevronLeft, ChevronRight,
  Play, BarChart3, Table2, X, History, Undo2, Plus, Eye, Download, GitBranch, ChevronDown,
  Activity, ShieldCheck, ShieldAlert, Trash as TrashIcon,
} from 'lucide-vue-next'
import { useApi } from '~/composables/useApi'

const { api, download } = useApi()
const route = useRoute()
const ref_ = computed(() => String(route.params.id))

interface DatasetMeta {
  id: string
  name: string
  description: string
  schema_json: { name: string; dtype: string }[]
  row_count: number
  source: string
  tags: string[]
  created_at: string | null
  updated_at: string | null
}

interface DatasetVersionRow {
  id: string
  dataset_id: string
  version: number
  row_count: number
  source: string
  note: string
  created_at: string
  current: boolean
  file_exists: boolean
}

const loading = ref(true)
const meta = ref<DatasetMeta | null>(null)
const error = ref<string | null>(null)
const deleting = ref(false)

// data preview
const rows = ref<any[]>([])
const columns = ref<string[]>([])
const offset = ref(0)
const pageSize = 25
const loadingRows = ref(false)

// profile
const profile = ref<any>(null)
const loadingProfile = ref(false)

// v45: dataset export
const exportOpen = ref(false)
const exporting = ref<string | null>(null)
const EXPORT_FORMATS = ['csv', 'xlsx', 'json', 'parquet'] as const
async function exportDataset(fmt: string) {
  if (!meta.value) return
  exporting.value = fmt
  try {
    await download(`/datasets/${meta.value.id}/export?fmt=${fmt}`, `${meta.value.name}.${fmt === 'parquet' ? 'parquet' : fmt}`)
    exportOpen.value = false
  } catch (e: any) {
    error.value = e?.data?.detail || e?.message || 'Export failed'
  } finally {
    exporting.value = null
  }
}

// sql console
const sql = ref('')
const running = ref(false)
const sqlResult = ref<{ columns: string[]; rows: any[]; row_count: number; duration_ms: number; views: Record<string, string> } | null>(null)
const sqlError = ref<string | null>(null)

async function loadMeta() {
  loading.value = true
  error.value = null
  try {
    meta.value = await api.get<DatasetMeta>(`/datasets/${ref_.value}`)
  } catch (e: any) {
    error.value = e?.data?.detail || e?.message || 'Dataset not found'
  } finally {
    loading.value = false
  }
}

async function loadRows() {
  if (!meta.value) return
  loadingRows.value = true
  try {
    const data = await api.get<any>(`/datasets/${meta.value.id}/rows?offset=${offset.value}&limit=${pageSize}`)
    rows.value = data.rows
    columns.value = data.columns
  } catch (e: any) {
    error.value = e?.data?.detail || e?.message || 'Failed to load rows'
  } finally {
    loadingRows.value = false
  }
}

// v45: correlation lookup for the profile panel
function corrFor(name: string): Record<string, number> | null {
  const row = (profile.value?.correlation || []).find((r: any) => r.column === name)
  if (!row) return null
  const others: Record<string, number> = { ...row.correlations }
  delete others[name]
  return Object.keys(others).length ? others : null
}

async function loadProfile() {
  if (!meta.value) return
  loadingProfile.value = true
  try {
    profile.value = await api.get<any>(`/datasets/${meta.value.id}/profile`)
  } catch {
    profile.value = null
  } finally {
    loadingProfile.value = false
  }
}

onMounted(async () => {
  await loadMeta()
  if (meta.value) {
    await Promise.all([loadRows(), loadProfile(), loadVersions(), loadHealth()])
  }
})

watch(offset, loadRows)

async function runSql() {
  if (!sql.value.trim()) return
  running.value = true
  sqlError.value = null
  try {
    sqlResult.value = await api.post<any>('/datasets/query', { sql: sql.value })
  } catch (e: any) {
    sqlError.value = e?.data?.detail || e?.message || 'SQL failed'
    sqlResult.value = null
  } finally {
    running.value = false
  }
}

async function removeDataset() {
  if (!meta.value) return
  if (!confirm(`Delete dataset "${meta.value.name}" and its ${meta.value.row_count} rows? All snapshots die with it.`)) return
  deleting.value = true
  try {
    await api.del(`/datasets/${meta.value.id}`)
    navigateTo('/datasets')
  } catch (e: any) {
    alert(e?.data?.detail || e?.message || 'Delete failed')
    deleting.value = false
  }
}

// ------------------------------------------------------------------ tags (v44)
const newTag = ref('')
const savingTags = ref(false)

async function addTag() {
  if (!meta.value || !newTag.value.trim()) return
  const t = newTag.value.trim()
  if (meta.value.tags.some((x) => x.toLowerCase() === t.toLowerCase())) {
    newTag.value = ''
    return
  }
  await saveTags([...meta.value.tags, t])
  newTag.value = ''
}

async function removeTag(tag: string) {
  if (!meta.value) return
  await saveTags(meta.value.tags.filter((x) => x !== tag))
}

async function saveTags(tags: string[]) {
  if (!meta.value) return
  savingTags.value = true
  try {
    meta.value = await api.put<DatasetMeta>(`/datasets/${meta.value.id}`, { tags })
  } catch (e: any) {
    error.value = e?.data?.detail || e?.message || 'Tag update failed'
  } finally {
    savingTags.value = false
  }
}

// ------------------------------------------------------------------ versions (v44)
const versions = ref<DatasetVersionRow[]>([])
const loadingVersions = ref(false)
const versionPreview = ref<{ version: number; columns: string[]; rows: any[]; shown: number } | null>(null)
const busyVersion = ref<number | null>(null)
const versionMsg = ref('')

const SOURCE_STYLE: Record<string, string> = {
  api: 'bg-sky-500/10 text-sky-400',
  upload: 'bg-cyan-500/10 text-cyan-400',
  import: 'bg-violet-500/10 text-violet-400',
  workflow: 'bg-orange-500/10 text-orange-400',
  append: 'bg-emerald-500/10 text-emerald-400',
  replace: 'bg-amber-500/10 text-amber-400',
  restore: 'bg-rose-500/10 text-rose-400',
}

async function loadVersions() {
  if (!meta.value) return
  loadingVersions.value = true
  try {
    versions.value = await api.get<DatasetVersionRow[]>(`/datasets/${meta.value.id}/versions`)
  } catch {
    versions.value = []
  } finally {
    loadingVersions.value = false
  }
}

async function previewVersion(v: DatasetVersionRow) {
  if (!meta.value) return
  busyVersion.value = v.version
  try {
    const data = await api.get<any>(`/datasets/${meta.value.id}/versions/${v.version}/rows?limit=50`)
    versionPreview.value = data
  } catch (e: any) {
    error.value = e?.data?.detail || e?.message || 'Preview failed'
  } finally {
    busyVersion.value = null
  }
}

async function restoreVersion(v: DatasetVersionRow) {
  if (!meta.value) return
  if (!confirm(`Restore v${v.version} (${v.row_count} rows, ${v.source})? The current ${meta.value.row_count} rows will be replaced - the restored state itself becomes a new version, so this is undoable.`)) return
  busyVersion.value = v.version
  versionMsg.value = ''
  try {
    await api.post(`/datasets/${meta.value.id}/versions/${v.version}/restore`)
    versionMsg.value = `Restored v${v.version} - it is now the newest snapshot`
    await Promise.all([loadMeta(), loadRows(), loadVersions()])
  } catch (e: any) {
    error.value = e?.data?.detail || e?.message || 'Restore failed'
  } finally {
    busyVersion.value = null
  }
}

async function deleteVersion(v: DatasetVersionRow) {
  if (!meta.value) return
  if (!confirm(`Delete snapshot v${v.version}? The live dataset is untouched.`)) return
  busyVersion.value = v.version
  try {
    await api.del(`/datasets/${meta.value.id}/versions/${v.version}`)
    await loadVersions()
  } catch (e: any) {
    error.value = e?.data?.detail || e?.message || 'Delete failed'
  } finally {
    busyVersion.value = null
  }
}

// ------------------------------------------------------------------ lineage (v47)
// Provenance timeline: every version with the workflow/execution/node that
// produced it (origin 'surface' = API/upload-side writes). Lazy-loaded on
// first expand, ascending by version like the versions endpoint.
interface LineageStep {
  version: number
  row_count: number
  source: string
  note: string
  created_at: string | null
  workflow_id: string | null
  workflow_name: string | null
  execution_id: string | null
  node_name: string | null
  origin: 'workflow' | 'surface'
}

interface LineageResponse {
  dataset_id: string
  name: string
  created_at: string | null
  row_count: number
  workflow_versions: number
  steps: LineageStep[]
}

const lineage = ref<LineageResponse | null>(null)
const lineageOpen = ref(false)
const loadingLineage = ref(false)

async function toggleLineage() {
  lineageOpen.value = !lineageOpen.value
  if (lineageOpen.value && !lineage.value && meta.value) {
    loadingLineage.value = true
    try {
      lineage.value = await api.get<LineageResponse>(`/datasets/${meta.value.id}/lineage`)
    } catch (e: any) {
      error.value = e?.data?.detail || e?.message || 'Lineage failed'
    } finally {
      loadingLineage.value = false
    }
  }
}

function lineageOriginLabel(s: LineageStep): string {
  return s.origin === 'workflow'
    ? `${s.workflow_name || 'workflow'} · ${s.node_name || 'node'}`
    : s.source
}

function fmtCell(v: any): string {
  if (v === null || v === undefined) return '-'
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}

function fmtDate(iso: string | null) {
  if (!iso) return '-'
  return new Date(iso).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

const dtypeColor: Record<string, string> = {
  integer: 'text-sky-300 border-sky-500/30 bg-sky-500/10',
  number: 'text-cyan-300 border-cyan-500/30 bg-cyan-500/10',
  boolean: 'text-amber-300 border-amber-500/30 bg-amber-500/10',
  datetime: 'text-violet-300 border-violet-500/30 bg-violet-500/10',
  text: 'text-zinc-300 border-zinc-700 bg-zinc-800/60',
}

// ------------------------------------------------------------------ v50: health
interface HealthReport {
  status: string
  score: number
  checked_rows: number
  freshness: { last_write_at: string | null; age_minutes: number | null; tier: string }
  volume: { rows: number; previous_rows: number | null; delta: number | null; delta_pct: number | null; versions: number }
  schema: { columns: number; contract_present: boolean; contract_ok: boolean | null; contract_violations: any[]; contract_version: number }
  quality: { score: number; null_rate_pct: number | null; worst_null_column: { column: string; null_pct: number } | null; duplicate_rows_pct: number | null; completeness_pct: number | null }
  signals: { fresh: boolean; schema_valid: boolean; no_volume_shock: boolean }
}

const health = ref<HealthReport | null>(null)
const loadingHealth = ref(false)

const healthStatusStyle: Record<string, string> = {
  healthy: 'border-emerald-500/40 bg-emerald-500/10 text-emerald-300',
  degraded: 'border-amber-500/40 bg-amber-500/10 text-amber-300',
  unhealthy: 'border-rose-500/40 bg-rose-500/10 text-rose-300',
}
const healthDot: Record<string, string> = {
  healthy: 'bg-emerald-400', degraded: 'bg-amber-400', unhealthy: 'bg-rose-400',
}

async function loadHealth() {
  if (!meta.value) return
  loadingHealth.value = true
  try {
    health.value = await api.get<HealthReport>(`/datasets/${meta.value.id}/health`)
  } catch {
    health.value = null
  } finally {
    loadingHealth.value = false
  }
}

function fmtAgeMin(m: number | null): string {
  if (m === null || m === undefined) return 'never'
  if (m < 1) return 'just now'
  if (m < 60) return `${Math.round(m)} min ago`
  if (m < 1440) return `${Math.round(m / 60)}h ago`
  return `${Math.round(m / 1440)}d ago`
}

// ------------------------------------------------------------------ v50: data contract
interface ContractCol { name: string; dtype: string; nullable: boolean; allowed?: string[] | null }
interface ContractReport { present: boolean; columns: ContractCol[]; on_violation: string | null; version: number; ok?: boolean | null; violations?: any[] }

const contract = ref<ContractReport | null>(null)
const contractCols = ref<ContractCol[]>([])
const contractOnViolation = ref<'warn' | 'error'>('warn')
const contractOpen = ref(false)
const loadingContract = ref(false)
const savingContract = ref(false)
const contractMsg = ref('')
const contractErr = ref('')
const checkingContract = ref(false)
const contractCheckResult = ref<any>(null)

const DTYPES = ['text', 'integer', 'number', 'boolean', 'datetime']

async function loadContract() {
  if (!meta.value) return
  loadingContract.value = true
  try {
    const res = await api.get<ContractReport>(`/datasets/${meta.value.id}/contract`)
    contract.value = res
    contractCols.value = res.present ? res.columns.map(c => ({ ...c, allowed: c.allowed || null })) : []
    contractOnViolation.value = (res.on_violation as any) || 'warn'
  } catch (e: any) {
    contractErr.value = e?.data?.detail || e?.message || 'Could not load the contract'
  } finally {
    loadingContract.value = false
  }
}

function toggleContract() {
  contractOpen.value = !contractOpen.value
  if (contractOpen.value && !contract.value) loadContract()
}

function addContractColumn() {
  contractCols.value.push({ name: '', dtype: 'text', nullable: true, allowed: null })
}

function removeContractColumn(i: number) {
  contractCols.value.splice(i, 1)
}

function parseAllowed(raw: string): string[] | null {
  const parts = raw.split(',').map(s => s.trim()).filter(Boolean)
  return parts.length ? parts : null
}

function allowedText(col: ContractCol): string {
  return (col.allowed || []).join(', ')
}

async function saveContract() {
  if (!meta.value) return
  savingContract.value = true
  contractErr.value = ''
  contractMsg.value = ''
  contractCheckResult.value = null
  try {
    const payload = {
      columns: contractCols.value
        .filter(c => c.name.trim())
        .map(c => ({
          name: c.name.trim(),
          dtype: c.dtype,
          nullable: !!c.nullable,
          ...(parseAllowed(allowedText(c) || '') ? { allowed: parseAllowed(allowedText(c)) } : {}),
        })),
      on_violation: contractOnViolation.value,
    }
    const res = await api.put<ContractReport>(`/datasets/${meta.value.id}/contract`, payload)
    contract.value = res
    contractCols.value = res.columns.map(c => ({ ...c, allowed: c.allowed || null }))
    contractMsg.value = `Contract v${res.version} saved (${res.on_violation} mode) - every write is now checked`
    await loadHealth()
  } catch (e: any) {
    contractErr.value = e?.data?.detail || e?.message || 'Could not save the contract'
  } finally {
    savingContract.value = false
  }
}

async function deleteContract() {
  if (!meta.value) return
  savingContract.value = true
  contractErr.value = ''
  contractMsg.value = ''
  try {
    await api.del(`/datasets/${meta.value.id}/contract`)
    contract.value = { present: false, columns: [], on_violation: null, version: 0 }
    contractCols.value = []
    contractMsg.value = 'Contract removed - writes are no longer gated'
    await loadHealth()
  } catch (e: any) {
    contractErr.value = e?.data?.detail || e?.message || 'Could not remove the contract'
  } finally {
    savingContract.value = false
  }
}

async function checkNow() {
  if (!meta.value) return
  checkingContract.value = true
  contractErr.value = ''
  try {
    contractCheckResult.value = await api.post<any>(`/datasets/${meta.value.id}/contract/check`, { rows: [] })
  } catch (e: any) {
    contractErr.value = e?.data?.detail || e?.message || 'Check failed'
  } finally {
    checkingContract.value = false
  }
}

// ------------------------------------------------------------------ v53: ingestion checkpoints
interface IngestionStateRow {
  key: string
  watermark: string | null
  runs: number
  rows_total: number
  last_run_at: string | null
  updated_at: string | null
  stats: Record<string, any> | null
}
const ingestionStates = ref<IngestionStateRow[]>([])
const ingestionOpen = ref(false)
const loadingIngestion = ref(false)
const ingestionMsg = ref('')

async function loadIngestion() {
  loadingIngestion.value = true
  try {
    ingestionStates.value = await api.get<IngestionStateRow[]>(`/datasets/${ref_.value}/ingestion-states`)
  } catch (e: any) {
    ingestionMsg.value = e?.data?.detail || e?.message || 'Could not load checkpoints'
  } finally {
    loadingIngestion.value = false
  }
}

function toggleIngestion() {
  ingestionOpen.value = !ingestionOpen.value
  if (ingestionOpen.value && !ingestionStates.value.length) loadIngestion()
}

async function resetCheckpoint(key: string) {
  ingestionMsg.value = ''
  try {
    await api.del(`/datasets/${ref_.value}/ingestion-states/${encodeURIComponent(key)}`)
    ingestionMsg.value = `Checkpoint "${key}" reset - the next run re-ingests from scratch`
    await loadIngestion()
  } catch (e: any) {
    ingestionMsg.value = e?.data?.detail || e?.message || 'Could not reset the checkpoint'
  }
}
</script>

<template>
  <div class="pb-10 text-zinc-100">
    <header class="sticky top-0 z-20 border-b border-zinc-800/80 bg-zinc-950/90 backdrop-blur">
      <div class="mx-auto flex max-w-6xl items-center gap-3 px-4 py-3.5 lg:px-6">
        <NuxtLink to="/datasets" class="rounded-lg p-1.5 text-zinc-500 transition hover:bg-zinc-900 hover:text-zinc-200" title="All datasets">
          <ArrowLeft class="h-4 w-4" />
        </NuxtLink>
        <span class="flex h-9 w-9 items-center justify-center rounded-xl bg-sky-500/15">
          <Database class="h-4 w-4 text-sky-400" />
        </span>
        <div v-if="meta" class="min-w-0 flex-1">
          <h1 class="truncate text-base font-bold leading-tight">{{ meta.name }}</h1>
          <p class="text-xs text-zinc-500">
            {{ meta.description || 'No description' }} ·
            <span class="uppercase">{{ meta.source }}</span> · updated {{ fmtDate(meta.updated_at) }}
          </p>
        </div>
        <div v-if="meta" class="flex items-center gap-2 text-xs text-zinc-400">
          <span class="flex items-center gap-1.5 rounded-lg border border-zinc-800 bg-zinc-900/60 px-2.5 py-1.5">
            <Rows3 class="h-3.5 w-3.5 text-emerald-400" /> <b class="text-zinc-100">{{ meta.row_count.toLocaleString() }}</b> rows
          </span>
          <span class="hidden items-center gap-1.5 rounded-lg border border-zinc-800 bg-zinc-900/60 px-2.5 py-1.5 sm:flex">
            <Table2 class="h-3.5 w-3.5 text-sky-400" /> <b class="text-zinc-100">{{ meta.schema_json.length }}</b> cols
          </span>
          <!-- v45: export -->
          <div class="relative">
            <button
              class="flex items-center gap-1.5 rounded-lg border border-zinc-800 bg-zinc-900/60 px-2.5 py-1.5 text-zinc-400 transition hover:border-sky-500/40 hover:text-sky-400"
              title="Export dataset"
              @click="exportOpen = !exportOpen"
            >
              <Loader2 v-if="exporting" class="h-3.5 w-3.5 animate-spin" />
              <Download v-else class="h-3.5 w-3.5" />
              <span class="hidden sm:inline">Export</span>
            </button>
            <div
              v-if="exportOpen"
              class="absolute right-0 z-20 mt-1 w-36 overflow-hidden rounded-xl border border-zinc-800 bg-zinc-900 shadow-xl shadow-black/40"
            >
              <button
                v-for="fmt in EXPORT_FORMATS"
                :key="fmt"
                class="flex w-full items-center justify-between px-3 py-2 text-xs text-zinc-300 transition hover:bg-zinc-800/80 hover:text-sky-300"
                @click="exportDataset(fmt)"
              >
                <span class="uppercase">{{ fmt }}</span>
                <Loader2 v-if="exporting === fmt" class="h-3 w-3 animate-spin" />
              </button>
            </div>
          </div>
          <button
            class="rounded-lg border border-zinc-800 bg-zinc-900/60 p-1.5 text-zinc-500 transition hover:border-amber-500/40 hover:text-amber-400"
            title="Delete dataset"
            @click="removeDataset"
          >
            <Loader2 v-if="deleting" class="h-3.5 w-3.5 animate-spin" />
            <Trash2 v-else class="h-3.5 w-3.5" />
          </button>
        </div>
      </div>
    </header>

    <div v-if="loading" class="mt-10 flex justify-center text-zinc-500"><Loader2 class="h-6 w-6 animate-spin" /></div>
    <div v-else-if="!meta" class="mx-auto mt-16 max-w-6xl px-4 text-center text-sm text-zinc-400">
      <p>{{ error || 'Dataset not found' }}</p>
      <NuxtLink to="/datasets" class="mt-3 inline-block text-xs text-sky-400 hover:underline">← All datasets</NuxtLink>
    </div>

    <div v-else class="mx-auto max-w-6xl space-y-5 px-4 lg:px-6">
      <p v-if="error" class="mt-4 rounded-xl border border-amber-500/30 bg-amber-500/10 px-4 py-2.5 text-sm text-amber-300">{{ error }}</p>

      <!-- tags editor (v44) -->
      <div class="mt-5 flex flex-wrap items-center gap-1.5">
        <span
          v-for="t in meta.tags"
          :key="t"
          class="flex items-center gap-1 rounded-lg border border-orange-500/30 bg-orange-500/10 px-2 py-1 text-[11px] font-medium text-orange-300"
        >
          {{ t }}
          <button class="text-orange-400/60 transition hover:text-orange-200" title="Remove tag" @click="removeTag(t)">
            <X class="h-3 w-3" />
          </button>
        </span>
        <span class="flex items-center gap-1 rounded-lg border border-dashed border-zinc-700 px-2 py-1">
          <input
            v-model="newTag"
            class="w-24 bg-transparent text-[11px] text-zinc-200 outline-none placeholder:text-zinc-600"
            placeholder="add tag"
            @keyup.enter="addTag"
          />
          <button class="text-zinc-500 transition hover:text-orange-300 disabled:opacity-40" :disabled="!newTag.trim() || savingTags" title="Add tag" @click="addTag">
            <Loader2 v-if="savingTags" class="h-3 w-3 animate-spin" />
            <Plus v-else class="h-3 w-3" />
          </button>
        </span>
      </div>

      <!-- v50: health strip -->
      <div v-if="health" class="flex flex-wrap items-center gap-3 rounded-2xl border px-4 py-3" :class="healthStatusStyle[health.status] || healthStatusStyle.healthy">
        <span class="flex items-center gap-2 text-sm font-bold">
          <span class="h-2.5 w-2.5 animate-pulse rounded-full" :class="healthDot[health.status] || healthDot.healthy" />
          {{ health.status.toUpperCase() }} · {{ health.score }}/100
        </span>
        <span class="hidden h-4 w-px bg-current opacity-20 sm:block" />
        <span class="flex items-center gap-1.5 text-[11px]">
          <Activity class="h-3.5 w-3.5" />
          {{ health.signals.fresh ? 'Fresh' : 'Stale' }} · written {{ fmtAgeMin(health.freshness.age_minutes) }}
        </span>
        <span class="flex items-center gap-1.5 text-[11px]">
          <Table2 class="h-3.5 w-3.5" />
          {{ health.signals.schema_valid ? 'Schema valid' : `Contract violated (${health.schema.contract_violations.length} rule${health.schema.contract_violations.length === 1 ? '' : 's'})` }}
        </span>
        <span class="flex items-center gap-1.5 text-[11px]">
          <Rows3 class="h-3.5 w-3.5" />
          {{ health.volume.rows.toLocaleString() }} rows<template v-if="health.volume.delta !== null"> · {{ health.volume.delta >= 0 ? '+' : '' }}{{ health.volume.delta }} vs prev</template>
        </span>
        <span v-if="health.quality.completeness_pct !== null" class="hidden items-center gap-1.5 text-[11px] lg:flex">
          <BarChart3 class="h-3.5 w-3.5" />
          quality {{ health.quality.score }}/100 · {{ health.quality.completeness_pct }}% filled<template v-if="health.quality.duplicate_rows_pct"> · {{ health.quality.duplicate_rows_pct }}% dupes</template>
        </span>
        <Loader2 v-if="loadingHealth" class="ml-auto h-3.5 w-3.5 animate-spin" />
      </div>

      <!-- schema chips -->
      <div class="mt-5 flex flex-wrap gap-1.5">
        <span
          v-for="c in meta.schema_json"
          :key="c.name"
          class="rounded-lg border px-2 py-1 text-[11px] font-medium"
          :class="dtypeColor[c.dtype] || dtypeColor.text"
        >
          {{ c.name }} <span class="opacity-60">· {{ c.dtype }}</span>
        </span>
      </div>

      <div class="grid gap-5 lg:grid-cols-3">
        <!-- data preview -->
        <section class="lg:col-span-2">
          <div class="overflow-hidden rounded-2xl border border-zinc-800/80 bg-zinc-900/40">
            <div class="flex items-center justify-between border-b border-zinc-800/80 px-4 py-2.5">
              <h2 class="text-xs font-bold uppercase tracking-wider text-zinc-400">Data preview</h2>
              <div class="flex items-center gap-2 text-xs text-zinc-500">
                <span>rows {{ offset + 1 }}-{{ Math.min(offset + pageSize, meta.row_count) }} of {{ meta.row_count.toLocaleString() }}</span>
                <button
                  class="rounded-lg border border-zinc-800 p-1 transition hover:text-zinc-200 disabled:opacity-30"
                  :disabled="offset === 0 || loadingRows"
                  @click="offset = Math.max(0, offset - pageSize)"
                ><ChevronLeft class="h-3.5 w-3.5" /></button>
                <button
                  class="rounded-lg border border-zinc-800 p-1 transition hover:text-zinc-200 disabled:opacity-30"
                  :disabled="offset + pageSize >= meta.row_count || loadingRows"
                  @click="offset += pageSize"
                ><ChevronRight class="h-3.5 w-3.5" /></button>
              </div>
            </div>
            <div class="overflow-x-auto">
              <table v-if="rows.length" class="w-full text-left text-xs">
                <thead>
                  <tr class="border-b border-zinc-800/80 text-zinc-500">
                    <th class="px-3 py-2 font-medium">#</th>
                    <th v-for="c in columns" :key="c" class="whitespace-nowrap px-3 py-2 font-medium">{{ c }}</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="(r, i) in rows" :key="i" class="border-b border-zinc-800/40 last:border-0 hover:bg-zinc-900/60">
                    <td class="px-3 py-1.5 text-zinc-600">{{ offset + i + 1 }}</td>
                    <td v-for="c in columns" :key="c" class="max-w-[220px] truncate px-3 py-1.5 text-zinc-300" :title="fmtCell(r[c])">
                      {{ fmtCell(r[c]) }}
                    </td>
                  </tr>
                </tbody>
              </table>
              <p v-else class="px-4 py-8 text-center text-xs text-zinc-500">No rows yet - write some from a workflow or append via the API.</p>
            </div>
          </div>

          <!-- sql console -->
          <div class="mt-5 overflow-hidden rounded-2xl border border-zinc-800/80 bg-zinc-900/40">
            <div class="flex items-center justify-between border-b border-zinc-800/80 px-4 py-2.5">
              <h2 class="text-xs font-bold uppercase tracking-wider text-zinc-400">SQL console - DuckDB across all datasets</h2>
              <button
                class="flex items-center gap-1.5 rounded-lg bg-orange-500 px-3 py-1 text-xs font-semibold text-white transition hover:bg-orange-400 disabled:opacity-40"
                :disabled="running || !sql.trim()"
                @click="runSql"
              >
                <Loader2 v-if="running" class="h-3 w-3 animate-spin" />
                <Play v-else class="h-3 w-3" /> Run
              </button>
            </div>
            <div class="p-3">
              <textarea
                v-model="sql"
                rows="3"
                spellcheck="false"
                :placeholder="`SELECT * FROM ${meta.name.toLowerCase().replace(/[^a-z0-9_]/g, '_')} WHERE …`"
                class="w-full rounded-xl border border-zinc-800 bg-zinc-950/60 px-3 py-2 font-mono text-xs text-zinc-200 outline-none focus:border-orange-500/50"
              />
              <p v-if="sqlError" class="mt-2 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-300">{{ sqlError }}</p>
              <div v-if="sqlResult" class="mt-2">
                <p class="mb-1.5 text-[11px] text-zinc-500">
                  {{ sqlResult.row_count.toLocaleString() }} rows · {{ sqlResult.duration_ms }} ms ·
                  views: <span v-for="(v, k) in sqlResult.views" :key="k" class="mr-1 rounded bg-zinc-800 px-1.5 py-0.5 font-mono text-[10px] text-sky-300">{{ k }}</span>
                </p>
                <div class="max-h-72 overflow-auto rounded-xl border border-zinc-800">
                  <table class="w-full text-left text-xs">
                    <thead class="sticky top-0 bg-zinc-900">
                      <tr class="text-zinc-500">
                        <th v-for="c in sqlResult.columns" :key="c" class="whitespace-nowrap px-3 py-2 font-medium">{{ c }}</th>
                      </tr>
                    </thead>
                    <tbody>
                      <tr v-for="(r, i) in sqlResult.rows.slice(0, 200)" :key="i" class="border-t border-zinc-800/40 hover:bg-zinc-900/60">
                        <td v-for="c in sqlResult.columns" :key="c" class="max-w-[240px] truncate px-3 py-1.5 text-zinc-300" :title="fmtCell(r[c])">{{ fmtCell(r[c]) }}</td>
                      </tr>
                    </tbody>
                  </table>
                  <p v-if="sqlResult.rows.length === 0" class="px-3 py-4 text-center text-xs text-zinc-500">Query returned no rows</p>
                </div>
              </div>
            </div>
          </div>
        </section>

        <!-- profile -->
        <section>
          <div class="rounded-2xl border border-zinc-800/80 bg-zinc-900/40">
            <div class="flex items-center gap-2 border-b border-zinc-800/80 px-4 py-2.5">
              <BarChart3 class="h-3.5 w-3.5 text-emerald-400" />
              <h2 class="text-xs font-bold uppercase tracking-wider text-zinc-400">Column profile</h2>
              <Loader2 v-if="loadingProfile" class="ml-auto h-3.5 w-3.5 animate-spin text-zinc-500" />
            </div>
            <div v-if="profile" class="divide-y divide-zinc-800/40">
              <!-- v45: dataset-level stats -->
              <div class="grid grid-cols-2 gap-x-3 gap-y-1 bg-zinc-900/60 px-4 py-3 text-[11px] text-zinc-400">
                <span>completeness <b class="text-zinc-200">{{ profile.completeness_pct }}%</b></span>
                <span>dup rows <b class="text-zinc-200">{{ profile.duplicate_rows }}</b></span>
                <span>constant cols <b class="text-zinc-200">{{ profile.constant_columns.length || 0 }}</b></span>
                <span>correlations <b class="text-zinc-200">{{ profile.correlation.length }}</b></span>
              </div>
              <div v-for="c in profile.columns" :key="c.name" class="px-4 py-3">
                <div class="flex items-center justify-between gap-2">
                  <p class="truncate font-mono text-xs font-semibold text-zinc-200">{{ c.name }}</p>
                  <span
                    class="rounded border px-1.5 py-0.5 text-[10px] font-medium"
                    :class="dtypeColor[c.dtype] || dtypeColor.text"
                  >{{ c.dtype }}</span>
                </div>
                <p class="mt-1 text-[11px] text-zinc-500">
                  {{ c.non_null }} non-null ({{ c.null_pct }}% empty) · {{ c.unique }} unique
                </p>
                <p v-if="c.min !== undefined" class="mt-1 text-[11px] text-zinc-400">
                  min <b class="text-zinc-200">{{ fmtCell(c.min) }}</b> · max <b class="text-zinc-200">{{ fmtCell(c.max) }}</b> · mean <b class="text-zinc-200">{{ Number(c.mean ?? 0).toFixed(2) }}</b>
                </p>
                <p v-if="c.median !== undefined" class="mt-1 text-[11px] text-zinc-500">
                  median <b class="text-zinc-300">{{ Number(c.median).toFixed(2) }}</b> · q25 <b class="text-zinc-300">{{ Number(c.q25).toFixed(2) }}</b> · q75 <b class="text-zinc-300">{{ Number(c.q75).toFixed(2) }}</b> · std <b class="text-zinc-300">{{ Number(c.std ?? 0).toFixed(2) }}</b>
                </p>
                <p v-if="c.outliers_iqr" class="mt-1 text-[11px] text-amber-400">
                  ⚠ {{ c.outliers_iqr }} IQR outlier{{ c.outliers_iqr === 1 ? '' : 's' }} outside [{{ Number(c.outlier_lower).toFixed(1) }}, {{ Number(c.outlier_upper).toFixed(1) }}]
                </p>
                <p v-if="c.parsed_as_datetime" class="mt-1 text-[11px] text-sky-400">
                  spans {{ c.span_days }} days ({{ c.datetime_min?.slice(0, 10) }} → {{ c.datetime_max?.slice(0, 10) }})
                </p>
                <div v-if="c.top_values?.length" class="mt-1.5 flex flex-wrap gap-1">
                  <span
                    v-for="t in c.top_values"
                    :key="t.value"
                    class="rounded bg-zinc-800/80 px-1.5 py-0.5 text-[10px] text-zinc-300"
                  >{{ t.value }} <span class="text-zinc-500">×{{ t.count }}</span></span>
                </div>
                <!-- v45: correlation row -->
                <div v-if="corrFor(c.name)" class="mt-1.5 flex flex-wrap gap-1">
                  <span
                    v-for="(v, other) in corrFor(c.name)"
                    :key="other"
                    class="rounded bg-zinc-800/60 px-1.5 py-0.5 text-[10px] text-zinc-400"
                  >↔ {{ other }} <b class="text-zinc-200">{{ v }}</b></span>
                </div>
              </div>
            </div>
            <p v-else class="px-4 py-6 text-center text-xs text-zinc-500">No profile available</p>
          </div>

          <!-- version timeline (v44) -->
          <div class="mt-5 overflow-hidden rounded-2xl border border-zinc-800/80 bg-zinc-900/40">
            <div class="flex items-center justify-between border-b border-zinc-800/80 px-4 py-2.5">
              <h2 class="flex items-center gap-2 text-xs font-bold uppercase tracking-wider text-zinc-400">
                <History class="h-3.5 w-3.5 text-violet-400" /> Version timeline
              </h2>
              <span class="text-[11px] text-zinc-500">{{ versions.length }} snapshot{{ versions.length === 1 ? '' : 's' }} (cap 20)</span>
            </div>
            <p v-if="versionMsg" class="border-b border-emerald-500/20 bg-emerald-500/10 px-4 py-2 text-[11px] text-emerald-300">{{ versionMsg }}</p>
            <div v-if="loadingVersions" class="flex items-center justify-center py-6 text-zinc-500">
              <Loader2 class="h-4 w-4 animate-spin" />
            </div>
            <p v-else-if="!versions.length" class="px-4 py-5 text-center text-xs text-zinc-500">No snapshots yet</p>
            <div v-else class="max-h-80 divide-y divide-zinc-800/40 overflow-y-auto">
              <div v-for="v in versions" :key="v.id" class="flex items-center gap-2 px-4 py-2.5">
                <span class="w-9 shrink-0 font-mono text-xs font-bold text-zinc-200">v{{ v.version }}</span>
                <span
                  class="shrink-0 rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide"
                  :class="SOURCE_STYLE[v.source] || 'bg-zinc-800 text-zinc-400'"
                >{{ v.source }}</span>
                <span class="shrink-0 text-[11px] tabular-nums text-zinc-500">{{ v.row_count.toLocaleString() }} rows</span>
                <span class="min-w-0 flex-1 truncate text-[11px] text-zinc-600">{{ fmtDate(v.created_at) }}{{ v.note ? ` · ${v.note}` : '' }}</span>
                <span v-if="v.current" class="shrink-0 rounded bg-emerald-500/15 px-1.5 py-0.5 text-[10px] font-semibold text-emerald-400">CURRENT</span>
                <span v-else-if="!v.file_exists" class="shrink-0 rounded bg-zinc-800 px-1.5 py-0.5 text-[10px] text-zinc-500">no file</span>
                <div class="flex shrink-0 items-center gap-1">
                  <button
                    v-if="v.file_exists"
                    class="rounded-lg border border-zinc-800 p-1 text-zinc-500 transition hover:border-sky-500/40 hover:text-sky-300 disabled:opacity-40"
                    :disabled="busyVersion === v.version"
                    title="Preview this snapshot"
                    @click="previewVersion(v)"
                  ><Eye class="h-3.5 w-3.5" /></button>
                  <button
                    v-if="v.file_exists && !v.current"
                    class="rounded-lg border border-zinc-800 p-1 text-zinc-500 transition hover:border-amber-500/40 hover:text-amber-300 disabled:opacity-40"
                    :disabled="busyVersion === v.version"
                    title="Roll back to this snapshot"
                    @click="restoreVersion(v)"
                  ><Undo2 class="h-3.5 w-3.5" /></button>
                  <button
                    class="rounded-lg border border-zinc-800 p-1 text-zinc-500 transition hover:border-rose-500/40 hover:text-rose-300 disabled:opacity-40"
                    :disabled="busyVersion === v.version"
                    title="Delete snapshot"
                    @click="deleteVersion(v)"
                  ><Trash2 class="h-3.5 w-3.5" /></button>
                </div>
              </div>
            </div>
          </div>

          <!-- v47: provenance lineage -->
          <div class="mt-5 overflow-hidden rounded-2xl border border-zinc-800/80 bg-zinc-900/40">
            <button class="flex w-full items-center justify-between border-b border-zinc-800/80 px-4 py-2.5 text-left" @click="toggleLineage">
              <h2 class="flex items-center gap-2 text-xs font-bold uppercase tracking-wider text-zinc-400">
                <GitBranch class="h-3.5 w-3.5 text-cyan-400" /> Lineage
              </h2>
              <span class="flex items-center gap-2 text-[11px] text-zinc-500">
                {{ lineage ? `${lineage.workflow_versions} workflow version${lineage.workflow_versions === 1 ? '' : 's'}` : 'provenance timeline' }}
                <ChevronDown class="h-3.5 w-3.5 transition" :class="lineageOpen && 'rotate-180'" />
              </span>
            </button>
            <div v-if="lineageOpen">
              <div v-if="loadingLineage" class="flex items-center justify-center py-6 text-zinc-500">
                <Loader2 class="h-4 w-4 animate-spin" />
              </div>
              <p v-else-if="!lineage?.steps.length" class="px-4 py-5 text-center text-xs text-zinc-500">No versions yet</p>
              <div v-else class="max-h-80 divide-y divide-zinc-800/40 overflow-y-auto">
                <div
                  v-for="s in lineage.steps"
                  :key="s.version"
                  class="flex flex-wrap items-center gap-2 px-4 py-2.5"
                  :class="s.origin === 'workflow' ? 'border-l-2 border-orange-500/50' : 'border-l-2 border-transparent'"
                  :title="s.execution_id ? `execution ${s.execution_id}` : (s.note || s.source)"
                >
                  <span class="w-9 shrink-0 font-mono text-xs font-bold text-zinc-200">v{{ s.version }}</span>
                  <span class="shrink-0 text-[11px] tabular-nums text-zinc-500">{{ s.row_count.toLocaleString() }} rows</span>
                  <span
                    class="min-w-0 flex-1 truncate text-[11px]"
                    :class="s.origin === 'workflow' ? 'font-medium text-orange-300/90' : 'text-zinc-600'"
                  >{{ lineageOriginLabel(s) }}</span>
                  <span class="shrink-0 text-[11px] text-zinc-600">{{ fmtDate(s.created_at) }}</span>
                </div>
              </div>
            </div>
          </div>
        </section>
      </div>

      <!-- v50: data contract -->
      <div class="overflow-hidden rounded-2xl border border-zinc-800/80 bg-zinc-900/40">
        <button class="flex w-full items-center justify-between border-b border-zinc-800/80 px-4 py-2.5 text-left" @click="toggleContract">
          <h2 class="flex items-center gap-2 text-xs font-bold uppercase tracking-wider text-zinc-400">
            <component :is="contract?.present && contract.on_violation === 'error' ? ShieldAlert : ShieldCheck" class="h-3.5 w-3.5" :class="contract?.present ? (contract.on_violation === 'error' ? 'text-rose-400' : 'text-amber-400') : 'text-zinc-500'" />
            Data contract
          </h2>
          <span class="flex items-center gap-2 text-[11px] text-zinc-500">
            <template v-if="contract?.present">
              v{{ contract.version }} · {{ contract.on_violation }} mode · every write is checked
            </template>
            <template v-else>No contract - writes are ungated</template>
            <ChevronDown class="h-3.5 w-3.5 transition" :class="contractOpen && 'rotate-180'" />
          </span>
        </button>

        <div v-if="contractOpen">
          <p class="border-b border-zinc-800/60 bg-zinc-900/60 px-4 py-2.5 text-[11px] leading-relaxed text-zinc-400">
            A contract is the schema this dataset promises: per column a type (castability-checked, so "7" counts as an
            integer), nullability and an optional comma-separated allowed-value domain.
            <b class="text-zinc-200">Error mode</b> hard-stops every write that violates it (workflows fail, the API returns 422);
            <b class="text-zinc-200">warn mode</b> lets the write land and reports the violations on the output and in the health strip.
          </p>

          <p v-if="contractMsg" class="border-b border-emerald-500/20 bg-emerald-500/10 px-4 py-2 text-[11px] text-emerald-300">{{ contractMsg }}</p>
          <p v-if="contractErr" class="border-b border-rose-500/20 bg-rose-500/10 px-4 py-2 text-[11px] text-rose-300">{{ contractErr }}</p>

          <div v-if="loadingContract" class="flex items-center justify-center py-6 text-zinc-500">
            <Loader2 class="h-4 w-4 animate-spin" />
          </div>

          <div v-else class="px-4 py-4">
            <div class="overflow-x-auto rounded-xl border border-zinc-800">
              <table class="w-full text-left text-xs">
                <thead class="bg-zinc-900/80 text-zinc-500">
                  <tr>
                    <th class="px-3 py-2 font-medium">Column</th>
                    <th class="px-3 py-2 font-medium">Type</th>
                    <th class="px-3 py-2 font-medium">Nullable</th>
                    <th class="px-3 py-2 font-medium">Allowed values</th>
                    <th class="px-3 py-2"></th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="(c, i) in contractCols" :key="i" class="border-t border-zinc-800/60">
                    <td class="px-3 py-1.5">
                      <input
                        v-model="c.name"
                        class="w-full rounded-lg border border-zinc-800 bg-zinc-950 px-2 py-1 font-mono text-[11px] outline-none focus:border-orange-500/60"
                        placeholder="column_name"
                      />
                    </td>
                    <td class="px-3 py-1.5">
                      <select
                        v-model="c.dtype"
                        class="rounded-lg border border-zinc-800 bg-zinc-950 px-2 py-1 text-[11px] outline-none focus:border-orange-500/60"
                      >
                        <option v-for="d in DTYPES" :key="d" :value="d">{{ d }}</option>
                      </select>
                    </td>
                    <td class="px-3 py-1.5">
                      <input v-model="c.nullable" type="checkbox" class="h-3.5 w-3.5 accent-orange-500" />
                    </td>
                    <td class="px-3 py-1.5">
                      <input
                        class="w-full rounded-lg border border-zinc-800 bg-zinc-950 px-2 py-1 font-mono text-[11px] outline-none placeholder:text-zinc-600 focus:border-orange-500/60"
                        placeholder="e.g. active, inactive (blank = any)"
                        :value="allowedText(c)"
                        @input="c.allowed = parseAllowed($event.target.value)"
                      />
                    </td>
                    <td class="px-3 py-1.5 text-right">
                      <button class="rounded-lg border border-zinc-800 p-1 text-zinc-500 transition hover:border-rose-500/40 hover:text-rose-300" title="Remove column" @click="removeContractColumn(i)">
                        <TrashIcon class="h-3.5 w-3.5" />
                      </button>
                    </td>
                  </tr>
                  <tr v-if="!contractCols.length">
                    <td colspan="5" class="px-3 py-4 text-center text-[11px] text-zinc-500">No columns defined yet - add one, or mirror the current schema</td>
                  </tr>
                </tbody>
              </table>
            </div>

            <div class="mt-3 flex flex-wrap items-center gap-2">
              <button
                class="flex items-center gap-1.5 rounded-xl border border-zinc-800 bg-zinc-900 px-3 py-1.5 text-xs font-medium text-zinc-300 transition hover:border-sky-500/40 hover:text-sky-300"
                @click="addContractColumn"
              >
                <Plus class="h-3.5 w-3.5" /> Add column
              </button>
              <button
                class="flex items-center gap-1.5 rounded-xl border border-zinc-800 bg-zinc-900 px-3 py-1.5 text-xs font-medium text-zinc-300 transition hover:border-violet-500/40 hover:text-violet-300 disabled:opacity-50"
                :disabled="checkingContract || !contract?.present"
                title="Lint the CURRENT dataset contents against this contract"
                @click="checkNow"
              >
                <Loader2 v-if="checkingContract" class="h-3.5 w-3.5 animate-spin" />
                <Play v-else class="h-3.5 w-3.5" /> Check current data
              </button>
              <div class="flex-1"></div>
              <label class="flex items-center gap-2 text-[11px] text-zinc-400">
                On violation
                <select
                  v-model="contractOnViolation"
                  class="rounded-lg border border-zinc-800 bg-zinc-950 px-2 py-1.5 text-[11px] outline-none focus:border-orange-500/60"
                >
                  <option value="warn">warn (write + report)</option>
                  <option value="error">error (hard-stop)</option>
                </select>
              </label>
              <button
                class="flex items-center gap-1.5 rounded-xl bg-orange-500 px-3.5 py-1.5 text-xs font-semibold text-white shadow-lg shadow-orange-500/20 transition hover:bg-orange-400 disabled:opacity-50"
                :disabled="savingContract"
                @click="saveContract"
              >
                <Loader2 v-if="savingContract" class="h-3.5 w-3.5 animate-spin" />
                <ShieldCheck v-else class="h-3.5 w-3.5" />
                Save contract
              </button>
              <button
                v-if="contract?.present"
                class="rounded-xl border border-zinc-800 px-3 py-1.5 text-xs font-medium text-zinc-400 transition hover:border-rose-500/40 hover:text-rose-300 disabled:opacity-50"
                :disabled="savingContract"
                title="Remove the contract - writes stop being gated"
                @click="deleteContract"
              >
                Remove
              </button>
            </div>

            <!-- check result -->
            <div v-if="contractCheckResult" class="mt-3 rounded-xl border px-3.5 py-3 text-xs" :class="contractCheckResult.ok ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-300' : 'border-rose-500/30 bg-rose-500/10 text-rose-300'">
              <p class="font-semibold">
                {{ contractCheckResult.ok ? 'Current data satisfies the contract' : `Current data violates ${contractCheckResult.violations.length} rule${contractCheckResult.violations.length === 1 ? '' : 's'}` }}
                <span class="font-normal opacity-70">({{ contractCheckResult.checked_rows }} rows checked · contract v{{ contractCheckResult.contract_version }})</span>
              </p>
              <div v-if="contractCheckResult.violations.length" class="mt-2 space-y-1">
                <p v-for="(v, i) in contractCheckResult.violations" :key="i" class="font-mono text-[11px]">
                  {{ v.column }} · {{ v.rule }} · {{ v.count }} row(s) · e.g. {{ v.samples.slice(0, 3).join(' | ') }}
                </p>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- v53: incremental ingestion checkpoints -->
      <div class="overflow-hidden rounded-2xl border border-zinc-800/80 bg-zinc-900/40">
        <button class="flex w-full items-center justify-between border-b border-zinc-800/80 px-4 py-2.5 text-left" @click="toggleIngestion">
          <h2 class="flex items-center gap-2 text-xs font-bold uppercase tracking-wider text-zinc-400">
            <GitBranch class="h-3.5 w-3.5 text-lime-400" />
            Ingestion checkpoints
          </h2>
          <span class="flex items-center gap-2 text-[11px] text-zinc-500">
            {{ ingestionStates.length ? `${ingestionStates.length} pipeline${ingestionStates.length === 1 ? '' : 's'} feeding this dataset` : 'where incremental pipelines left off' }}
            <ChevronDown class="h-3.5 w-3.5 transition" :class="ingestionOpen && 'rotate-180'" />
          </span>
        </button>

        <div v-if="ingestionOpen">
          <p class="border-b border-zinc-800/60 bg-zinc-900/60 px-4 py-2.5 text-[11px] leading-relaxed text-zinc-400">
            One checkpoint per pipeline (key): dataset_write in incremental or upsert+watermark mode only writes rows
            beyond the stored watermark, then advances it. <b class="text-zinc-200">lookback</b> re-admits boundary rows
            (units for numeric cursors, seconds for ISO) so late arrivals merge instead of vanish. Resetting a checkpoint
            makes the next run re-ingest from scratch.
          </p>

          <p v-if="ingestionMsg" class="border-b border-emerald-500/20 bg-emerald-500/10 px-4 py-2 text-[11px] text-emerald-300">{{ ingestionMsg }}</p>

          <div v-if="loadingIngestion" class="flex items-center justify-center py-6 text-zinc-500">
            <Loader2 class="h-4 w-4 animate-spin" />
          </div>

          <div v-else-if="!ingestionStates.length" class="px-4 py-6 text-center text-[11px] text-zinc-600">
            No checkpoints yet - point a dataset_write node (mode=incremental, or upsert with a watermark_column) at this dataset.
          </div>

          <div v-else class="divide-y divide-zinc-800/40">
            <div v-for="s in ingestionStates" :key="s.key" class="flex flex-wrap items-start gap-3 px-4 py-3">
              <div class="min-w-0 flex-1">
                <div class="flex flex-wrap items-center gap-2">
                  <span class="rounded bg-zinc-800 px-1.5 py-0.5 font-mono text-[10px] text-zinc-300">{{ s.key }}</span>
                  <span class="font-mono text-[10px] text-zinc-500" title="Stored watermark">wm: {{ s.watermark ?? 'null' }}</span>
                  <span class="text-[10px] text-zinc-600">{{ s.runs }} runs · {{ s.rows_total.toLocaleString() }} rows in · last {{ s.last_run_at ? new Date(s.last_run_at).toLocaleString() : 'never' }}</span>
                </div>
                <p v-if="s.stats" class="mt-1 flex flex-wrap gap-1.5 text-[10px] text-zinc-500">
                  <span class="rounded bg-zinc-800/60 px-1.5 py-0.5">mode: {{ s.stats.mode }}</span>
                  <span class="rounded bg-zinc-800/60 px-1.5 py-0.5">rows_in: {{ s.stats.rows_in }}</span>
                  <span class="rounded bg-emerald-500/10 px-1.5 py-0.5 text-emerald-300">written: {{ s.stats.written }}</span>
                  <span class="rounded bg-zinc-800/60 px-1.5 py-0.5">skipped: {{ s.stats.skipped }}</span>
                  <template v-if="s.stats.updated != null">
                    <span class="rounded bg-sky-500/10 px-1.5 py-0.5 text-sky-300">updated: {{ s.stats.updated }}</span>
                    <span class="rounded bg-zinc-800/60 px-1.5 py-0.5">inserted: {{ s.stats.inserted }}</span>
                  </template>
                  <span v-if="s.stats.lookback" class="rounded bg-amber-500/10 px-1.5 py-0.5 text-amber-300">lookback: {{ s.stats.lookback }}</span>
                </p>
              </div>
              <button
                class="flex items-center gap-1.5 rounded-lg border border-zinc-800 px-2.5 py-1.5 text-[10px] font-medium text-zinc-400 transition hover:border-rose-500/40 hover:text-rose-300"
                title="Reset this checkpoint - the next run re-ingests everything"
                @click="resetCheckpoint(s.key)"
              >
                <Trash2 class="h-3 w-3" /> reset
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- version preview modal (v44) -->
    <Teleport to="body">
      <div
        v-if="versionPreview"
        class="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4 backdrop-blur-sm"
        @click.self="versionPreview = null"
      >
        <div class="max-h-[85vh] w-full max-w-2xl overflow-y-auto rounded-2xl border border-zinc-800 bg-zinc-950 shadow-2xl">
          <div class="sticky top-0 flex items-center justify-between border-b border-zinc-800/80 bg-zinc-950 px-5 py-4">
            <h2 class="text-sm font-bold">Snapshot v{{ versionPreview.version }} preview</h2>
            <button class="rounded-lg p-1 text-zinc-500 transition hover:text-zinc-200" @click="versionPreview = null">
              <X class="h-4 w-4" />
            </button>
          </div>
          <div class="px-5 py-4">
            <p class="mb-2 text-[11px] text-zinc-500">showing {{ versionPreview.shown }} row(s)</p>
            <div class="overflow-x-auto rounded-xl border border-zinc-800">
              <table class="w-full text-left text-xs">
                <thead class="bg-zinc-900">
                  <tr class="text-zinc-500">
                    <th v-for="c in versionPreview.columns" :key="c" class="whitespace-nowrap px-3 py-2 font-medium">{{ c }}</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="(r, i) in versionPreview.rows" :key="i" class="border-t border-zinc-800/40 hover:bg-zinc-900/60">
                    <td v-for="c in versionPreview.columns" :key="c" class="max-w-[240px] truncate px-3 py-1.5 text-zinc-300" :title="fmtCell(r[c])">{{ fmtCell(r[c]) }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

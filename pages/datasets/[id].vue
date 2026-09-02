<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue'
import {
  Database, Trash2, Loader2, ArrowLeft, Rows3, ChevronLeft, ChevronRight,
  Play, BarChart3, Table2, X, History, Undo2, Plus, Eye, Download,
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
    await Promise.all([loadRows(), loadProfile(), loadVersions()])
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
        </section>
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

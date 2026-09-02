<script setup lang="ts">
import { ref, onMounted } from 'vue'
import {
  FileBarChart, Plus, Loader2, Trash2, XCircle, Play, Power, Download, CalendarClock, CheckCircle2, Eye, X,
} from 'lucide-vue-next'
import { useApi } from '~/composables/useApi'

// v48: scheduled report exports - point a cron at a dataset (csv/xlsx/json/
// parquet) or a dashboard (JSON snapshot or v49 PNG image of every rendered
// component); the platform writes an Artifact each run and the row remembers
// the last one.
interface ScheduledReport {
  id: string
  name: string
  source_type: 'dataset' | 'dashboard'
  source_id: string
  source_name: string | null
  fmt: string
  cron: string
  enabled: boolean
  created_at: string | null
  last_run_at: string | null
  fire_count: number
  last_status: string | null
  last_error: string | null
  last_artifact_id: string | null
  next_runs?: string[]
}

const { api, download, blobUrl } = useApi()
const loading = ref(true)
const reports = ref<ScheduledReport[]>([])
const datasets = ref<{ id: string; name: string }[]>([])
const dashboards = ref<{ id: string; name: string }[]>([])

const showCreate = ref(false)
const formName = ref('')
const formSourceType = ref<'dataset' | 'dashboard'>('dataset')
const formSourceId = ref('')
const formFmt = ref('csv')
const formCron = ref('0 6 * * *')
const formEnabled = ref(true)
const formSaving = ref(false)
const formError = ref('')

const running = ref<string | null>(null)
const toggling = ref<string | null>(null)
const pageError = ref('')
const pageNotice = ref('')

const FMTS_BY_TYPE: Record<string, string[]> = {
  dataset: ['csv', 'xlsx', 'json', 'parquet'],
  dashboard: ['json', 'png'],
}

const CRON_HINTS = [
  { label: 'Every day 06:00', value: '0 6 * * *' },
  { label: 'Weekdays 08:00', value: '0 8 * * 1-5' },
  { label: 'Mondays 07:00', value: '0 7 * * 1' },
  { label: '1st of month 06:00', value: '0 6 1 * *' },
]

async function loadAll() {
  loading.value = true
  pageError.value = ''
  try {
    reports.value = await api.get<ScheduledReport[]>('/reports')
    const dsList = await api.get<any[]>('/datasets')
    datasets.value = dsList.map((d) => ({ id: d.id, name: d.name }))
    const dbList = await api.get<any[]>('/dashboards')
    dashboards.value = dbList.map((d) => ({ id: d.id, name: d.name }))
    // fire previews, best-effort, in parallel
    await Promise.all(
      reports.value.map(async (r) => {
        try {
          const runs = await api.get<{ next_runs: string[] }>(`/reports/${r.id}/runs`)
          r.next_runs = runs.next_runs
        } catch {
          r.next_runs = []
        }
      }),
    )
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'Load failed'
  } finally {
    loading.value = false
  }
}

onMounted(loadAll)

function openCreate() {
  formName.value = ''
  formSourceType.value = 'dataset'
  formSourceId.value = ''
  formFmt.value = 'csv'
  formCron.value = '0 6 * * *'
  formEnabled.value = true
  formError.value = ''
  showCreate.value = true
}

function switchType() {
  formFmt.value = FMTS_BY_TYPE[formSourceType.value][0]
  formSourceId.value = ''
}

function sourceOptions() {
  return formSourceType.value === 'dataset' ? datasets.value : dashboards.value
}

async function saveForm() {
  if (!formName.value.trim() || !formSourceId.value) return
  formSaving.value = true
  formError.value = ''
  try {
    await api.post('/reports', {
      name: formName.value.trim(),
      source_type: formSourceType.value,
      source_id: formSourceId.value,
      fmt: formFmt.value,
      cron: formCron.value.trim(),
      enabled: formEnabled.value,
    })
    showCreate.value = false
    await loadAll()
  } catch (e: any) {
    formError.value = e?.data?.detail || e?.message || 'Save failed'
  } finally {
    formSaving.value = false
  }
}

async function runNow(r: ScheduledReport) {
  running.value = r.id
  pageError.value = ''
  pageNotice.value = ''
  try {
    await api.post<any>(`/reports/${r.id}/run`)
    pageNotice.value = `Exported "${r.name}" - artifact ready to download`
    await loadAll()
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'Run failed'
  } finally {
    running.value = null
  }
}

async function toggleReport(r: ScheduledReport) {
  toggling.value = r.id
  try {
    await api.put(`/reports/${r.id}`, { enabled: !r.enabled })
    r.enabled = !r.enabled
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'Toggle failed'
  } finally {
    toggling.value = null
  }
}

async function removeReport(r: ScheduledReport) {
  if (!confirm(`Delete report "${r.name}"? The generated artifacts stay in Artifacts.`)) return
  try {
    await api.del(`/reports/${r.id}`)
    await loadAll()
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'Delete failed'
  }
}

async function downloadLast(r: ScheduledReport) {
  if (!r.last_artifact_id) return
  try {
    await download(`/artifacts/${r.last_artifact_id}/content`, `${r.name}.${r.fmt}`)
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'Download failed'
  }
}

// v49: inline preview of dashboard PNG snapshots (image reports only)
const previewUrl = ref('')
const previewName = ref('')
const previewLoading = ref(false)
let previewReport: ScheduledReport | null = null

async function openPreview(r: ScheduledReport) {
  if (!r.last_artifact_id) return
  previewLoading.value = true
  pageError.value = ''
  try {
    const url = await blobUrl(`/artifacts/${r.last_artifact_id}/content`)
    if (previewUrl.value) URL.revokeObjectURL(previewUrl.value)
    previewUrl.value = url
    previewName.value = r.name
    previewReport = r
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'Preview failed'
  } finally {
    previewLoading.value = false
  }
}

function closePreview() {
  if (previewUrl.value) URL.revokeObjectURL(previewUrl.value)
  previewUrl.value = ''
  previewName.value = ''
  previewReport = null
}

function fmtDate(iso: string | null) {
  if (!iso) return 'never'
  return new Date(iso).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

function nextRun(r: ScheduledReport): string {
  if (!r.next_runs || !r.next_runs.length) return '-'
  return fmtDate(r.next_runs[0])
}
</script>

<template>
  <div class="text-zinc-100">
    <header class="sticky top-0 z-20 border-b border-zinc-800/80 bg-zinc-950/90 backdrop-blur">
      <div class="mx-auto flex max-w-7xl items-center justify-between gap-3 px-4 py-3.5 sm:px-6">
        <div class="flex items-center gap-3">
          <div class="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-cyan-500 to-sky-600 shadow-lg shadow-cyan-500/20">
            <FileBarChart class="h-4 w-4 text-white" />
          </div>
          <div>
            <h1 class="text-lg font-bold tracking-tight">Reports</h1>
            <p class="-mt-0.5 text-[11px] text-zinc-500">Scheduled exports - a fresh file on a cron, waiting in Artifacts</p>
          </div>
        </div>
        <button
          class="flex items-center gap-1.5 rounded-xl bg-cyan-500 px-3.5 py-2 text-sm font-semibold text-white shadow-lg shadow-cyan-500/20 transition hover:bg-cyan-400 active:scale-[0.98]"
          @click="openCreate"
        >
          <Plus class="h-4 w-4" /> New report
        </button>
      </div>
    </header>

    <main class="mx-auto max-w-7xl px-4 py-6 sm:px-6">
      <!-- how it works -->
      <div class="mb-6 flex items-start gap-3 rounded-2xl border border-zinc-800 bg-zinc-900/40 p-4">
        <CalendarClock class="mt-0.5 h-4 w-4 shrink-0 text-cyan-400" />
        <p class="text-xs leading-relaxed text-zinc-400">
          Each report snapshots its source on a crontab (UTC): datasets export as
          <code class="rounded bg-zinc-800 px-1 font-mono text-[11px] text-cyan-300">csv / xlsx / json / parquet</code>,
          dashboards as a <code class="rounded bg-zinc-800 px-1 font-mono text-[11px] text-cyan-300">JSON</code> snapshot
          or a <code class="rounded bg-zinc-800 px-1 font-mono text-[11px] text-cyan-300">PNG</code> image of every rendered
          component. Every run writes a regular Artifact and the row keeps the freshest one - use
          <code class="rounded bg-zinc-800 px-1 font-mono text-[11px] text-zinc-300">Run now</code> to verify wiring instantly.
        </p>
      </div>

      <div v-if="pageError" class="mb-4 flex items-start gap-2 rounded-xl border border-rose-500/40 bg-rose-500/10 px-4 py-3 text-sm text-rose-300">
        <XCircle class="mt-0.5 h-4 w-4 shrink-0" /> {{ pageError }}
      </div>
      <div v-if="pageNotice" class="mb-4 flex items-start gap-2 rounded-xl border border-emerald-500/40 bg-emerald-500/10 px-4 py-3 text-sm text-emerald-300">
        <CheckCircle2 class="mt-0.5 h-4 w-4 shrink-0" /> {{ pageNotice }}
      </div>

      <div v-if="loading" class="grid place-items-center py-16 text-zinc-600">
        <Loader2 class="h-6 w-6 animate-spin" />
      </div>

      <div v-else-if="!reports.length" class="grid place-items-center rounded-2xl border border-dashed border-zinc-800 py-16 text-center">
        <FileBarChart class="mb-3 h-8 w-8 text-zinc-700" />
        <p class="text-sm font-medium text-zinc-400">No scheduled reports yet</p>
        <p class="mt-1 max-w-md text-xs text-zinc-600">
          Create one to land a fresh CSV of your CRM dataset - or a snapshot of the weekly board - in Artifacts every morning.
        </p>
      </div>

      <div v-else class="space-y-2.5">
        <div
          v-for="r in reports"
          :key="r.id"
          class="rounded-2xl border border-zinc-800/80 bg-zinc-900/50 p-4 transition hover:border-zinc-700"
        >
          <div class="flex flex-wrap items-center gap-3">
            <div
              class="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl border"
              :class="r.enabled ? 'border-cyan-500/30 bg-cyan-500/10 text-cyan-400' : 'border-zinc-700 bg-zinc-800 text-zinc-500'"
            >
              <CalendarClock class="h-4 w-4" />
            </div>
            <div class="min-w-0 flex-1">
              <div class="flex flex-wrap items-center gap-2">
                <span class="truncate text-sm font-semibold">{{ r.name }}</span>
                <span
                  class="rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide"
                  :class="r.enabled ? 'bg-emerald-500/15 text-emerald-400' : 'bg-zinc-800 text-zinc-500'"
                >{{ r.enabled ? 'Enabled' : 'Paused' }}</span>
                <span class="rounded bg-zinc-800 px-1.5 py-0.5 text-[10px] font-medium uppercase text-zinc-400">{{ r.source_type }}</span>
                <span class="rounded bg-zinc-800 px-1.5 py-0.5 text-[10px] font-mono uppercase text-zinc-400">{{ r.fmt }}</span>
                <span class="rounded bg-zinc-800 px-1.5 py-0.5 text-[10px] text-zinc-400">
                  scope: {{ r.source_name || 'missing source' }}
                </span>
              </div>
              <div class="mt-0.5 flex flex-wrap items-center gap-2 text-[11px] text-zinc-500">
                <span class="font-mono text-[10px]">cron {{ r.cron }}</span>
                <span>·</span>
                <span>next {{ nextRun(r) }}</span>
                <span>·</span>
                <span>ran {{ r.fire_count }}x, last {{ fmtDate(r.last_run_at) }}</span>
                <template v-if="r.last_status">
                  <span>·</span>
                  <span :class="r.last_status === 'ok' ? 'text-emerald-400/80' : 'text-rose-300'">
                    {{ r.last_status === 'ok' ? 'exported' : (r.last_error || 'error') }}
                  </span>
                </template>
              </div>
            </div>

            <div class="flex shrink-0 items-center gap-1.5">
              <button
                class="flex items-center gap-1.5 rounded-xl border border-zinc-800 bg-zinc-900 px-3 py-1.5 text-xs font-medium text-zinc-300 transition hover:border-cyan-500/40 hover:text-cyan-300 disabled:opacity-50"
                :disabled="running === r.id"
                title="Export right now"
                @click="runNow(r)"
              >
                <Loader2 v-if="running === r.id" class="h-3.5 w-3.5 animate-spin" />
                <Play v-else class="h-3.5 w-3.5" />
                Run now
              </button>
              <button
                v-if="r.source_type === 'dashboard' && r.fmt === 'png' && r.last_artifact_id"
                class="flex items-center gap-1.5 rounded-xl border border-zinc-800 bg-zinc-900 px-3 py-1.5 text-xs font-medium text-zinc-300 transition hover:border-violet-500/40 hover:text-violet-300"
                title="Preview the latest image"
                @click="openPreview(r)"
              >
                <Loader2 v-if="previewLoading" class="h-3.5 w-3.5 animate-spin" />
                <Eye v-else class="h-3.5 w-3.5" />
                Preview
              </button>
              <button
                class="flex items-center gap-1.5 rounded-xl border border-zinc-800 bg-zinc-900 px-3 py-1.5 text-xs font-medium text-zinc-300 transition hover:border-emerald-500/40 hover:text-emerald-300 disabled:opacity-40"
                :disabled="!r.last_artifact_id"
                title="Download the latest artifact"
                @click="downloadLast(r)"
              >
                <Download class="h-3.5 w-3.5" />
                Latest
              </button>
              <button
                class="rounded-xl border border-zinc-800 bg-zinc-900 p-2 text-zinc-400 transition hover:border-zinc-600 hover:text-zinc-100 disabled:opacity-50"
                :disabled="toggling === r.id"
                :title="r.enabled ? 'Pause report' : 'Resume report'"
                @click="toggleReport(r)"
              >
                <Loader2 v-if="toggling === r.id" class="h-3.5 w-3.5 animate-spin" />
                <Power v-else class="h-3.5 w-3.5" :class="r.enabled ? 'text-emerald-400' : ''" />
              </button>
              <button
                class="rounded-xl border border-zinc-800 bg-zinc-900 p-2 text-zinc-400 transition hover:border-rose-500/40 hover:text-rose-300"
                title="Delete report"
                @click="removeReport(r)"
              >
                <Trash2 class="h-3.5 w-3.5" />
              </button>
            </div>
          </div>
        </div>
      </div>
    </main>

    <!-- create modal -->
    <Teleport to="body">
      <div
        v-if="showCreate"
        class="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4 backdrop-blur-sm"
        @click.self="showCreate = false"
      >
        <div class="w-full max-w-md rounded-2xl border border-zinc-800 bg-zinc-950 shadow-2xl">
          <div class="border-b border-zinc-800/80 px-5 py-4">
            <h2 class="text-sm font-bold">New scheduled report</h2>
            <p class="mt-0.5 text-[11px] text-zinc-500">Pick the source, the file format and a crontab (UTC).</p>
          </div>

          <div class="space-y-3.5 px-5 py-4">
            <label class="block">
              <span class="mb-1 block text-xs font-medium text-zinc-400">Name</span>
              <input
                v-model="formName"
                class="w-full rounded-xl border border-zinc-800 bg-zinc-900 px-3 py-2 text-sm outline-none transition placeholder:text-zinc-600 focus:border-cyan-500/60"
                placeholder="e.g. Weekly CRM export"
              />
            </label>

            <div class="grid grid-cols-2 gap-2">
              <label class="block">
                <span class="mb-1 block text-xs font-medium text-zinc-400">Source type</span>
                <select
                  v-model="formSourceType"
                  class="w-full rounded-xl border border-zinc-800 bg-zinc-900 px-3 py-2 text-sm outline-none focus:border-cyan-500/60"
                  @change="switchType"
                >
                  <option value="dataset">Dataset</option>
                  <option value="dashboard">Dashboard</option>
                </select>
              </label>
              <label class="block">
                <span class="mb-1 block text-xs font-medium text-zinc-400">Format</span>
                <select
                  v-model="formFmt"
                  class="w-full rounded-xl border border-zinc-800 bg-zinc-900 px-3 py-2 text-sm outline-none focus:border-cyan-500/60"
                >
                  <option v-for="f in FMTS_BY_TYPE[formSourceType]" :key="f" :value="f">{{ f }}</option>
                </select>
              </label>
            </div>

            <label class="block">
              <span class="mb-1 block text-xs font-medium text-zinc-400">Source</span>
              <select
                v-model="formSourceId"
                class="w-full rounded-xl border border-zinc-800 bg-zinc-900 px-3 py-2 text-sm outline-none focus:border-cyan-500/60"
              >
                <option value="" disabled>Select a {{ formSourceType }}…</option>
                <option v-for="s in sourceOptions()" :key="s.id" :value="s.id">{{ s.name }}</option>
              </select>
            </label>

            <label class="block">
              <span class="mb-1 block text-xs font-medium text-zinc-400">Crontab (UTC)</span>
              <input
                v-model="formCron"
                class="w-full rounded-xl border border-zinc-800 bg-zinc-900 px-3 py-2 font-mono text-xs outline-none transition placeholder:text-zinc-600 focus:border-cyan-500/60"
                placeholder="0 6 * * *"
              />
            </label>
            <div class="flex flex-wrap gap-1.5">
              <button
                v-for="h in CRON_HINTS"
                :key="h.value"
                class="rounded-lg border border-zinc-800 bg-zinc-900 px-2 py-1 text-[10px] text-zinc-400 transition hover:border-cyan-500/40 hover:text-cyan-300"
                @click="formCron = h.value"
              >
                {{ h.label }}
              </button>
            </div>

            <label class="flex items-center gap-2 text-xs text-zinc-400">
              <input v-model="formEnabled" type="checkbox" class="h-4 w-4 accent-cyan-500" />
              Enabled immediately
            </label>

            <p v-if="formError" class="rounded-xl border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-xs text-rose-300">{{ formError }}</p>
          </div>

          <div class="flex justify-end gap-2 border-t border-zinc-800/80 px-5 py-3.5">
            <button
              class="rounded-xl border border-zinc-800 px-3.5 py-2 text-sm font-medium text-zinc-400 transition hover:text-zinc-100"
              @click="showCreate = false"
            >
              Cancel
            </button>
            <button
              class="flex items-center gap-1.5 rounded-xl bg-cyan-500 px-3.5 py-2 text-sm font-semibold text-white shadow-lg shadow-cyan-500/20 transition hover:bg-cyan-400 disabled:opacity-50"
              :disabled="formSaving || !formName.trim() || !formSourceId"
              @click="saveForm"
            >
              <Loader2 v-if="formSaving" class="h-3.5 w-3.5 animate-spin" />
              Create report
            </button>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- v49: PNG preview modal -->
    <Teleport to="body">
      <div
        v-if="previewUrl"
        class="fixed inset-0 z-50 flex items-center justify-center bg-black/80 p-4 backdrop-blur-sm"
        @click.self="closePreview"
      >
        <div class="flex max-h-[92vh] w-full max-w-4xl flex-col overflow-hidden rounded-2xl border border-zinc-800 bg-zinc-950 shadow-2xl">
          <div class="flex items-center justify-between border-b border-zinc-800/80 px-5 py-3.5">
            <div>
              <h2 class="text-sm font-bold">{{ previewName }}</h2>
              <p class="text-[11px] text-zinc-500">Dashboard snapshot - exactly what the cron export rendered</p>
            </div>
            <button class="rounded-lg p-1 text-zinc-500 hover:text-zinc-200" @click="closePreview"><X class="h-4 w-4" /></button>
          </div>
          <div class="overflow-auto p-4">
            <img :src="previewUrl" :alt="previewName" class="mx-auto h-auto w-full rounded-xl border border-zinc-800" />
          </div>
          <div class="flex justify-end gap-2 border-t border-zinc-800/80 px-5 py-3">
            <button
              class="flex items-center gap-1.5 rounded-xl border border-zinc-800 px-3.5 py-2 text-xs font-medium text-zinc-300 transition hover:border-cyan-500/40 hover:text-cyan-300"
              @click="previewReport && downloadLast(previewReport)"
            >
              <Download class="h-3.5 w-3.5" /> Download PNG
            </button>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

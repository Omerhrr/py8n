<script setup lang="ts">
import {
  CheckCircle2, XCircle, Loader2, MinusCircle, RotateCcw, Trash2,
  RefreshCw, Activity, ChevronDown, ChevronUp, Clock, Webhook, Play, Copy, Check,
  PauseCircle, Send, Square, Ban,
} from 'lucide-vue-next'
import type { ExecutionDetail, ExecutionSummary, NodeRun, WorkflowListItem } from '~/types/node'

const { api } = useApi()
const store = usePy8nStore()

// ------------------------------------------------------------------ state
const workflows = ref<WorkflowListItem[]>([])
const statusFilter = ref<'' | 'success' | 'error' | 'running' | 'waiting' | 'cancelled'>('')
const workflowFilter = ref('')
const loading = ref(false)
const expandedId = ref<string | null>(null)
const copiedKey = ref<string | null>(null)
const resumePayload = ref('')
const resuming = ref(false)
const resumeError = ref<string | null>(null)

const executions = computed(() => store.allExecutions)

// ------------------------------------------------------------------ stats
const stats = computed(() => {
  const rows = executions.value
  const total = rows.length
  const ok = rows.filter((r) => r.status === 'success').length
  const failed = rows.filter((r) => r.status === 'error').length
  const running = rows.filter((r) => r.status === 'running').length
  const durations = rows.map((r) => r.duration_ms).filter((d): d is number => d != null)
  const avg = durations.length ? Math.round(durations.reduce((a, b) => a + b, 0) / durations.length) : null
  return { total, ok, failed, running, rate: total ? Math.round((ok / total) * 100) : 100, avg }
})

// status counts for the filter chips (computed over the unfiltered recent set)
const chipCounts = ref<{ all: number; success: number; error: number; running: number; waiting: number; cancelled: number }>({
  all: 0, success: 0, error: 0, running: 0, waiting: 0, cancelled: 0,
})
function recount(rows: ExecutionSummary[]) {
  chipCounts.value = {
    all: rows.length,
    success: rows.filter((r) => r.status === 'success').length,
    error: rows.filter((r) => r.status === 'error').length,
    running: rows.filter((r) => r.status === 'running').length,
    waiting: rows.filter((r) => r.status === 'waiting').length,
    cancelled: rows.filter((r) => r.status === 'cancelled').length,
  }
}

// ------------------------------------------------------------------ data
async function refresh({ silent = false } = {}) {
  if (!silent) loading.value = true
  try {
    // fetch unfiltered counts from a plain call, then the filtered list
    const base = await api.get<ExecutionSummary[]>('/executions?limit=50')
    recount(base)
    const params = new URLSearchParams()
    if (statusFilter.value) params.set('status', statusFilter.value)
    if (workflowFilter.value) params.set('workflow_id', workflowFilter.value)
    params.set('limit', '50')
    store.allExecutions = await api.get<ExecutionSummary[]>(`/executions?${params}`)
    // live-refresh the open detail while its execution is still running
    if (expandedId.value && store.selectedExecution?.status === 'running') {
      await store.loadExecutionDetail(expandedId.value)
    }
  } catch {
    /* backend hiccup - keep last data */
  } finally {
    if (!silent) loading.value = false
  }
}

let pollTimer: ReturnType<typeof setInterval> | null = null
onMounted(async () => {
  try {
    workflows.value = await api.get<WorkflowListItem[]>('/workflows?limit=100')
  } catch { /* non-fatal */ }
  await refresh()
  pollTimer = setInterval(() => refresh({ silent: true }), 5000)
})
onUnmounted(() => { if (pollTimer) clearInterval(pollTimer) })

function setStatus(s: '' | 'success' | 'error' | 'running' | 'waiting' | 'cancelled') {
  statusFilter.value = s
  refresh({ silent: true })
}
function setWorkflow(id: string) {
  workflowFilter.value = id
  refresh({ silent: true })
}

// ------------------------------------------------------------------ actions
async function rerun(exec: ExecutionSummary) {
  try {
    const res = await store.rerunExecution(exec.id)
    expandedId.value = res.execution_id
    await store.loadExecutionDetail(res.execution_id)
    await refresh({ silent: true })
  } catch (e: any) {
    alert(e?.data?.detail || e?.message || 'Re-run failed')
  }
}

async function remove(exec: ExecutionSummary) {
  if (!confirm('Delete this execution record?')) return
  await store.deleteExecution(exec.id)
  if (expandedId.value === exec.id) expandedId.value = null
  recount(store.allExecutions)
}

// v8: cancel a running execution (cooperative stop between nodes)
async function cancel(exec: ExecutionSummary) {
  try {
    await store.cancelExecution(exec.id)
    // the runner flips the row itself; poll quickly so the UI follows suit
    setTimeout(() => refresh({ silent: true }), 400)
  } catch (e: any) {
    console.warn('cancel failed', e)
  }
}

async function toggleDetail(exec: ExecutionSummary) {
  if (expandedId.value === exec.id) {
    expandedId.value = null
    store.selectedExecution = null
    return
  }
  expandedId.value = exec.id
  await store.loadExecutionDetail(exec.id)
}

// ------------------------------------------------------------------ resume
const waitHint = computed(() => {
  const d = store.selectedExecution
  if (!d?.resume?.node_id) return null
  const run = (d.node_runs || []).find(
    (r) => r.node_id === d.resume!.node_id && r.status === 'waiting' && r.output?.resume_hint,
  )
  return run?.output?.resume_hint ?? null
})

async function doResume() {
  const d = store.selectedExecution
  if (!d?.resume) return
  let payload: any = null
  const text = resumePayload.value.trim()
  if (text) {
    try {
      payload = JSON.parse(text)
    } catch {
      resumeError.value = 'Payload is not valid JSON'
      return
    }
  }
  resuming.value = true
  resumeError.value = null
  try {
    await store.resumeExecution(d.id, d.resume.token, payload)
    resumePayload.value = ''
    await store.loadExecutionDetail(d.id)
    await refresh({ silent: true })
  } catch (e: any) {
    resumeError.value = e?.data?.detail || e?.message || 'Resume failed'
  } finally {
    resuming.value = false
  }
}

// ------------------------------------------------------------------ helpers
const statusIcon = (status: string) => {
  switch (status) {
    case 'success': return CheckCircle2
    case 'error': return XCircle
    case 'skipped': return MinusCircle
    case 'waiting': return PauseCircle
    case 'cancelled': return Ban
    default: return Loader2
  }
}
const statusClass = (status: string) => {
  switch (status) {
    case 'success': return 'text-emerald-400'
    case 'error': return 'text-rose-400'
    case 'skipped': return 'text-zinc-600'
    case 'waiting': return 'text-violet-400 animate-pulse'
    case 'cancelled': return 'text-zinc-400'
    default: return 'text-amber-400 animate-pulse'
  }
}
const statusDot = (status: string) => {
  switch (status) {
    case 'success': return 'bg-emerald-400'
    case 'error': return 'bg-rose-400'
    case 'waiting': return 'bg-violet-400 animate-pulse'
    case 'cancelled': return 'bg-zinc-400'
    default: return 'bg-amber-400 animate-pulse'
  }
}
const triggerIcon = (t: string) => (t === 'webhook' ? Webhook : t === 'schedule' ? Clock : Play)

function fmtDuration(ms: number | null) {
  if (ms == null) return '-'
  return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(ms < 10000 ? 2 : 1)}s`
}
function fmtRelative(iso: string | null) {
  if (!iso) return '-'
  const diff = Date.now() - new Date(iso).getTime()
  if (diff < 10_000) return 'just now'
  if (diff < 60_000) return `${Math.floor(diff / 1000)}s ago`
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m ago`
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)}h ago`
  return new Date(iso).toLocaleDateString()
}
function fmtFull(iso: string | null) {
  return iso ? new Date(iso).toLocaleString() : '-'
}

const prettyJson = (v: any) => {
  try { return JSON.stringify(v, null, 2) } catch { return String(v) }
}
async function copyJson(key: string, v: any) {
  try {
    await navigator.clipboard.writeText(prettyJson(v))
    copiedKey.value = key
    setTimeout(() => { if (copiedKey.value === key) copiedKey.value = null }, 1200)
  } catch { /* clipboard unavailable */ }
}

const nodeRunsOf = (detail: ExecutionDetail | null): NodeRun[] => detail?.node_runs || []
</script>

<template>
  <div class="text-zinc-100">
    <!-- page header (app nav lives in the sidebar) -->
    <header class="border-b border-zinc-800/80 bg-zinc-950/90 backdrop-blur sticky top-0 z-20">
      <div class="mx-auto flex max-w-7xl items-center justify-between px-4 py-3.5 sm:px-6">
        <div class="flex items-center gap-3">
          <div class="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-orange-500 to-rose-500 shadow-lg shadow-orange-500/20">
            <Activity class="h-4 w-4 text-white" />
          </div>
          <div>
            <h1 class="text-lg font-bold tracking-tight">Executions</h1>
            <p class="-mt-0.5 text-[11px] text-zinc-500">History across all workflows · auto-refresh 5s</p>
          </div>
        </div>
        <div class="flex items-center gap-2">
          <span class="hidden items-center gap-1.5 rounded-full border border-emerald-500/30 bg-emerald-500/10 px-2.5 py-1 text-[10px] font-semibold uppercase tracking-wider text-emerald-400 sm:flex">
            <span class="h-1.5 w-1.5 animate-pulse rounded-full bg-emerald-400" /> Live
          </span>
          <button
            class="inline-flex items-center gap-2 rounded-xl border border-zinc-700 px-3 py-2 text-sm text-zinc-300 transition hover:border-zinc-500 hover:text-white"
            :disabled="loading"
            title="Refresh now"
            @click="refresh()"
          >
            <RefreshCw class="h-4 w-4" :class="loading ? 'animate-spin' : ''" /> Refresh
          </button>
        </div>
      </div>
    </header>

    <main class="mx-auto max-w-7xl px-4 py-6 sm:px-6">
      <!-- stats strip -->
      <section class="mb-5 grid grid-cols-2 gap-3 sm:grid-cols-5">
        <div class="rounded-2xl border border-zinc-800 bg-zinc-900/40 p-4">
          <p class="text-[10px] font-bold uppercase tracking-widest text-zinc-500">Total</p>
          <p class="mt-1 text-2xl font-bold tabular-nums">{{ stats.total }}</p>
        </div>
        <div class="rounded-2xl border border-zinc-800 bg-zinc-900/40 p-4">
          <p class="text-[10px] font-bold uppercase tracking-widest text-zinc-500">Succeeded</p>
          <p class="mt-1 text-2xl font-bold tabular-nums text-emerald-400">{{ stats.ok }}</p>
        </div>
        <div class="rounded-2xl border border-zinc-800 bg-zinc-900/40 p-4">
          <p class="text-[10px] font-bold uppercase tracking-widest text-zinc-500">Failed</p>
          <p class="mt-1 text-2xl font-bold tabular-nums text-rose-400">{{ stats.failed }}</p>
        </div>
        <div class="rounded-2xl border border-zinc-800 bg-zinc-900/40 p-4">
          <p class="text-[10px] font-bold uppercase tracking-widest text-zinc-500">Running</p>
          <p class="mt-1 text-2xl font-bold tabular-nums text-amber-400">{{ stats.running }}</p>
        </div>
        <div class="col-span-2 rounded-2xl border border-zinc-800 bg-zinc-900/40 p-4 sm:col-span-1">
          <p class="text-[10px] font-bold uppercase tracking-widest text-zinc-500">Success rate</p>
          <p class="mt-1 text-2xl font-bold tabular-nums">{{ stats.rate }}<span class="text-sm text-zinc-500">%</span></p>
        </div>
      </section>

      <!-- filters -->
      <section class="mb-4 flex flex-wrap items-center gap-2">
        <button
          v-for="opt in [
            { v: '', label: 'All', n: chipCounts.all },
            { v: 'success', label: 'Success', n: chipCounts.success },
            { v: 'error', label: 'Failed', n: chipCounts.error },
            { v: 'running', label: 'Running', n: chipCounts.running },
            { v: 'waiting', label: 'Waiting', n: chipCounts.waiting },
            { v: 'cancelled', label: 'Cancelled', n: chipCounts.cancelled },
          ]"
          :key="opt.v"
          class="inline-flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-xs font-semibold transition"
          :class="statusFilter === opt.v
            ? 'border-orange-500/60 bg-orange-500/15 text-orange-300'
            : 'border-zinc-800 bg-zinc-900/40 text-zinc-400 hover:border-zinc-600 hover:text-zinc-200'"
          @click="setStatus(opt.v as any)"
        >
          <span v-if="opt.v" class="h-1.5 w-1.5 rounded-full" :class="statusDot(opt.v)" />
          {{ opt.label }}
          <span class="rounded-full bg-zinc-800 px-1.5 text-[10px] tabular-nums text-zinc-400">{{ opt.n }}</span>
        </button>

        <div class="ml-auto">
          <select
            :value="workflowFilter"
            class="rounded-xl border border-zinc-800 bg-zinc-900 px-3 py-1.5 text-xs text-zinc-300 outline-none transition focus:border-orange-500/60"
            @change="setWorkflow(($event.target as HTMLSelectElement).value)"
          >
            <option value="">All workflows</option>
            <option v-for="wf in workflows" :key="wf.id" :value="wf.id">{{ wf.name }}</option>
          </select>
        </div>
      </section>

      <!-- execution rows -->
      <section class="space-y-2">
        <div
          v-for="exec in executions"
          :key="exec.id"
          class="overflow-hidden rounded-2xl border transition"
          :class="[
            exec.status === 'error' ? 'border-rose-500/25'
              : exec.status === 'waiting' ? 'border-violet-500/30'
              : exec.status === 'cancelled' ? 'border-zinc-700/70'
              : 'border-zinc-800',
            expandedId === exec.id ? 'bg-zinc-900/60' : 'bg-zinc-900/40 hover:bg-zinc-900/70',
          ]"
        >
          <!-- row -->
          <div
            class="grid cursor-pointer grid-cols-[auto_1fr_auto] items-center gap-3 px-4 py-3 sm:grid-cols-[auto_minmax(0,2fr)_auto_auto_auto_auto]"
            @click="toggleDetail(exec)"
          >
            <component :is="statusIcon(exec.status)" class="h-5 w-5 shrink-0" :class="statusClass(exec.status)" />

            <div class="min-w-0">
              <p class="truncate text-sm font-semibold text-zinc-100">
                {{ exec.workflow_name || 'Deleted workflow' }}
              </p>
              <p class="mt-0.5 flex items-center gap-1.5 text-[10px] text-zinc-500" :title="fmtFull(exec.started_at)">
                <component :is="triggerIcon(exec.trigger_type)" class="h-3 w-3" />
                {{ fmtRelative(exec.started_at) }}
                <span class="rounded bg-zinc-800 px-1 py-0.5 font-mono text-[9px] uppercase">{{ exec.trigger_type }}</span>
                <span v-if="expandedId === exec.id" class="font-mono text-zinc-600">#{{ exec.id.slice(0, 10) }}</span>
              </p>
            </div>

            <span
              v-if="exec.error"
              class="hidden max-w-[220px] truncate font-mono text-[10px] text-rose-400/90 lg:block"
              :title="exec.error"
            >{{ exec.error }}</span>

            <span class="hidden text-right text-xs tabular-nums text-zinc-400 sm:block">{{ fmtDuration(exec.duration_ms) }}</span>

            <div class="flex items-center gap-1">
              <!-- v8: cancel a running execution -->
              <button
                v-if="exec.status === 'running'"
                class="rounded-lg border border-rose-500/40 bg-rose-500/10 p-1.5 text-rose-400 transition hover:bg-rose-500/20 hover:text-rose-300"
                title="Cancel this execution"
                @click.stop="cancel(exec)"
              >
                <Loader2 v-if="store.cancelling === exec.id" class="h-3.5 w-3.5 animate-spin" />
                <Square v-else class="h-3.5 w-3.5 fill-current" />
              </button>
              <button
                class="rounded-lg border border-zinc-700/80 p-1.5 text-zinc-400 transition hover:border-orange-500/60 hover:text-orange-300"
                title="Re-run with recorded payload"
                @click.stop="rerun(exec)"
              >
                <Loader2 v-if="store.rerunning === exec.id" class="h-3.5 w-3.5 animate-spin" />
                <RotateCcw v-else class="h-3.5 w-3.5" />
              </button>
              <button
                class="rounded-lg border border-zinc-700/80 p-1.5 text-zinc-400 transition hover:border-rose-500/60 hover:text-rose-300"
                title="Delete record"
                @click.stop="remove(exec)"
              >
                <Trash2 class="h-3.5 w-3.5" />
              </button>
            </div>

            <component :is="expandedId === exec.id ? ChevronUp : ChevronDown" class="h-4 w-4 text-zinc-500" />
          </div>

          <!-- detail -->
          <div v-if="expandedId === exec.id" class="border-t border-zinc-800/70 bg-zinc-950/60 p-4">
            <div v-if="store.loadingDetail && !store.selectedExecution" class="flex items-center gap-2 text-xs text-zinc-500">
              <Loader2 class="h-3.5 w-3.5 animate-spin" /> Loading run…
            </div>
            <template v-else-if="store.selectedExecution">
              <!-- resume panel (waiting executions) -->
              <div
                v-if="store.selectedExecution.status === 'waiting' && store.selectedExecution.resume"
                class="mb-3 rounded-xl border border-violet-500/40 bg-violet-500/5 p-3"
              >
                <div class="flex flex-wrap items-center gap-2">
                  <PauseCircle class="h-4 w-4 animate-pulse text-violet-400" />
                  <p class="text-xs font-bold uppercase tracking-widest text-violet-300">Waiting for resume</p>
                  <p v-if="waitHint" class="text-[11px] text-zinc-400">{{ waitHint }}</p>
                </div>
                <div class="mt-2 flex items-center gap-2 rounded-lg border border-zinc-800 bg-zinc-950/80 px-2.5 py-1.5">
                  <code class="min-w-0 flex-1 truncate font-mono text-[10px] text-zinc-400" :title="store.selectedExecution.resume.url">
                    POST {{ store.selectedExecution.resume.url }}
                  </code>
                  <button class="rounded p-1 text-zinc-500 hover:text-zinc-200" title="Copy URL" @click="copyJson('resume-url', store.selectedExecution.resume.url)">
                    <Check v-if="copiedKey === 'resume-url'" class="h-3 w-3 text-emerald-400" />
                    <Copy v-else class="h-3 w-3" />
                  </button>
                </div>
                <div class="mt-2 flex flex-col gap-2 sm:flex-row">
                  <textarea
                    v-model="resumePayload"
                    rows="2"
                    placeholder='Resume payload JSON, e.g. {"approved": true}'
                    class="min-h-0 flex-1 resize-y rounded-lg border border-zinc-800 bg-zinc-950/80 px-2.5 py-1.5 font-mono text-[11px] text-zinc-200 outline-none transition focus:border-violet-500/60"
                  />
                  <button
                    class="inline-flex items-center justify-center gap-2 self-stretch rounded-lg bg-violet-500 px-4 py-2 text-xs font-semibold text-white shadow-lg shadow-violet-500/25 transition hover:bg-violet-400 disabled:opacity-50 sm:self-auto"
                    :disabled="resuming"
                    @click="doResume"
                  >
                    <Loader2 v-if="resuming" class="h-3.5 w-3.5 animate-spin" />
                    <Send v-else class="h-3.5 w-3.5" /> Resume
                  </button>
                </div>
                <p v-if="resumeError" class="mt-1.5 font-mono text-[10px] text-rose-400">{{ resumeError }}</p>
              </div>

              <!-- meta -->
              <div class="mb-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-zinc-500">
                <span>Started <span class="text-zinc-300">{{ fmtFull(store.selectedExecution.started_at) }}</span></span>
                <span>Finished <span class="text-zinc-300">{{ fmtFull(store.selectedExecution.finished_at) }}</span></span>
                <span>Duration <span class="tabular-nums text-zinc-300">{{ fmtDuration(store.selectedExecution.duration_ms) }}</span></span>
                <span class="font-mono">#{{ store.selectedExecution.id }}</span>
              </div>

              <!-- trigger payload -->
              <details v-if="store.selectedExecution.trigger_payload && Object.keys(store.selectedExecution.trigger_payload).length" open class="mb-3 rounded-xl border border-zinc-800">
                <summary class="flex cursor-pointer items-center gap-2 px-3 py-2 text-[10px] font-bold uppercase tracking-widest text-zinc-500 hover:text-zinc-300">
                  Trigger payload
                  <button class="ml-auto rounded p-1 text-zinc-500 hover:text-zinc-200" title="Copy JSON" @click.prevent="copyJson(`tp-${exec.id}`, store.selectedExecution.trigger_payload)">
                    <Check v-if="copiedKey === `tp-${exec.id}`" class="h-3 w-3 text-emerald-400" />
                    <Copy v-else class="h-3 w-3" />
                  </button>
                </summary>
                <pre class="max-h-44 overflow-auto border-t border-zinc-800/70 px-3 py-2 font-mono text-[10px] leading-relaxed text-zinc-400">{{ prettyJson(store.selectedExecution.trigger_payload) }}</pre>
              </details>

              <!-- node runs -->
              <p class="mb-2 text-[10px] font-bold uppercase tracking-widest text-zinc-500">Node runs ({{ nodeRunsOf(store.selectedExecution).length }})</p>
              <div class="space-y-2">
                <div
                  v-for="(run, i) in nodeRunsOf(store.selectedExecution)"
                  :key="`${run.node_id}-${run.batch_index ?? 'x'}-${i}`"
                  class="rounded-xl border bg-zinc-900/40"
                  :class="run.status === 'error' ? 'border-rose-500/30' : run.status === 'success' ? 'border-zinc-800' : 'border-zinc-800/60'"
                >
                  <div class="flex items-center gap-2 px-3 py-2">
                    <component :is="statusIcon(run.status)" class="h-4 w-4 shrink-0" :class="statusClass(run.status)" />
                    <span class="text-xs font-semibold text-zinc-200">{{ run.node_name }}</span>
                    <span class="rounded bg-zinc-800 px-1.5 py-0.5 font-mono text-[9px] text-zinc-500">{{ run.node_type }}</span>
                    <span
                      v-if="run.batch_index != null"
                      class="rounded bg-sky-500/15 px-1.5 py-0.5 font-mono text-[9px] text-sky-400"
                      :title="`Loop batch ${run.batch_index + 1}`"
                    >batch {{ run.batch_index + 1 }}</span>
                    <span v-if="run.duration_ms != null" class="ml-auto text-[10px] tabular-nums text-zinc-600">{{ run.duration_ms }}ms</span>
                  </div>
                  <p v-if="run.error" class="border-t border-zinc-800/60 px-3 py-1.5 font-mono text-[10px] leading-relaxed text-rose-400">{{ run.error }}</p>
                  <details v-if="run.input !== null && run.input !== undefined" class="border-t border-zinc-800/60">
                    <summary class="flex cursor-pointer items-center gap-2 px-3 py-1 text-[10px] font-bold uppercase tracking-wider text-zinc-600 hover:text-zinc-400">
                      input
                      <button class="rounded p-0.5 hover:text-zinc-200" title="Copy JSON" @click.prevent="copyJson(`${exec.id}-in-${i}`, run.input)">
                        <Check v-if="copiedKey === `${exec.id}-in-${i}`" class="h-3 w-3 text-emerald-400" />
                        <Copy v-else class="h-3 w-3" />
                      </button>
                    </summary>
                    <pre class="max-h-44 overflow-auto px-3 pb-2 font-mono text-[10px] leading-relaxed text-zinc-500">{{ prettyJson(run.input) }}</pre>
                  </details>
                  <details v-if="run.output !== null && run.output !== undefined" class="border-t border-zinc-800/60">
                    <summary class="flex cursor-pointer items-center gap-2 px-3 py-1 text-[10px] font-bold uppercase tracking-wider text-zinc-600 hover:text-zinc-400">
                      output
                      <button class="rounded p-0.5 hover:text-zinc-200" title="Copy JSON" @click.prevent="copyJson(`${exec.id}-${i}`, run.output)">
                        <Check v-if="copiedKey === `${exec.id}-${i}`" class="h-3 w-3 text-emerald-400" />
                        <Copy v-else class="h-3 w-3" />
                      </button>
                    </summary>
                    <pre class="max-h-44 overflow-auto px-3 pb-2 font-mono text-[10px] leading-relaxed text-zinc-400">{{ prettyJson(run.output) }}</pre>
                  </details>
                </div>
              </div>
              <p v-if="!nodeRunsOf(store.selectedExecution).length" class="rounded-xl border border-zinc-800 px-3 py-4 text-center text-[11px] text-zinc-600">
                No node runs recorded yet - still running?
              </p>
            </template>
          </div>
        </div>

        <!-- empty state -->
        <div v-if="!loading && !executions.length" class="rounded-2xl border border-dashed border-zinc-800 px-6 py-16 text-center">
          <Activity class="mx-auto mb-3 h-8 w-8 text-zinc-700" />
          <p class="text-sm font-semibold text-zinc-400">No executions found</p>
          <p class="mt-1 text-xs text-zinc-600">Run a workflow from the dashboard, or trigger one via webhook / schedule.</p>
          <NuxtLink
            to="/"
            class="mt-4 inline-flex items-center gap-2 rounded-xl bg-orange-500 px-4 py-2 text-sm font-semibold text-white shadow-lg shadow-orange-500/25 transition hover:bg-orange-400"
          >
            <Play class="h-4 w-4" /> Go to workflows
          </NuxtLink>
        </div>
      </section>
    </main>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import {
  Activity, Loader2, RefreshCw, AlertTriangle, Database,
  Inbox, Timer, ArrowRight, CircleDot, Bot, X,
  FileText, Workflow as WorkflowIcon, ChevronsDown, CheckCircle2, XCircle, Sparkles,
} from 'lucide-vue-next'
import { useApi } from '~/composables/useApi'

// v53: data observability - one derived surface for "is the estate okay?".
// v57: the OPERATIONS CENTER upgrade - the whole-environment rollup
// (workflows / datasets / reports / agents + SYSTEM verdict) plus the
// incident drilldown chain: incident -> workflow -> execution -> node ->
// input -> error -> related dataset -> impact. Everything derived, never stored.

interface ObsEvent {
  id: string
  type: string
  ts: string | null
  severity: 'info' | 'warn' | 'error'
  title: string
  detail: string | null
  ref: string
  meta: Record<string, any>
}

interface OpsOverview {
  verdict: string
  generated_at: string | null
  workflows: {
    total: number; active: number; running_now: number
    runs_24h: number; failed_24h: number; failures_7d: number; failure_rate_7d: number
    failing_workflows: { workflow_id: string; name: string; failures: number; last_error: string | null; last_failed_at: string | null; ref: string }[]
  }
  datasets: {
    total: number; scored: number; unscored: number
    healthy: number; degraded: number; unhealthy: number
    violating_contracts: number; contracts_total: number
    stale_or_cold: number; rows_total: number
    worst: { dataset_id: string; name: string; score: number; status: string; ref: string } | null
  }
  reports: { scheduled: number; dashboards: number; ok_7d: number; error_7d: number; skipped_7d: number; last_error: string | null }
  agents: {
    agent_workflows: number; runs_7d: number; errors_7d: number; last_error: string | null
    workflows: { id: string; name: string; ref: string }[]
  }
  incidents: ObsEvent[]
}

interface LegacyOverview {
  ingestion: {
    checkpoints: number; rows_total: number; active_24h: number
    pipelines: { dataset: string; dataset_id: string; ref: string; key: string; watermark: string | null; runs: number; rows_total: number; last_run_at: string | null; stats: Record<string, any> | null }[]
  }
}

interface IncidentChain {
  execution: { id: string; status: string; trigger_type: string; started_at: string | null; finished_at: string | null; duration_ms: number | null; error: string | null; ref: string }
  workflow: { id: string; name: string; ref: string; active: boolean; tags: string[] }
  failed_node: Record<string, any> | null
  all_failed_nodes: { node_id: string; name: string; type: string; error: string | null }[]
  comparison_with_previous_success: Record<string, any> | null
  related_datasets: { id: string; name: string; rows: number; ref: string; health: { score: number; status: string } | null }[]
  impact: Record<string, any>[]
  severity: string
  chain: { step: string; label: string; ref: string | null }[]
}

const { api } = useApi()
const loading = ref(true)
const ops = ref<OpsOverview | null>(null)
const legacy = ref<LegacyOverview | null>(null)
const pageError = ref('')
const refreshing = ref(false)
const auto = ref(false)
let timer: ReturnType<typeof setInterval> | null = null

// event stream state
const events = ref<ObsEvent[]>([])
const eventsTotal = ref(0)
const eventsLoading = ref(false)
const typeFilter = ref('')          // '', 'dataset.', 'workflow.', 'report.', 'share.'
const severityFilter = ref('')      // '', 'info', 'warn', 'error'
const EVENTS_PAGE = 60

// incident drilldown state
const drillOpen = ref(false)
const drillLoading = ref(false)
const drill = ref<IncidentChain | null>(null)
const drillError = ref('')

// v58: AI investigation state
const aiLoading = ref(false)
const aiFindings = ref<any>(null)
const aiError = ref('')
const aiNarrate = ref(false)
const applying = ref(false)
const appliedNote = ref('')

const typeChips = [
  { label: 'All', value: '' },
  { label: 'Dataset writes', value: 'dataset.' },
  { label: 'Workflow runs', value: 'workflow.' },
  { label: 'Report deliveries', value: 'report.' },
  { label: 'Share denials', value: 'share.' },
]

const severityMeta: Record<string, { dot: string; chip: string }> = {
  info: { dot: 'bg-sky-400', chip: 'bg-sky-500/10 text-sky-300' },
  warn: { dot: 'bg-amber-400', chip: 'bg-amber-500/10 text-amber-300' },
  error: { dot: 'bg-rose-500', chip: 'bg-rose-500/10 text-rose-300' },
}

const verdictMeta: Record<string, { ring: string; text: string; label: string }> = {
  healthy: { ring: 'border-emerald-500/40 bg-emerald-500/10', text: 'text-emerald-300', label: 'SYSTEM HEALTHY' },
  degraded: { ring: 'border-amber-500/40 bg-amber-500/10', text: 'text-amber-300', label: 'SYSTEM DEGRADED' },
  unhealthy: { ring: 'border-rose-500/40 bg-rose-500/10', text: 'text-rose-300', label: 'SYSTEM UNHEALTHY' },
}

const severityBadge: Record<string, string> = {
  critical: 'bg-rose-500/20 text-rose-200',
  high: 'bg-rose-500/15 text-rose-300',
  medium: 'bg-amber-500/15 text-amber-300',
  low: 'bg-zinc-700/40 text-zinc-300',
  info: 'bg-zinc-800 text-zinc-400',
}

function fmtWhen(ts: string | null): string {
  if (!ts) return ''
  const d = new Date(ts)
  const s = Math.max(0, (Date.now() - d.getTime()) / 1000)
  if (s < 60) return 'just now'
  if (s < 3600) return `${Math.round(s / 60)}m ago`
  if (s < 86400) return `${Math.round(s / 3600)}h ago`
  return `${Math.round(s / 86400)}d ago`
}

function fmtNum(n: number): string {
  return n >= 1_000_000 ? `${(n / 1_000_000).toFixed(1)}M` : n >= 10_000 ? `${(n / 1000).toFixed(1)}k` : n.toLocaleString()
}

function fmtDur(ms: number | null): string {
  if (ms == null) return '-'
  if (ms < 1000) return `${Math.round(ms)}ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`
  return `${Math.floor(ms / 60_000)}m ${Math.round((ms % 60_000) / 1000)}s`
}

async function loadOps() {
  try {
    ops.value = await api.get<OpsOverview>('/ops/overview')
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'Could not load the ops overview'
  }
}

async function loadLegacy() {
  // v53 surface kept for the ingestion checkpoints panel
  try {
    legacy.value = await api.get<LegacyOverview>('/observability/overview')
  } catch {
    /* the ops overview remains the primary - ingestion panel just stays hidden */
  }
}

async function loadEvents(reset = true) {
  if (reset) events.value = []
  eventsLoading.value = true
  try {
    const params = new URLSearchParams()
    if (typeFilter.value) params.set('type', typeFilter.value)
    if (severityFilter.value) params.set('severity', severityFilter.value)
    params.set('limit', String(EVENTS_PAGE))
    params.set('offset', String(reset ? 0 : events.value.length))
    const res = await api.get<{ events: ObsEvent[]; total: number }>(`/observability/events?${params}`)
    events.value = reset ? res.events : [...events.value, ...res.events]
    eventsTotal.value = res.total
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'Could not load events'
  } finally {
    eventsLoading.value = false
  }
}

async function refreshAll() {
  refreshing.value = true
  pageError.value = ''
  await Promise.all([loadOps(), loadLegacy(), loadEvents(true)])
  refreshing.value = false
}

function setFilter() {
  loadEvents(true)
}

function toggleAuto() {
  auto.value = !auto.value
  if (auto.value) {
    timer = setInterval(refreshAll, 30_000)
  } else if (timer) {
    clearInterval(timer)
    timer = null
  }
}

// ---------------------------------------------------------------- drilldown

function openIncident(e: ObsEvent) {
  const execId = e.meta?.execution_id
  if (!execId) {
    window.location.href = e.ref
    return
  }
  drillOpen.value = true
  drillLoading.value = true
  drill.value = null
  drillError.value = ''
  aiFindings.value = null
  aiError.value = ''
  appliedNote.value = ''
  api.get<IncidentChain>(`/ops/incidents/${execId}`)
    .then((res) => { drill.value = res })
    .catch((err: any) => { drillError.value = err?.data?.detail || err?.message || 'Drilldown failed' })
    .finally(() => { drillLoading.value = false })
}

async function runInvestigation() {
  if (!drill.value) return
  aiLoading.value = true
  aiError.value = ''
  aiFindings.value = null
  appliedNote.value = ''
  try {
    aiFindings.value = await api.post('/ops/ai/investigate', {
      execution_id: drill.value.execution.id,
      narrate: aiNarrate.value,
    })
  } catch (e: any) {
    aiError.value = e?.data?.detail || e?.message || 'Investigation failed'
  } finally {
    aiLoading.value = false
  }
}

async function applyProposal() {
  const p = aiFindings.value?.proposed_action
  if (!p) return
  applying.value = true
  try {
    const res = await api.post('/ops/ai/apply-proposal', {
      workflow_id: p.workflow_id,
      patch: p.patch,
    })
    appliedNote.value = `Applied as workflow v${res.version} - policy: ${Object.entries(res.policy || {}).map(([k, v]) => `${k}=${v}`).join(', ')}`
  } catch (e: any) {
    aiError.value = e?.data?.detail || e?.message || 'Apply failed'
  } finally {
    applying.value = false
  }
}

function stepIcon(step: string) {
  if (step === 'error') return XCircle
  if (step === 'impact') return Sparkles
  return CheckCircle2
}

const impactTotals = computed(() => {
  if (!drill.value?.impact?.length) return null
  const t = drill.value.impact.reduce((acc: Record<string, number>, i) => {
    for (const [k, v] of Object.entries(i.totals || {})) acc[k] = (acc[k] || 0) + Number(v || 0)
    return acc
  }, {})
  const ranks = drill.value.impact.map((i: any) => i.highest_risk).filter(Boolean)
  return { totals: t, highest: ranks[0] || null, severity: drill.value.impact[0]?.severity }
})

onMounted(refreshAll)
onUnmounted(() => { if (timer) clearInterval(timer) })
</script>

<template>
  <div class="text-zinc-100">
    <header class="sticky top-0 z-20 border-b border-zinc-800/80 bg-zinc-950/90 backdrop-blur">
      <div class="mx-auto max-w-7xl px-4 py-3.5 sm:px-6">
        <div class="flex flex-wrap items-center gap-3">
          <div class="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-sky-500 to-indigo-500 shadow-lg shadow-sky-500/20">
            <Activity class="h-4 w-4 text-white" />
          </div>
          <div class="min-w-0 flex-1">
            <h1 class="text-lg font-bold tracking-tight">Operations Center</h1>
            <p class="-mt-0.5 text-[11px] text-zinc-500">Is the entire automation environment healthy? Click any incident to drill down.</p>
          </div>
          <button
            class="flex items-center gap-1.5 rounded-xl border px-3 py-2 text-xs font-medium transition"
            :class="auto ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-300' : 'border-zinc-800 bg-zinc-900 text-zinc-300 hover:text-zinc-100'"
            title="Refresh every 30s"
            @click="toggleAuto"
          >
            <Timer class="h-3.5 w-3.5" /> auto {{ auto ? 'on' : 'off' }}
          </button>
          <button
            class="flex items-center gap-1.5 rounded-xl border border-zinc-800 bg-zinc-900 px-3 py-2 text-xs font-medium text-zinc-300 transition hover:border-sky-500/40 hover:text-sky-300 disabled:opacity-50"
            :disabled="refreshing"
            @click="refreshAll"
          >
            <Loader2 v-if="refreshing" class="h-3.5 w-3.5 animate-spin" />
            <RefreshCw v-else class="h-3.5 w-3.5" />
          </button>
        </div>
      </div>
    </header>

    <main class="mx-auto max-w-7xl px-4 py-6 sm:px-6">
      <div v-if="pageError" class="mb-4 flex items-start gap-2 rounded-xl border border-rose-500/40 bg-rose-500/10 px-4 py-3 text-sm text-rose-300">
        {{ pageError }}
      </div>

      <div v-if="loading" class="grid place-items-center py-16 text-zinc-600">
        <Loader2 class="h-6 w-6 animate-spin" />
      </div>

      <template v-else-if="ops">
        <!-- SYSTEM banner -->
        <div class="mb-5 flex flex-wrap items-center gap-3 rounded-2xl border p-4" :class="verdictMeta[ops.verdict]?.ring || 'border-zinc-800 bg-zinc-900/40'">
          <CircleDot class="h-4 w-4" :class="verdictMeta[ops.verdict]?.text || 'text-zinc-400'" />
          <span class="text-sm font-bold tracking-wide" :class="verdictMeta[ops.verdict]?.text || 'text-zinc-200'">{{ verdictMeta[ops.verdict]?.label || 'SYSTEM' }}</span>
          <span class="text-[11px] text-zinc-500">derived {{ fmtWhen(ops.generated_at) }} · {{ ops.workflows.total }} workflows · {{ ops.datasets.total }} datasets · {{ ops.reports.scheduled }} reports · {{ ops.agents.agent_workflows }} agents · nothing stored</span>
        </div>

        <!-- ops cards -->
        <div class="mb-6 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <div class="rounded-2xl border border-zinc-800/80 bg-zinc-900/50 p-4">
            <div class="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wide text-zinc-500">
              <WorkflowIcon class="h-3.5 w-3.5 text-orange-400" /> Workflows
            </div>
            <p class="mt-2 text-2xl font-bold">{{ ops.workflows.total }} <span class="text-xs font-normal text-zinc-500">· {{ ops.workflows.active }} active</span></p>
            <div class="mt-2 flex flex-wrap gap-1.5 text-[10px]">
              <span class="rounded-full bg-sky-500/15 px-2 py-0.5 text-sky-300">{{ ops.workflows.running_now }} running now</span>
              <span class="rounded-full bg-zinc-800 px-2 py-0.5 text-zinc-300">{{ ops.workflows.runs_24h }} runs / 24h</span>
              <span class="rounded-full" :class="ops.workflows.failed_24h ? 'bg-rose-500/15 text-rose-300' : 'bg-emerald-500/15 text-emerald-300'">
                {{ ops.workflows.failed_24h }} failed / 24h
              </span>
            </div>
            <p class="mt-2 text-[10px] text-zinc-600">{{ ops.workflows.failure_rate_7d }}% failure rate over 7d</p>
          </div>

          <div class="rounded-2xl border border-zinc-800/80 bg-zinc-900/50 p-4">
            <div class="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wide text-zinc-500">
              <Database class="h-3.5 w-3.5 text-emerald-400" /> Datasets
            </div>
            <p class="mt-2 text-2xl font-bold">{{ ops.datasets.total }} <span class="text-xs font-normal text-zinc-500">· {{ fmtNum(ops.datasets.rows_total) }} rows</span></p>
            <div class="mt-2 flex flex-wrap gap-1.5 text-[10px]">
              <span class="rounded-full bg-emerald-500/15 px-2 py-0.5 text-emerald-300">{{ ops.datasets.healthy }} healthy</span>
              <span class="rounded-full bg-amber-500/15 px-2 py-0.5 text-amber-300">{{ ops.datasets.degraded }} degraded</span>
              <span class="rounded-full bg-rose-500/15 px-2 py-0.5 text-rose-300">{{ ops.datasets.unhealthy }} unhealthy</span>
            </div>
            <p class="mt-2 text-[10px] leading-relaxed text-zinc-600">
              {{ ops.datasets.violating_contracts }} violating contracts · {{ ops.datasets.stale_or_cold }} stale/cold
              <template v-if="ops.datasets.unscored"> · {{ ops.datasets.unscored }} unscored (budget)</template>
            </p>
            <NuxtLink v-if="ops.datasets.worst" :to="ops.datasets.worst.ref" class="mt-1 inline-flex items-center gap-1 text-[10px] text-zinc-500 hover:text-orange-300">
              worst: {{ ops.datasets.worst.name }} ({{ ops.datasets.worst.score }}) <ArrowRight class="h-2.5 w-2.5" />
            </NuxtLink>
          </div>

          <div class="rounded-2xl border border-zinc-800/80 bg-zinc-900/50 p-4">
            <div class="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wide text-zinc-500">
              <FileText class="h-3.5 w-3.5 text-violet-400" /> Reports
            </div>
            <p class="mt-2 text-2xl font-bold">{{ ops.reports.scheduled }} <span class="text-xs font-normal text-zinc-500">· {{ ops.reports.dashboards }} boards</span></p>
            <div class="mt-2 flex flex-wrap gap-1.5 text-[10px]">
              <span class="rounded-full bg-emerald-500/15 px-2 py-0.5 text-emerald-300">{{ ops.reports.ok_7d }} delivered / 7d</span>
              <span class="rounded-full" :class="ops.reports.error_7d ? 'bg-rose-500/15 text-rose-300' : 'bg-zinc-800 text-zinc-400'">{{ ops.reports.error_7d }} failures</span>
              <span class="rounded-full bg-zinc-800 px-2 py-0.5 text-zinc-400">{{ ops.reports.skipped_7d }} skipped</span>
            </div>
            <p v-if="ops.reports.last_error" class="mt-2 truncate text-[10px] text-rose-300/80" :title="ops.reports.last_error">{{ ops.reports.last_error }}</p>
          </div>

          <div class="rounded-2xl border border-zinc-800/80 bg-zinc-900/50 p-4">
            <div class="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wide text-zinc-500">
              <Bot class="h-3.5 w-3.5 text-pink-400" /> Agents
            </div>
            <p class="mt-2 text-2xl font-bold">{{ ops.agents.agent_workflows }} <span class="text-xs font-normal text-zinc-500">agent workflows</span></p>
            <div class="mt-2 flex flex-wrap gap-1.5 text-[10px]">
              <span class="rounded-full bg-zinc-800 px-2 py-0.5 text-zinc-300">{{ ops.agents.runs_7d }} runs / 7d</span>
              <span class="rounded-full" :class="ops.agents.errors_7d ? 'bg-rose-500/15 text-rose-300' : 'bg-emerald-500/15 text-emerald-300'">{{ ops.agents.errors_7d }} errors</span>
            </div>
            <p v-if="ops.agents.last_error" class="mt-2 truncate text-[10px] text-rose-300/80" :title="ops.agents.last_error">{{ ops.agents.last_error }}</p>
          </div>
        </div>

        <div class="grid gap-5 lg:grid-cols-3">
          <!-- event stream (2/3) -->
          <section class="lg:col-span-2">
            <div class="mb-3 flex flex-wrap items-center gap-2">
              <h2 class="mr-1 text-sm font-bold">Event stream</h2>
              <button
                v-for="c in typeChips" :key="c.value"
                class="rounded-full border px-2.5 py-1 text-[10px] font-medium transition"
                :class="typeFilter === c.value ? 'border-sky-500/50 bg-sky-500/10 text-sky-300' : 'border-zinc-800 bg-zinc-900 text-zinc-400 hover:text-zinc-200'"
                @click="typeFilter = c.value; setFilter()"
              >{{ c.label }}</button>
              <select
                v-model="severityFilter"
                class="ml-auto rounded-xl border border-zinc-800 bg-zinc-950 px-2 py-1.5 text-[10px] text-zinc-300 outline-none focus:border-sky-500/60"
                @change="setFilter"
              >
                <option value="">all severities</option>
                <option value="error">error</option>
                <option value="warn">warn</option>
                <option value="info">info</option>
              </select>
            </div>

            <div class="space-y-2">
              <div v-if="eventsLoading && !events.length" class="grid place-items-center rounded-2xl border border-zinc-800/80 py-10 text-zinc-600">
                <Loader2 class="h-5 w-5 animate-spin" />
              </div>
              <p v-else-if="!events.length" class="rounded-2xl border border-dashed border-zinc-800 py-10 text-center text-xs text-zinc-600">
                No events match - the estate is quiet.
              </p>
              <button
                v-for="e in events" :key="e.id"
                class="flex w-full items-start gap-3 rounded-xl border border-zinc-800/80 bg-zinc-900/50 px-3 py-2.5 text-left transition hover:border-sky-500/30 hover:bg-zinc-900"
                @click="openIncident(e)"
              >
                <span class="mt-1.5 h-2 w-2 shrink-0 rounded-full" :class="severityMeta[e.severity]?.dot" />
                <div class="min-w-0 flex-1">
                  <div class="flex flex-wrap items-center gap-2">
                    <span class="rounded px-1.5 py-0.5 font-mono text-[9px] font-semibold" :class="severityMeta[e.severity]?.chip">{{ e.type }}</span>
                    <span class="text-[10px] text-zinc-600">{{ fmtWhen(e.ts) }}</span>
                  </div>
                  <p class="mt-0.5 truncate text-xs text-zinc-300" :title="e.title">{{ e.title }}</p>
                  <p v-if="e.detail" class="mt-0.5 truncate text-[10px] text-zinc-600" :title="e.detail">{{ e.detail }}</p>
                </div>
                <ChevronsDown class="mt-2 h-3 w-3 shrink-0 text-zinc-700" />
              </button>
              <button
                v-if="events.length < eventsTotal"
                class="w-full rounded-xl border border-zinc-800 py-2 text-[11px] text-zinc-400 transition hover:border-sky-500/40 hover:text-sky-300 disabled:opacity-50"
                :disabled="eventsLoading"
                @click="loadEvents(false)"
              >
                {{ eventsLoading ? 'loading...' : `load more (${events.length}/${eventsTotal})` }}
              </button>
            </div>
          </section>

          <!-- side panels -->
          <section class="space-y-5">
            <div v-if="ops.incidents.length" class="rounded-2xl border border-rose-500/30 bg-rose-500/5 p-4">
              <h2 class="flex items-center gap-1.5 text-sm font-bold text-rose-300">
                <AlertTriangle class="h-3.5 w-3.5" /> Incidents (72h)
              </h2>
              <div class="mt-3 space-y-2">
                <button v-for="e in ops.incidents" :key="e.id" class="block w-full rounded-xl border border-zinc-800/80 bg-zinc-950/60 px-3 py-2 text-left transition hover:border-rose-500/40" @click="openIncident(e)">
                  <div class="flex items-center gap-2">
                    <span class="rounded bg-rose-500/15 px-1.5 py-0.5 font-mono text-[9px] font-semibold text-rose-300">{{ e.type }}</span>
                    <span class="text-[10px] text-zinc-600">{{ fmtWhen(e.ts) }}</span>
                  </div>
                  <p class="mt-0.5 line-clamp-2 text-[11px] text-zinc-300">{{ e.title }}</p>
                </button>
              </div>
            </div>

            <div v-if="ops.workflows.failing_workflows.length" class="rounded-2xl border border-zinc-800/80 bg-zinc-900/50 p-4">
              <h2 class="text-sm font-bold">Failing pipelines</h2>
              <div class="mt-3 space-y-2">
                <NuxtLink v-for="w in ops.workflows.failing_workflows" :key="w.workflow_id" :to="w.ref" class="block rounded-xl border border-zinc-800/80 bg-zinc-950/60 px-3 py-2 transition hover:border-orange-500/40">
                  <div class="flex items-center justify-between gap-2">
                    <span class="truncate text-xs font-semibold">{{ w.name }}</span>
                    <span class="shrink-0 rounded-full bg-rose-500/15 px-2 py-0.5 text-[10px] text-rose-300">{{ w.failures }} fails</span>
                  </div>
                  <p v-if="w.last_error" class="mt-0.5 line-clamp-1 text-[10px] text-zinc-600" :title="w.last_error">{{ w.last_error }}</p>
                  <p class="mt-0.5 text-[10px] text-zinc-600">last {{ fmtWhen(w.last_failed_at) }}</p>
                </NuxtLink>
              </div>
            </div>

            <div v-if="legacy?.ingestion?.pipelines?.length" class="rounded-2xl border border-zinc-800/80 bg-zinc-900/50 p-4">
              <h2 class="flex items-center gap-1.5 text-sm font-bold">
                <Inbox class="h-3.5 w-3.5 text-lime-400" /> Ingestion checkpoints
              </h2>
              <div class="mt-3 space-y-2">
                <NuxtLink v-for="p in legacy.ingestion.pipelines" :key="p.dataset_id + p.key" :to="p.ref" class="block rounded-xl border border-zinc-800/80 bg-zinc-950/60 px-3 py-2 transition hover:border-lime-500/40">
                  <div class="flex items-center justify-between gap-2">
                    <span class="truncate text-xs font-semibold">{{ p.dataset }}</span>
                    <span class="shrink-0 rounded bg-zinc-800 px-1.5 py-0.5 font-mono text-[9px] text-zinc-400">{{ p.key }}</span>
                  </div>
                  <p class="mt-0.5 font-mono text-[10px] text-zinc-500">wm: {{ p.watermark ?? 'null' }}</p>
                  <p class="text-[10px] text-zinc-600">
                    {{ p.runs }} runs · {{ fmtNum(p.rows_total) }} rows · {{ fmtWhen(p.last_run_at) }}
                    <template v-if="p.stats"> · {{ p.stats.written }}w/{{ p.stats.skipped }}s{{ p.stats.updated != null ? `/${p.stats.updated}u` : '' }}</template>
                  </p>
                </NuxtLink>
              </div>
            </div>
          </section>
        </div>
      </template>
    </main>

    <!-- incident drilldown modal (v57) -->
    <Teleport to="body">
      <div
        v-if="drillOpen"
        class="fixed inset-0 z-50 grid place-items-center bg-black/70 p-4 backdrop-blur-sm"
        @click.self="drillOpen = false"
      >
        <div class="flex max-h-[85vh] w-full max-w-3xl flex-col rounded-2xl border border-zinc-800 bg-zinc-900 shadow-2xl">
          <div class="flex items-center justify-between border-b border-zinc-800 px-5 py-4">
            <div class="min-w-0">
              <h3 class="flex items-center gap-2 text-sm font-bold">
                <ChevronsDown class="h-4 w-4 text-rose-400" /> Incident drilldown
                <span v-if="drill" class="rounded-full px-2 py-0.5 text-[10px] font-bold uppercase" :class="severityBadge[drill.severity] || 'bg-zinc-800 text-zinc-300'">{{ drill.severity }}</span>
              </h3>
              <p v-if="drill" class="mt-0.5 truncate text-[11px] text-zinc-500">
                {{ drill.workflow.name }} · {{ fmtWhen(drill.execution.started_at) }} · {{ fmtDur(drill.execution.duration_ms) }}
              </p>
            </div>
            <button class="rounded-lg p-1.5 text-zinc-500 transition hover:bg-zinc-800 hover:text-zinc-200" title="Close" @click="drillOpen = false">
              <X class="h-4 w-4" />
            </button>
          </div>

          <div class="min-h-0 flex-1 overflow-y-auto p-5">
            <div v-if="drillLoading" class="flex h-40 items-center justify-center text-sm text-zinc-500">
              <Loader2 class="mr-2 h-4 w-4 animate-spin" /> Walking the chain…
            </div>
            <p v-else-if="drillError" class="rounded-xl border border-rose-500/40 bg-rose-500/10 px-4 py-3 text-xs text-rose-300">{{ drillError }}</p>
            <template v-else-if="drill">
              <!-- the chain -->
              <div class="mb-5 space-y-1.5">
                <div v-for="s in drill.chain" :key="s.step" class="flex items-center gap-2 text-[11px]">
                  <component :is="stepIcon(s.step)" class="h-3 w-3 shrink-0" :class="s.step === 'error' ? 'text-rose-400' : s.label === 'none' ? 'text-zinc-600' : 'text-sky-400'" />
                  <span class="w-32 shrink-0 font-mono text-[10px] uppercase tracking-wide text-zinc-500">{{ s.step }}</span>
                  <span v-if="!s.ref" class="min-w-0 truncate text-zinc-300">{{ s.label }}</span>
                  <NuxtLink v-else :to="s.ref" class="min-w-0 truncate text-sky-300 hover:text-sky-200">{{ s.label }}</NuxtLink>
                </div>
              </div>

              <!-- failed node -->
              <div v-if="drill.failed_node" class="mb-4 rounded-xl border border-rose-500/30 bg-rose-500/5 p-3">
                <p class="text-[10px] font-semibold uppercase tracking-wide text-rose-400">Failed node</p>
                <p class="mt-1 text-sm font-bold">{{ drill.failed_node.node_name || drill.failed_node.node_id }}</p>
                <p class="font-mono text-[10px] text-zinc-500">{{ drill.failed_node.node_type }} · {{ fmtDur(drill.failed_node.duration_ms) }}</p>
                <p v-if="drill.failed_node.error" class="mt-1.5 break-all font-mono text-[10px] text-rose-200/90">{{ drill.failed_node.error }}</p>
                <details v-if="drill.failed_node.input !== undefined && drill.failed_node.input !== null" class="mt-2">
                  <summary class="cursor-pointer text-[10px] text-zinc-500 hover:text-zinc-300">input the node received</summary>
                  <pre class="mt-1 max-h-40 overflow-auto rounded-lg bg-zinc-950 p-2 font-mono text-[9px] leading-relaxed text-zinc-400">{{ JSON.stringify(drill.failed_node.input, null, 2) }}</pre>
                </details>
              </div>

              <!-- comparison with previous success -->
              <div v-if="drill.comparison_with_previous_success" class="mb-4 rounded-xl border border-zinc-800 bg-zinc-950/60 p-3">
                <p class="text-[10px] font-semibold uppercase tracking-wide text-zinc-400">Vs previous success ({{ fmtWhen(drill.comparison_with_previous_success.previous_started_at) }})</p>
                <p class="mt-1 text-[11px] text-zinc-400">
                  run duration {{ fmtDur(drill.comparison_with_previous_success.previous_duration_ms) }} → {{ fmtDur(drill.comparison_with_previous_success.failed_duration_ms) }}
                  · {{ drill.comparison_with_previous_success.previous_nodes }} nodes in the passing run
                </p>
                <p v-if="drill.comparison_with_previous_success.node" class="mt-0.5 text-[10px] text-zinc-500">
                  this node previously: {{ drill.comparison_with_previous_success.node.present_in_previous ? `${drill.comparison_with_previous_success.node.previous_status} in ${fmtDur(drill.comparison_with_previous_success.node.previous_duration_ms)}` : 'not present - it is new since the last success' }}
                </p>
              </div>

              <!-- related datasets -->
              <div v-if="drill.related_datasets.length" class="mb-4">
                <p class="mb-2 text-[10px] font-semibold uppercase tracking-wide text-zinc-400">Related datasets</p>
                <div class="flex flex-wrap gap-2">
                  <NuxtLink v-for="d in drill.related_datasets" :key="d.id" :to="d.ref" class="flex items-center gap-2 rounded-xl border border-zinc-800 bg-zinc-950/60 px-3 py-2 transition hover:border-emerald-500/40">
                    <Database class="h-3 w-3 text-emerald-400" />
                    <span class="text-[11px] font-semibold">{{ d.name }}</span>
                    <span v-if="d.health" class="rounded-full px-1.5 py-0.5 text-[9px]" :class="d.health.status === 'healthy' ? 'bg-emerald-500/15 text-emerald-300' : d.health.status === 'degraded' ? 'bg-amber-500/15 text-amber-300' : 'bg-rose-500/15 text-rose-300'">
                      {{ d.health.score }}
                    </span>
                    <span class="text-[9px] text-zinc-600">{{ fmtNum(d.rows) }} rows</span>
                  </NuxtLink>
                </div>
              </div>

              <!-- impact -->
              <div v-if="impactTotals" class="rounded-xl border border-sky-500/25 bg-sky-500/5 p-3">
                <p class="text-[10px] font-semibold uppercase tracking-wide text-sky-300">Impact</p>
                <p class="mt-1 text-[11px] text-zinc-300">
                  {{ impactTotals.totals.affected || 0 }} things downstream:
                  {{ impactTotals.totals.workflows || 0 }} workflows · {{ impactTotals.totals.dashboards || 0 }} dashboards ·
                  {{ impactTotals.totals.apps || 0 }} apps · {{ impactTotals.totals.models || 0 }} models ·
                  {{ impactTotals.totals.downstream_datasets || 0 }} downstream datasets
                </p>
                <p v-if="impactTotals.highest" class="mt-1 text-[10px] text-zinc-500">
                  highest risk: <span class="font-semibold text-zinc-300">{{ impactTotals.highest.kind }} {{ impactTotals.highest.name }}</span>
                </p>
              </div>

              <!-- v58: AI operations -->
              <div class="mt-5 border-t border-zinc-800 pt-4">
                <div class="flex flex-wrap items-center gap-2">
                  <button
                    class="flex items-center gap-1.5 rounded-xl border border-pink-500/40 bg-pink-500/10 px-3 py-1.5 text-[11px] font-semibold text-pink-300 transition hover:bg-pink-500/20 disabled:opacity-50"
                    :disabled="aiLoading"
                    @click="runInvestigation"
                  >
                    <Loader2 v-if="aiLoading" class="h-3.5 w-3.5 animate-spin" />
                    <Bot v-else class="h-3.5 w-3.5" />
                    {{ aiFindings ? 'Re-run AI investigation' : 'AI investigate' }}
                  </button>
                  <label class="flex cursor-pointer items-center gap-1.5 text-[10px] text-zinc-500">
                    <input v-model="aiNarrate" type="checkbox" class="h-3 w-3 accent-pink-500" />
                    narrate with LLM (fail-soft)
                  </label>
                </div>
                <p v-if="aiError" class="mt-2 rounded-lg border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-[10px] text-rose-300">{{ aiError }}</p>

                <div v-if="aiFindings" class="mt-3 space-y-3">
                  <!-- checklist -->
                  <div class="rounded-xl border border-zinc-800 bg-zinc-950/60 p-3">
                    <p class="text-[10px] font-semibold uppercase tracking-wide text-zinc-400">Investigation checklist</p>
                    <div class="mt-1.5 space-y-1">
                      <div v-for="c in aiFindings.checklist" :key="c.step" class="flex items-start gap-2 text-[10px]">
                        <CheckCircle2 v-if="c.ok" class="mt-0.5 h-3 w-3 shrink-0 text-emerald-400" />
                        <XCircle v-else class="mt-0.5 h-3 w-3 shrink-0 text-zinc-600" />
                        <span class="w-40 shrink-0 font-mono text-zinc-500">{{ c.step }}</span>
                        <span class="min-w-0 flex-1 truncate text-zinc-300" :title="c.detail">{{ c.detail }}</span>
                      </div>
                    </div>
                  </div>

                  <!-- cause + recommendation -->
                  <div class="rounded-xl border border-amber-500/30 bg-amber-500/5 p-3">
                    <p class="text-[10px] font-semibold uppercase tracking-wide text-amber-300">Cause</p>
                    <p class="mt-1 text-xs font-bold text-zinc-100">{{ aiFindings.cause.label }}
                      <span class="ml-1 rounded-full bg-amber-500/15 px-1.5 py-0.5 text-[9px] font-semibold text-amber-300">{{ aiFindings.cause.kind }} · {{ aiFindings.cause.confidence }}</span>
                    </p>
                    <p class="mt-0.5 break-all font-mono text-[9px] text-zinc-500">evidence: {{ aiFindings.cause.evidence }}</p>
                    <p v-for="(h, i) in aiFindings.hints" :key="i" class="mt-0.5 text-[10px] text-zinc-400">· {{ h }}</p>
                    <p class="mt-2 text-[10px] font-semibold uppercase tracking-wide text-sky-300">Recommendation</p>
                    <p class="mt-0.5 text-[11px] leading-relaxed text-zinc-300">{{ aiFindings.recommendation }}</p>
                  </div>

                  <!-- proposed action -->
                  <div v-if="aiFindings.proposed_action" class="rounded-xl border border-pink-500/30 bg-pink-500/5 p-3">
                    <div class="flex flex-wrap items-center justify-between gap-2">
                      <div>
                        <p class="text-[10px] font-semibold uppercase tracking-wide text-pink-300">Proposed change (AI proposes - you execute)</p>
                        <p class="mt-0.5 font-mono text-[10px] text-zinc-300">
                          policy patch: {{ Object.entries(aiFindings.proposed_action.patch).map(([k, v]) => `${k}=${v}`).join(' · ') }}
                        </p>
                        <p class="text-[10px] text-zinc-500">{{ aiFindings.proposed_action.rationale }}</p>
                      </div>
                      <button
                        class="rounded-xl bg-pink-500 px-3 py-1.5 text-[11px] font-bold text-white transition hover:bg-pink-400 disabled:opacity-50"
                        :disabled="applying"
                        @click="applyProposal"
                      >
                        <Loader2 v-if="applying" class="mr-1 inline h-3 w-3 animate-spin" />
                        Apply proposal
                      </button>
                    </div>
                    <p v-if="appliedNote" class="mt-2 rounded-lg border border-emerald-500/40 bg-emerald-500/10 px-3 py-2 text-[10px] text-emerald-300">{{ appliedNote }}</p>
                  </div>
                  <p v-else class="text-[10px] text-zinc-600">No automated change proposed for this cause - the recommendation above is the action.</p>

                  <!-- narration -->
                  <div v-if="aiFindings.narration" class="rounded-xl border border-zinc-800 bg-zinc-950/60 p-3">
                    <p class="text-[10px] font-semibold uppercase tracking-wide text-pink-300">LLM incident report</p>
                    <p class="mt-1 whitespace-pre-wrap text-[11px] leading-relaxed text-zinc-300">{{ aiFindings.narration }}</p>
                  </div>
                  <p v-else-if="aiFindings.narration_note" class="text-[10px] text-zinc-600">{{ aiFindings.narration_note }}</p>
                  <p class="text-[9px] text-zinc-600">{{ aiFindings.disclaimer }}</p>
                </div>
              </div>
            </template>
          </div>

          <p class="border-t border-zinc-800 px-5 py-3 text-[10px] leading-relaxed text-zinc-600">
            The chain is derived on the spot from the execution log, the workflow graph, dataset health and the impact engine - nothing stored.
          </p>
        </div>
      </div>
    </Teleport>
  </div>
</template>

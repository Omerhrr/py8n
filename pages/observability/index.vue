<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import {
  Activity, Loader2, RefreshCw, AlertTriangle, Database, GitBranch,
  Inbox, Send, ShieldAlert, Timer, ArrowRight, CircleDot,
} from 'lucide-vue-next'
import { useApi } from '~/composables/useApi'

// v53: data observability - one derived surface for "is the estate okay?".
// The overview composes dataset health, pipeline reliability, ingestion
// checkpoints and report deliveries; the event stream stitches the tables
// that already own the truth (versions, executions, deliveries, audits).

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

interface Overview {
  overall: string
  generated_at: string | null
  datasets: {
    total: number; scored: number; unscored: number
    healthy: number; degraded: number; unhealthy: number
    violating_contracts: number; contracts_total: number
    stale_or_cold: number; rows_total: number
    worst: { dataset_id: string; name: string; score: number; status: string; ref: string } | null
  }
  pipelines: {
    workflows_total: number; active: number
    runs_24h: number; runs_7d: number; failures_7d: number; failure_rate_7d: number
    failing_workflows: { workflow_id: string; name: string; failures: number; last_error: string | null; last_failed_at: string | null; ref: string }[]
  }
  ingestion: {
    checkpoints: number; rows_total: number; active_24h: number
    pipelines: { dataset: string; dataset_id: string; ref: string; key: string; watermark: string | null; runs: number; rows_total: number; last_run_at: string | null; stats: Record<string, any> | null }[]
  }
  deliveries: { ok_7d: number; error_7d: number; skipped_7d: number; last_error: string | null }
  incidents: ObsEvent[]
}

const { api } = useApi()
const loading = ref(true)
const overview = ref<Overview | null>(null)
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

const overallMeta: Record<string, { ring: string; text: string; label: string }> = {
  healthy: { ring: 'border-emerald-500/40 bg-emerald-500/10', text: 'text-emerald-300', label: 'Estate healthy' },
  degraded: { ring: 'border-amber-500/40 bg-amber-500/10', text: 'text-amber-300', label: 'Estate degraded' },
  unhealthy: { ring: 'border-rose-500/40 bg-rose-500/10', text: 'text-rose-300', label: 'Estate unhealthy' },
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

async function loadOverview() {
  try {
    overview.value = await api.get<Overview>('/observability/overview')
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'Could not load the overview'
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
  await Promise.all([loadOverview(), loadEvents(true)])
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
            <h1 class="text-lg font-bold tracking-tight">Observability</h1>
            <p class="-mt-0.5 text-[11px] text-zinc-500">One derived surface: estate health, pipeline reliability, ingestion and deliveries</p>
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

      <template v-else-if="overview">
        <!-- overall banner -->
        <div class="mb-5 flex flex-wrap items-center gap-3 rounded-2xl border p-4" :class="overallMeta[overview.overall]?.ring || 'border-zinc-800 bg-zinc-900/40'">
          <CircleDot class="h-4 w-4" :class="overallMeta[overview.overall]?.text || 'text-zinc-400'" />
          <span class="text-sm font-bold" :class="overallMeta[overview.overall]?.text || 'text-zinc-200'">{{ overallMeta[overview.overall]?.label || 'Estate' }}</span>
          <span class="text-[11px] text-zinc-500">derived {{ fmtWhen(overview.generated_at) }} - datasets health-scored, pipelines judged on 7d runs, nothing stored</span>
        </div>

        <!-- overview cards -->
        <div class="mb-6 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <div class="rounded-2xl border border-zinc-800/80 bg-zinc-900/50 p-4">
            <div class="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wide text-zinc-500">
              <Database class="h-3.5 w-3.5 text-emerald-400" /> Datasets
            </div>
            <p class="mt-2 text-2xl font-bold">{{ overview.datasets.total }} <span class="text-xs font-normal text-zinc-500">· {{ fmtNum(overview.datasets.rows_total) }} rows</span></p>
            <div class="mt-2 flex flex-wrap gap-1.5 text-[10px]">
              <span class="rounded-full bg-emerald-500/15 px-2 py-0.5 text-emerald-300">{{ overview.datasets.healthy }} healthy</span>
              <span class="rounded-full bg-amber-500/15 px-2 py-0.5 text-amber-300">{{ overview.datasets.degraded }} degraded</span>
              <span class="rounded-full bg-rose-500/15 px-2 py-0.5 text-rose-300">{{ overview.datasets.unhealthy }} unhealthy</span>
            </div>
            <p class="mt-2 text-[10px] leading-relaxed text-zinc-600">
              {{ overview.datasets.violating_contracts }} violating contracts · {{ overview.datasets.stale_or_cold }} stale/cold
              <template v-if="overview.datasets.unscored"> · {{ overview.datasets.unscored }} unscored (budget)</template>
            </p>
            <NuxtLink v-if="overview.datasets.worst" :to="overview.datasets.worst.ref" class="mt-1 inline-flex items-center gap-1 text-[10px] text-zinc-500 hover:text-orange-300">
              worst: {{ overview.datasets.worst.name }} ({{ overview.datasets.worst.score }}) <ArrowRight class="h-2.5 w-2.5" />
            </NuxtLink>
          </div>

          <div class="rounded-2xl border border-zinc-800/80 bg-zinc-900/50 p-4">
            <div class="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wide text-zinc-500">
              <GitBranch class="h-3.5 w-3.5 text-orange-400" /> Pipelines
            </div>
            <p class="mt-2 text-2xl font-bold">{{ overview.pipelines.runs_24h }} <span class="text-xs font-normal text-zinc-500">runs / 24h</span></p>
            <div class="mt-2 flex flex-wrap gap-1.5 text-[10px]">
              <span class="rounded-full bg-zinc-800 px-2 py-0.5 text-zinc-300">{{ overview.pipelines.active }}/{{ overview.pipelines.workflows_total }} active</span>
              <span class="rounded-full" :class="overview.pipelines.failure_rate_7d >= 20 ? 'bg-rose-500/15 text-rose-300' : overview.pipelines.failures_7d ? 'bg-amber-500/15 text-amber-300' : 'bg-emerald-500/15 text-emerald-300'">
                {{ overview.pipelines.failure_rate_7d }}% fail / 7d
              </span>
            </div>
            <p class="mt-2 text-[10px] text-zinc-600">{{ overview.pipelines.failures_7d }} failed of {{ overview.pipelines.runs_7d }} runs this week</p>
          </div>

          <div class="rounded-2xl border border-zinc-800/80 bg-zinc-900/50 p-4">
            <div class="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wide text-zinc-500">
              <Inbox class="h-3.5 w-3.5 text-lime-400" /> Ingestion
            </div>
            <p class="mt-2 text-2xl font-bold">{{ overview.ingestion.checkpoints }} <span class="text-xs font-normal text-zinc-500">checkpoints</span></p>
            <div class="mt-2 flex flex-wrap gap-1.5 text-[10px]">
              <span class="rounded-full bg-zinc-800 px-2 py-0.5 text-zinc-300">{{ overview.ingestion.active_24h }} ran / 24h</span>
              <span class="rounded-full bg-zinc-800 px-2 py-0.5 text-zinc-300">{{ fmtNum(overview.ingestion.rows_total) }} rows in</span>
            </div>
          </div>

          <div class="rounded-2xl border border-zinc-800/80 bg-zinc-900/50 p-4">
            <div class="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wide text-zinc-500">
              <Send class="h-3.5 w-3.5 text-violet-400" /> Deliveries / 7d
            </div>
            <p class="mt-2 text-2xl font-bold">{{ overview.deliveries.ok_7d }} <span class="text-xs font-normal text-emerald-400">ok</span></p>
            <div class="mt-2 flex flex-wrap gap-1.5 text-[10px]">
              <span class="rounded-full bg-rose-500/15 px-2 py-0.5 text-rose-300">{{ overview.deliveries.error_7d }} error</span>
              <span class="rounded-full bg-zinc-800 px-2 py-0.5 text-zinc-400">{{ overview.deliveries.skipped_7d }} skipped</span>
            </div>
            <p v-if="overview.deliveries.last_error" class="mt-2 truncate text-[10px] text-rose-300/80" :title="overview.deliveries.last_error">{{ overview.deliveries.last_error }}</p>
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
              <NuxtLink
                v-for="e in events" :key="e.id"
                :to="e.ref"
                class="flex items-start gap-3 rounded-xl border border-zinc-800/80 bg-zinc-900/50 px-3 py-2.5 transition hover:border-sky-500/30 hover:bg-zinc-900"
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
                <ArrowRight class="mt-2 h-3 w-3 shrink-0 text-zinc-700" />
              </NuxtLink>
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
            <div v-if="overview.incidents.length" class="rounded-2xl border border-rose-500/30 bg-rose-500/5 p-4">
              <h2 class="flex items-center gap-1.5 text-sm font-bold text-rose-300">
                <AlertTriangle class="h-3.5 w-3.5" /> Incidents (72h)
              </h2>
              <div class="mt-3 space-y-2">
                <NuxtLink v-for="e in overview.incidents" :key="e.id" :to="e.ref" class="block rounded-xl border border-zinc-800/80 bg-zinc-950/60 px-3 py-2 transition hover:border-rose-500/40">
                  <div class="flex items-center gap-2">
                    <span class="rounded bg-rose-500/15 px-1.5 py-0.5 font-mono text-[9px] font-semibold text-rose-300">{{ e.type }}</span>
                    <span class="text-[10px] text-zinc-600">{{ fmtWhen(e.ts) }}</span>
                  </div>
                  <p class="mt-0.5 line-clamp-2 text-[11px] text-zinc-300">{{ e.title }}</p>
                </NuxtLink>
              </div>
            </div>

            <div v-if="overview.pipelines.failing_workflows.length" class="rounded-2xl border border-zinc-800/80 bg-zinc-900/50 p-4">
              <h2 class="text-sm font-bold">Failing pipelines</h2>
              <div class="mt-3 space-y-2">
                <NuxtLink v-for="w in overview.pipelines.failing_workflows" :key="w.workflow_id" :to="w.ref" class="block rounded-xl border border-zinc-800/80 bg-zinc-950/60 px-3 py-2 transition hover:border-orange-500/40">
                  <div class="flex items-center justify-between gap-2">
                    <span class="truncate text-xs font-semibold">{{ w.name }}</span>
                    <span class="shrink-0 rounded-full bg-rose-500/15 px-2 py-0.5 text-[10px] text-rose-300">{{ w.failures }} fails</span>
                  </div>
                  <p v-if="w.last_error" class="mt-0.5 line-clamp-1 text-[10px] text-zinc-600" :title="w.last_error">{{ w.last_error }}</p>
                  <p class="mt-0.5 text-[10px] text-zinc-600">last {{ fmtWhen(w.last_failed_at) }}</p>
                </NuxtLink>
              </div>
            </div>

            <div v-if="overview.ingestion.pipelines.length" class="rounded-2xl border border-zinc-800/80 bg-zinc-900/50 p-4">
              <h2 class="text-sm font-bold">Ingestion checkpoints</h2>
              <div class="mt-3 space-y-2">
                <NuxtLink v-for="p in overview.ingestion.pipelines" :key="p.dataset_id + p.key" :to="p.ref" class="block rounded-xl border border-zinc-800/80 bg-zinc-950/60 px-3 py-2 transition hover:border-lime-500/40">
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
  </div>
</template>

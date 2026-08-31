<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import {
  BarChart3, RefreshCw, Play, Globe, CalendarClock, ShieldAlert,
  Hourglass, Ban, CheckCircle2, XCircle, Timer, Workflow, Cpu, Loader2, DatabaseBackup,
} from 'lucide-vue-next'
import { useApi } from '~/composables/useApi'
import type { InsightsPayload } from '~/types/node'

const { api } = useApi()

const data = ref<InsightsPayload | null>(null)
const loading = ref(true)
const days = ref<7 | 14 | 30>(14)
let timer: ReturnType<typeof setInterval> | null = null

async function loadAll() {
  try {
    data.value = await api.get<InsightsPayload>(`/insights?days=${days.value}`)
  } finally {
    loading.value = false
  }
}

function setDays(d: 7 | 14 | 30) {
  days.value = d
  loadAll()
}

onMounted(() => {
  loadAll()
  loadRetention()
  timer = setInterval(loadAll, 60_000) // silent refresh
})
onBeforeUnmount(() => {
  if (timer) clearInterval(timer)
})

const s = computed(() => data.value?.summary)

// ---- chart geometry (pure CSS columns, no chart lib)
const maxTotal = computed(() =>
  Math.max(4, ...((data.value?.timeline ?? []).map((b) => b.total))),
)

interface Segment { key: string; count: number; cls: string; label: string }
const SEG_STYLES: Record<string, string> = {
  success: 'bg-emerald-500',
  error: 'bg-rose-500',
  waiting: 'bg-violet-500',
  cancelled: 'bg-zinc-500',
  running: 'bg-amber-400',
}

function segments(b: InsightsPayload['timeline'][number]): Segment[] {
  const out: Segment[] = []
  for (const key of ['success', 'error', 'waiting', 'cancelled', 'running'] as const) {
    const count = b[key]
    if (count > 0) {
      out.push({ key, count, cls: SEG_STYLES[key], label: `${key} × ${count}` })
    }
  }
  return out
}

function dayLabel(date: string) {
  const [, m, d] = date.split('-')
  return `${Number(m)}/${Number(d)}`
}

// ---- v19: execution data retention -----------------------------------------
const retention = ref<{ retention_days: number; max_executions_per_workflow: number; last_purge_at: string | null; last_purge_deleted: number } | null>(null)
const savingRetention = ref(false)
const purging = ref(false)
const retentionMsg = ref('')

async function loadRetention() {
  try {
    retention.value = await api.get('/settings/retention')
  } catch {
    /* settings panel is optional */
  }
}

async function saveRetention() {
  if (!retention.value) return
  savingRetention.value = true
  retentionMsg.value = ''
  try {
    retention.value = await api.put('/settings/retention', {
      retention_days: retention.value.retention_days,
      max_executions_per_workflow: retention.value.max_executions_per_workflow,
    })
    retentionMsg.value = 'Policy saved'
    setTimeout(() => (retentionMsg.value = ''), 2500)
  } catch (e: any) {
    retentionMsg.value = e?.data?.detail || 'Save failed'
  } finally {
    savingRetention.value = false
  }
}

async function purgeNow() {
  purging.value = true
  retentionMsg.value = ''
  try {
    const res = await api.post<{ deleted_by_age: number; deleted_by_volume: number }>('/settings/retention/purge', {})
    retentionMsg.value = `Purged ${res.deleted_by_age + res.deleted_by_volume} execution record${res.deleted_by_age + res.deleted_by_volume === 1 ? '' : 's'}`
    await loadRetention()
    loadAll()
    setTimeout(() => (retentionMsg.value = ''), 3500)
  } catch (e: any) {
    retentionMsg.value = e?.data?.detail || 'Purge failed'
  } finally {
    purging.value = false
  }
}

const gridLines = computed(() => {
  const max = maxTotal.value
  const step = max <= 8 ? 2 : max <= 20 ? 5 : Math.ceil(max / 4 / 5) * 5
  const lines: { pct: number; value: number }[] = []
  for (let v = step; v < max; v += step) lines.push({ pct: (v / max) * 100, value: v })
  return lines
})

// ---- formatting
function fmtMs(ms: number | undefined) {
  if (!ms) return '-'
  if (ms < 1000) return `${ms}ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(ms < 10_000 ? 2 : 1)}s`
  const m = Math.floor(ms / 60_000)
  return `${m}m ${String(Math.round((ms % 60_000) / 1000)).padStart(2, '0')}s`
}

function pctWidth(n: number, total: number) {
  return total ? `${Math.max(2, Math.round((n / total) * 100))}%` : '0%'
}

const TRIGGER_META: Record<string, { icon: any; label: string; cls: string }> = {
  manual: { icon: Play, label: 'Manual', cls: 'text-zinc-300 border-zinc-700 bg-zinc-900' },
  webhook: { icon: Globe, label: 'Webhook', cls: 'text-sky-300 border-sky-800/60 bg-sky-950/40' },
  schedule: { icon: CalendarClock, label: 'Schedule', cls: 'text-emerald-300 border-emerald-800/60 bg-emerald-950/40' },
  error: { icon: ShieldAlert, label: 'Error', cls: 'text-rose-300 border-rose-800/60 bg-rose-950/40' },
}

function prettyType(t: string) {
  return t.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
}
</script>

<template>
  <div class="text-zinc-100">
    <!-- page header (app nav lives in the sidebar) -->
    <header class="border-b border-zinc-800/80 bg-zinc-950/90 backdrop-blur sticky top-0 z-20">
      <div class="mx-auto flex max-w-7xl items-center justify-between px-4 py-3.5 sm:px-6">
        <div class="flex items-center gap-3">
          <span class="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-orange-500 to-rose-500 shadow-lg shadow-orange-500/20">
            <BarChart3 class="h-4 w-4 text-white" />
          </span>
          <div>
            <h1 class="text-base font-bold leading-tight">Insights</h1>
            <p class="text-xs text-zinc-500">Execution analytics across the platform</p>
          </div>
        </div>
        <div class="flex items-center gap-2">
          <!-- range selector -->
          <div class="flex items-center rounded-xl border border-zinc-800 bg-zinc-900/60 p-0.5">
            <button
              v-for="d in [7, 14, 30] as const"
              :key="d"
              class="rounded-lg px-2.5 py-1.5 text-xs font-semibold transition"
              :class="days === d ? 'bg-orange-500 text-white shadow' : 'text-zinc-400 hover:text-zinc-200'"
              @click="setDays(d)"
            >
              {{ d }}d
            </button>
          </div>
          <button
            class="flex items-center gap-1.5 rounded-xl border border-zinc-800 bg-zinc-900/60 px-3 py-2 text-xs font-semibold text-zinc-300 transition hover:bg-zinc-800 hover:text-white"
            @click="loadAll"
          >
            <RefreshCw class="h-3.5 w-3.5" :class="loading && 'animate-spin'" />
            Refresh
          </button>
        </div>
      </div>
    </header>

    <main class="mx-auto max-w-7xl space-y-6 px-4 py-6 sm:px-6">
      <div v-if="loading && !data" class="flex h-64 items-center justify-center text-zinc-500">
        <Loader2 class="mr-2 h-5 w-5 animate-spin" /> Loading insights…
      </div>

      <template v-else-if="data">
        <!-- ================================================== summary cards -->
        <section class="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
          <div class="rounded-2xl border border-zinc-800/80 bg-zinc-900/50 p-4">
            <div class="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-zinc-500">
              <Workflow class="h-3.5 w-3.5" /> Total runs
            </div>
            <p class="mt-2 text-2xl font-bold tabular-nums">{{ s!.total }}</p>
          </div>
          <div class="rounded-2xl border border-zinc-800/80 bg-zinc-900/50 p-4">
            <div class="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-zinc-500">
              <CheckCircle2 class="h-3.5 w-3.5 text-emerald-400" /> Success rate
            </div>
            <p class="mt-2 text-2xl font-bold tabular-nums" :class="s!.success_rate >= 90 ? 'text-emerald-400' : s!.success_rate >= 60 ? 'text-amber-300' : 'text-rose-400'">
              {{ s!.success_rate }}%
            </p>
            <div class="mt-2 h-1 overflow-hidden rounded-full bg-zinc-800">
              <div class="h-full rounded-full bg-emerald-500 transition-all" :style="{ width: `${s!.success_rate}%` }" />
            </div>
          </div>
          <div class="rounded-2xl border border-zinc-800/80 bg-zinc-900/50 p-4">
            <div class="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-zinc-500">
              <XCircle class="h-3.5 w-3.5 text-rose-400" /> Errors
            </div>
            <p class="mt-2 text-2xl font-bold tabular-nums" :class="s!.error > 0 && 'text-rose-400'">{{ s!.error }}</p>
          </div>
          <div class="rounded-2xl border border-zinc-800/80 bg-zinc-900/50 p-4">
            <div class="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-zinc-500">
              <Hourglass class="h-3.5 w-3.5 text-violet-400" /> Waiting
            </div>
            <p class="mt-2 text-2xl font-bold tabular-nums" :class="s!.waiting > 0 && 'text-violet-300'">{{ s!.waiting }}</p>
          </div>
          <div class="rounded-2xl border border-zinc-800/80 bg-zinc-900/50 p-4">
            <div class="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-zinc-500">
              <Ban class="h-3.5 w-3.5 text-zinc-400" /> Cancelled
            </div>
            <p class="mt-2 text-2xl font-bold tabular-nums">{{ s!.cancelled }}</p>
          </div>
          <div class="rounded-2xl border border-zinc-800/80 bg-zinc-900/50 p-4">
            <div class="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-zinc-500">
              <Timer class="h-3.5 w-3.5 text-orange-400" /> Avg duration
            </div>
            <p class="mt-2 text-2xl font-bold tabular-nums text-orange-300">{{ fmtMs(s!.avg_duration_ms) }}</p>
          </div>
        </section>

        <!-- ================================================== timeline chart -->
        <section class="rounded-2xl border border-zinc-800/80 bg-zinc-900/50 p-5">
          <div class="mb-4 flex flex-wrap items-center justify-between gap-2">
            <h2 class="text-sm font-bold">Runs per day</h2>
            <div class="flex flex-wrap items-center gap-3 text-[11px] text-zinc-400">
              <span v-for="(cls, key) in SEG_STYLES" :key="key" class="flex items-center gap-1.5">
                <span class="h-2 w-2 rounded-sm" :class="cls" /> {{ key }}
              </span>
            </div>
          </div>

          <div class="relative ml-10 flex h-48">
            <!-- grid lines -->
            <div class="absolute inset-0">
              <div
                v-for="g in gridLines"
                :key="g.value"
                class="absolute left-0 right-0 border-t border-dashed border-zinc-800/70"
                :style="{ bottom: `${g.pct}%` }"
              >
                <span class="absolute -left-10 -top-2 w-9 text-right text-[10px] tabular-nums text-zinc-600">{{ g.value }}</span>
              </div>
            </div>

            <!-- columns -->
            <div class="flex flex-1 items-end gap-[3px]">
              <div
                v-for="b in data.timeline"
                :key="b.date"
                class="group relative flex h-full flex-1 flex-col justify-end"
                :title="`${b.date} - ${b.total} run(s): ${b.success} success, ${b.error} error, ${b.waiting} waiting, ${b.cancelled} cancelled`"
              >
                <div
                  v-for="seg in segments(b)"
                  :key="seg.key"
                  class="w-full transition-colors group-hover:brightness-125"
                  :class="seg.cls"
                  :style="{ height: pctWidth(seg.count, maxTotal) }"
                />
                <div v-if="b.total === 0" class="h-[2px] w-full rounded bg-zinc-800" />
              </div>
            </div>
          </div>

          <!-- x labels -->
          <div class="mt-2 flex gap-[3px] text-[10px] text-zinc-600 sm:text-[11px]">
            <div
              v-for="(b, i) in data.timeline"
              :key="b.date"
              class="flex-1 text-center"
            >
              {{ dayLabel(b.date) }}
            </div>
          </div>
        </section>

        <!-- ==================================== triggers + top workflows row -->
        <section class="grid gap-6 lg:grid-cols-3">
          <div class="space-y-4">
            <!-- trigger breakdown -->
            <div class="rounded-2xl border border-zinc-800/80 bg-zinc-900/50 p-5">
              <h2 class="mb-3 text-sm font-bold">Trigger mix</h2>
              <div class="flex flex-wrap gap-2">
                <span
                  v-for="(meta, key) in TRIGGER_META"
                  :key="key"
                  class="flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-xs font-semibold"
                  :class="meta.cls"
                >
                  <component :is="meta.icon" class="h-3.5 w-3.5" />
                  {{ meta.label }}
                  <span class="tabular-nums opacity-80">{{ data.trigger_breakdown[key] || 0 }}</span>
                </span>
              </div>
              <p class="mt-3 text-[11px] leading-relaxed text-zinc-600">
                How the {{ s!.total }} runs in the last {{ data.window.days }} days were started.
              </p>
            </div>

            <!-- node stats mini-summary -->
            <div class="rounded-2xl border border-zinc-800/80 bg-zinc-900/50 p-5">
              <div class="flex items-center gap-2">
                <Cpu class="h-4 w-4 text-orange-400" />
                <h2 class="text-sm font-bold">Node activity</h2>
              </div>
              <p class="mt-2 text-2xl font-bold tabular-nums">{{ s!.node_runs_total }}</p>
              <p class="text-[11px] text-zinc-600">node runs executed in window (loop batches included)</p>
            </div>
          </div>

          <!-- top workflows -->
          <div class="rounded-2xl border border-zinc-800/80 bg-zinc-900/50 p-5 lg:col-span-2">
            <h2 class="mb-4 text-sm font-bold">Top workflows</h2>
            <div v-if="data.top_workflows.length === 0" class="py-8 text-center text-sm text-zinc-600">
              No runs in this window yet.
            </div>
            <div v-else class="space-y-3">
              <div v-for="(w, i) in data.top_workflows" :key="w.workflow_id" class="flex items-center gap-3">
                <span class="w-5 shrink-0 text-right text-xs font-bold tabular-nums" :class="i === 0 ? 'text-orange-400' : 'text-zinc-600'">{{ i + 1 }}</span>
                <div class="min-w-0 flex-1">
                  <div class="flex items-baseline justify-between gap-2">
                    <p class="truncate text-sm font-medium">{{ w.workflow_name }}</p>
                    <p class="shrink-0 text-[11px] tabular-nums text-zinc-500">{{ w.runs }} runs · avg {{ fmtMs(w.avg_duration_ms) }}</p>
                  </div>
                  <div class="mt-1.5 flex items-center gap-2">
                    <div class="h-1.5 flex-1 overflow-hidden rounded-full bg-zinc-800">
                      <div class="h-full rounded-full bg-orange-500/80" :style="{ width: pctWidth(w.runs, data.top_workflows[0].runs) }" />
                    </div>
                    <span
                      class="w-16 shrink-0 rounded-full px-2 py-0.5 text-center text-[10px] font-bold tabular-nums"
                      :class="w.errors === 0 ? 'bg-emerald-950/60 text-emerald-400' : w.success_rate >= 60 ? 'bg-amber-950/60 text-amber-300' : 'bg-rose-950/60 text-rose-400'"
                    >
                      {{ w.success_rate }}%
                    </span>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </section>

        <!-- ================================================== node performance -->
        <section class="rounded-2xl border border-zinc-800/80 bg-zinc-900/50 p-5">
          <h2 class="mb-4 text-sm font-bold">Node performance</h2>
          <div v-if="data.node_stats.length === 0" class="py-8 text-center text-sm text-zinc-600">
            No node runs in this window.
          </div>
          <div v-else class="overflow-x-auto">
            <table class="w-full min-w-[560px] text-left text-sm">
              <thead>
                <tr class="border-b border-zinc-800 text-[11px] uppercase tracking-wide text-zinc-500">
                  <th class="pb-2 pr-4 font-semibold">Node type</th>
                  <th class="pb-2 pr-4 text-right font-semibold">Runs</th>
                  <th class="pb-2 pr-4 text-right font-semibold">Errors</th>
                  <th class="pb-2 pr-4 font-semibold">Error share</th>
                  <th class="pb-2 text-right font-semibold">Avg duration</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="n in data.node_stats" :key="n.node_type" class="border-b border-zinc-800/50 last:border-0">
                  <td class="py-2.5 pr-4">
                    <span class="rounded-md bg-zinc-800/80 px-2 py-0.5 font-mono text-xs text-zinc-300">{{ n.node_type }}</span>
                    <span class="ml-2 hidden text-xs text-zinc-500 md:inline">{{ prettyType(n.node_type) }}</span>
                  </td>
                  <td class="py-2.5 pr-4 text-right tabular-nums">{{ n.runs }}</td>
                  <td class="py-2.5 pr-4 text-right tabular-nums" :class="n.errors > 0 ? 'text-rose-400' : 'text-zinc-500'">{{ n.errors }}</td>
                  <td class="w-40 py-2.5 pr-4">
                    <div class="flex items-center gap-2">
                      <div class="h-1.5 w-24 overflow-hidden rounded-full bg-zinc-800">
                        <div class="h-full rounded-full" :class="n.error_rate > 20 ? 'bg-rose-500' : n.error_rate > 0 ? 'bg-amber-400' : 'bg-zinc-700'" :style="{ width: `${n.error_rate}%` }" />
                      </div>
                      <span class="text-xs tabular-nums text-zinc-500">{{ n.error_rate }}%</span>
                    </div>
                  </td>
                  <td class="py-2.5 text-right tabular-nums text-zinc-400">{{ fmtMs(n.avg_duration_ms) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </section>

        <!-- v19: execution data retention -->
        <section class="rounded-2xl border border-zinc-800 bg-zinc-900/40 p-5">
          <div class="mb-1 flex items-center justify-between">
            <h2 class="flex items-center gap-2 text-sm font-semibold text-zinc-200">
              <DatabaseBackup class="h-4 w-4 text-orange-400" /> Execution data retention
            </h2>
            <span v-if="retentionMsg" class="text-[11px] font-medium text-emerald-400">{{ retentionMsg }}</span>
          </div>
          <p class="mb-4 text-xs text-zinc-500">
            How long finished execution records are kept. Running executions are never purged. A background job enforces this daily; purge also runs at startup.
          </p>
          <div v-if="retention" class="grid gap-4 md:grid-cols-3">
            <div>
              <label class="mb-1 block text-[10px] font-semibold uppercase tracking-wider text-zinc-500">Keep for (days)</label>
              <input
                v-model.number="retention.retention_days"
                type="number"
                min="0"
                max="3650"
                class="w-full rounded-lg border border-zinc-800 bg-zinc-950 px-2.5 py-1.5 text-xs outline-none focus:border-orange-500/60"
              />
              <p class="mt-1 text-[10px] text-zinc-600">0 = keep forever</p>
            </div>
            <div>
              <label class="mb-1 block text-[10px] font-semibold uppercase tracking-wider text-zinc-500">Max runs per workflow</label>
              <input
                v-model.number="retention.max_executions_per_workflow"
                type="number"
                min="0"
                max="100000"
                class="w-full rounded-lg border border-zinc-800 bg-zinc-950 px-2.5 py-1.5 text-xs outline-none focus:border-orange-500/60"
              />
              <p class="mt-1 text-[10px] text-zinc-600">0 = unlimited - newest records survive</p>
            </div>
            <div class="flex flex-col justify-between gap-2">
              <div class="flex gap-2">
                <button
                  class="flex-1 rounded-lg bg-orange-500 py-1.5 text-xs font-semibold text-white transition hover:bg-orange-400 disabled:opacity-50"
                  :disabled="savingRetention"
                  @click="saveRetention"
                >
                  {{ savingRetention ? 'Saving…' : 'Save policy' }}
                </button>
                <button
                  class="flex-1 rounded-lg border border-zinc-700 py-1.5 text-xs font-semibold text-zinc-300 transition hover:border-zinc-500 disabled:opacity-50"
                  :disabled="purging"
                  @click="purgeNow"
                >
                  {{ purging ? 'Purging…' : 'Purge now' }}
                </button>
              </div>
              <p class="text-[10px] leading-snug text-zinc-600">
                <template v-if="retention.last_purge_at">
                  Last purge {{ new Date(retention.last_purge_at).toLocaleString() }} -
                  {{ retention.last_purge_deleted }} record{{ retention.last_purge_deleted === 1 ? '' : 's' }} removed
                </template>
                <template v-else>No purge has run yet</template>
              </p>
            </div>
          </div>
        </section>
      </template>
    </main>
  </div>
</template>

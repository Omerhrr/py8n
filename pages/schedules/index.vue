<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import {
  CalendarClock, RefreshCw, Clock, Zap, PauseCircle,
  AlertTriangle, ChevronRight, Play, Loader2,
} from 'lucide-vue-next'
import { useApi } from '~/composables/useApi'
import type { GlobalScheduleEntry } from '~/types/node'

const { api } = useApi()

const entries = ref<GlobalScheduleEntry[]>([])
const loading = ref(true)
const toggling = ref<string | null>(null)
let timer: ReturnType<typeof setInterval> | null = null

async function loadAll() {
  try {
    entries.value = await api.get<GlobalScheduleEntry[]>('/schedules')
  } finally {
    loading.value = false
  }
}

async function toggle(entry: GlobalScheduleEntry) {
  toggling.value = entry.workflow_id + entry.node_id
  try {
    const info = await api.post<{ is_active: boolean; next_run_at: string | null }>(
      `/workflows/${entry.workflow_id}/${entry.is_active ? 'deactivate' : 'activate'}`,
    )
    entry.is_active = info.is_active
    entry.next_runs = info.is_active ? entry.next_runs : []
    await loadAll() // re-sort with the new state
  } catch (e: any) {
    alert(e?.data?.detail || e?.message || 'Activation failed')
  } finally {
    toggling.value = null
  }
}

const stats = computed(() => {
  const total = entries.value.length
  const active = entries.value.filter((e) => e.is_active && e.next_runs.length).length
  const paused = entries.value.filter((e) => !e.is_active).length
  const nextUp = entries.value.find((e) => e.is_active && e.next_runs.length)
  return { total, active, paused, nextUp }
})

function fmtDate(iso: string) {
  return new Date(iso).toLocaleString(undefined, {
    weekday: 'short', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
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

onMounted(() => {
  loadAll()
  timer = setInterval(loadAll, 30_000) // silent refresh keeps countdowns honest
})
onBeforeUnmount(() => {
  if (timer) clearInterval(timer)
})
</script>

<template>
  <div class="text-zinc-100">
    <!-- page header (app nav lives in the sidebar) -->
    <header class="border-b border-zinc-800/80 bg-zinc-950/90 backdrop-blur sticky top-0 z-20">
      <div class="mx-auto flex max-w-7xl items-center justify-between px-4 py-3.5 sm:px-6">
        <div class="flex items-center gap-3">
          <div class="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-orange-500 to-rose-500 shadow-lg shadow-orange-500/20">
            <CalendarClock class="h-4 w-4 text-white" />
          </div>
          <div>
            <h1 class="text-lg font-bold tracking-tight">Schedules</h1>
            <p class="-mt-0.5 text-[11px] text-zinc-500">Every schedule trigger and its next fire time</p>
          </div>
        </div>
        <div class="flex items-center gap-2">
          <span
            v-if="stats.active"
            class="hidden items-center gap-1.5 rounded-full border border-emerald-500/30 bg-emerald-500/10 px-3 py-1 text-[11px] font-medium text-emerald-400 sm:flex"
          >
            <span class="relative flex h-2 w-2">
              <span class="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-60" />
              <span class="relative inline-flex h-2 w-2 rounded-full bg-emerald-500" />
            </span>
            Live - {{ stats.active }} active
          </span>
          <button
            class="inline-flex items-center gap-2 rounded-xl border border-zinc-700 px-3.5 py-2 text-sm font-medium text-zinc-300 transition hover:border-zinc-500 hover:text-white"
            title="Reload fire-time previews"
            @click="loadAll"
          >
            <RefreshCw class="h-4 w-4" /> Refresh
          </button>
        </div>
      </div>
    </header>

    <main class="mx-auto max-w-7xl px-4 py-8 sm:px-6">
      <!-- stats strip -->
      <section class="mb-8 grid grid-cols-2 gap-3 sm:grid-cols-4">
        <div class="rounded-2xl border border-zinc-800 bg-zinc-900/40 p-4">
          <p class="text-2xl font-bold text-zinc-100">{{ stats.total }}</p>
          <p class="text-[11px] uppercase tracking-wide text-zinc-500">Schedule nodes</p>
        </div>
        <div class="rounded-2xl border border-zinc-800 bg-zinc-900/40 p-4">
          <p class="text-2xl font-bold text-emerald-400">{{ stats.active }}</p>
          <p class="text-[11px] uppercase tracking-wide text-zinc-500">Active</p>
        </div>
        <div class="rounded-2xl border border-zinc-800 bg-zinc-900/40 p-4">
          <p class="text-2xl font-bold text-zinc-500">{{ stats.paused }}</p>
          <p class="text-[11px] uppercase tracking-wide text-zinc-500">Paused</p>
        </div>
        <div class="rounded-2xl border border-zinc-800 bg-zinc-900/40 p-4">
          <p v-if="stats.nextUp" class="truncate text-sm font-semibold text-yellow-400" :title="stats.nextUp.workflow_name">
            {{ stats.nextUp.workflow_name }}
          </p>
          <p v-else class="text-sm text-zinc-600">Nothing scheduled</p>
          <p class="mt-1 text-[11px] uppercase tracking-wide text-zinc-500">
            {{ stats.nextUp ? `Next up ${relTime(stats.nextUp.next_runs[0])}` : 'Next up' }}
          </p>
        </div>
      </section>

      <!-- loading skeleton -->
      <div v-if="loading" class="space-y-3">
        <div v-for="i in 3" :key="i" class="h-24 animate-pulse rounded-2xl border border-zinc-800 bg-zinc-900/50" />
      </div>

      <!-- empty state -->
      <div v-else-if="entries.length === 0" class="rounded-2xl border border-dashed border-zinc-800 p-12 text-center">
        <CalendarClock class="mx-auto mb-3 h-8 w-8 text-zinc-600" />
        <p class="font-medium text-zinc-300">No schedule triggers yet</p>
        <p class="mx-auto mt-2 max-w-md text-sm leading-relaxed text-zinc-500">
          Add a <span class="text-yellow-400">Schedule Trigger</span> node to any workflow, then activate
          the workflow to run it hands-free. Cron and interval modes are both supported.
        </p>
        <NuxtLink
          to="/"
          class="mt-5 inline-flex items-center gap-2 rounded-xl bg-orange-500 px-4 py-2 text-sm font-semibold text-white transition hover:bg-orange-400"
        >
          <Zap class="h-4 w-4" /> Build a workflow
        </NuxtLink>
      </div>

      <!-- schedule rows -->
      <div v-else class="space-y-3">
        <article
          v-for="entry in entries"
          :key="entry.workflow_id + entry.node_id"
          class="rounded-2xl border bg-zinc-900/40 p-5 transition hover:bg-zinc-900/70"
          :class="entry.error ? 'border-rose-500/40' : entry.is_active ? 'border-zinc-800 hover:border-emerald-500/30' : 'border-zinc-800/60'"
        >
          <div class="flex flex-wrap items-start justify-between gap-4">
            <div class="min-w-0 flex-1">
              <div class="flex flex-wrap items-center gap-2">
                <span
                  class="inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide"
                  :class="entry.error
                    ? 'bg-rose-500/15 text-rose-400'
                    : entry.is_active
                      ? 'bg-emerald-500/15 text-emerald-400'
                      : 'bg-zinc-800 text-zinc-500'"
                >
                  <Loader2 v-if="toggling === entry.workflow_id + entry.node_id" class="h-3 w-3 animate-spin" />
                  <template v-else>
                    <Play v-if="entry.is_active" class="h-3 w-3" />
                    <PauseCircle v-else class="h-3 w-3" />
                  </template>
                  {{ entry.error ? 'Broken' : entry.is_active ? 'Active' : 'Paused' }}
                </span>
                <span class="rounded-md bg-zinc-800/80 px-1.5 py-0.5 text-[10px] uppercase text-zinc-400">
                  {{ entry.mode === 'cron' ? 'Cron' : 'Interval' }}
                </span>
                <NuxtLink
                  :to="`/workflows/${entry.workflow_id}`"
                  class="truncate text-sm font-semibold text-zinc-100 transition hover:text-orange-400"
                >
                  {{ entry.workflow_name }}
                  <ChevronRight class="inline h-3.5 w-3.5 text-zinc-600" />
                </NuxtLink>
                <span class="text-[11px] text-zinc-500">{{ entry.node_name }}</span>
              </div>

              <div class="mt-2 flex items-center gap-2 font-mono text-xs text-zinc-400">
                <Clock class="h-3.5 w-3.5 shrink-0 text-yellow-500/80" />
                {{ entry.summary }}
              </div>

              <p v-if="entry.error" class="mt-2 flex items-center gap-1.5 text-xs text-rose-400">
                <AlertTriangle class="h-3.5 w-3.5 shrink-0" /> {{ entry.error }}
              </p>

              <div v-else-if="entry.is_active && entry.next_runs.length" class="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px]">
                <span class="text-emerald-400" :title="fmtDate(entry.next_runs[0])">
                  Next {{ relTime(entry.next_runs[0]) }} - {{ fmtDate(entry.next_runs[0]) }}
                </span>
                <span
                  v-for="run in entry.next_runs.slice(1, 4)"
                  :key="run"
                  class="text-zinc-600"
                  :title="fmtDate(run)"
                >
                  then {{ relTime(run) }}
                </span>
              </div>
              <p v-else class="mt-2 text-xs text-zinc-600">
                No upcoming runs - activate the workflow to enable this schedule.
              </p>
            </div>

            <button
              class="shrink-0 rounded-xl border px-3.5 py-2 text-xs font-semibold transition disabled:opacity-50"
              :class="entry.is_active
                ? 'border-zinc-700 text-zinc-300 hover:border-rose-500/50 hover:text-rose-400'
                : 'border-emerald-500/40 bg-emerald-500/10 text-emerald-400 hover:bg-emerald-500/20'"
              :disabled="toggling === entry.workflow_id + entry.node_id"
              @click="toggle(entry)"
            >
              <Loader2 v-if="toggling === entry.workflow_id + entry.node_id" class="mr-1 inline h-3.5 w-3.5 animate-spin" />
              {{ entry.is_active ? 'Pause' : 'Activate' }}
            </button>
          </div>
        </article>
      </div>
    </main>
  </div>
</template>

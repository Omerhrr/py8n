<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount, computed } from 'vue'
import { Gauge, Loader2, RefreshCw, Database, AlertTriangle, Lock, Filter, X } from 'lucide-vue-next'
import { useApi } from '~/composables/useApi'

definePageMeta({ layout: 'plain' })

const { api } = useApi()
const route = useRoute()
const slug = route.params.slug as string

// v47 share tokens: forward the ?t= this page received to every runtime call.
const shareTok = computed(() => {
  const t = route.query.t
  const raw = Array.isArray(t) ? (t[0] ?? '') : t
  return raw ? String(raw) : ''
})

const rt = ref<any | null>(null)
const loading = ref(true)
const forbidden = ref(false)
const error = ref<string | null>(null)
const lastRefresh = ref<Date | null>(null)
let timer: any = null

// v47 cross-filtering: {COL: [value, ...]} toggled by chart segment clicks;
// every value becomes a repeated filter.COL= param on the runtime call and
// ALL components re-compute server-side over the filtered frames.
const crossFilters = ref<Record<string, string[]>>({})

// chart id -> group_by column. The rendered chart payload does not carry
// group_by, so learn the mapping from the board config when it is readable
// (owner sessions and unclaimed boards); empty otherwise.
const groupBys = ref<Record<string, string>>({})
let groupBysTried = false

function toggleFilter(col: string, value: string) {
  const next: Record<string, string[]> = {}
  for (const [k, vals] of Object.entries(crossFilters.value)) next[k] = [...vals]
  const vals = next[col] || []
  const i = vals.indexOf(value)
  if (i >= 0) vals.splice(i, 1)
  else vals.push(value)
  if (vals.length) next[col] = vals
  else delete next[col]
  crossFilters.value = next
  load()
}

function clearFilters() {
  if (!Object.keys(crossFilters.value).length) return
  crossFilters.value = {}
  load()
}

// DashboardBoard @segment-click: toggle col=value for the chart's group_by.
// Charts without a resolvable group_by (and stats/tables) never get here.
function onSegmentClick(chart: any, label: string) {
  const col = chart?.group_by || groupBys.value[chart?.id]
  if (!col || !label) return
  toggleFilter(col, String(label))
}

async function learnGroupBys() {
  // Best effort - the config read fails silently for anonymous views under
  // enforced auth; those boards render fine but segments stay non-clickable.
  try {
    const name = rt.value?.dashboard?.name
    if (!name) return
    const b = await api.get<any>(`/dashboards/${encodeURIComponent(name)}`)
    const map: Record<string, string> = {}
    for (const c of b?.config?.components || []) {
      if (c.type === 'chart' && c.group_by) map[c.id] = c.group_by
    }
    groupBys.value = map
  } catch { /* stay inert, chips still work off the filters echo */ }
}

async function load() {
  try {
    const params = new URLSearchParams()
    if (shareTok.value) params.set('t', shareTok.value)
    for (const [col, vals] of Object.entries(crossFilters.value)) {
      for (const v of vals) params.append(`filter.${col}`, v)
    }
    const qs = params.toString()
    rt.value = await api.get<any>(`/dashboards/${slug}/runtime${qs ? `?${qs}` : ''}`)
    error.value = null
    forbidden.value = false
    lastRefresh.value = new Date()
    // server truth wins - keeps the chips in sync with the runtime echo
    crossFilters.value = { ...(rt.value?.filters || {}) }
    if (!groupBysTried && !Object.keys(groupBys.value).length) {
      groupBysTried = true
      learnGroupBys()
    }
  } catch (e: any) {
    if (e?.status === 403 || e?.statusCode === 403) forbidden.value = true
    else error.value = e?.data?.detail || e?.message || 'Dashboard not found (or not published)'
  } finally {
    loading.value = false
  }
}

function schedule() {
  if (timer) clearInterval(timer)
  // v46: refresh interval is configurable in the builder (10s..3600s, default 60s)
  const seconds = Math.min(3600, Math.max(10, rt.value?.refresh_seconds || 60))
  timer = setInterval(load, seconds * 1000)
}
onMounted(() => { load(); schedule() })
onBeforeUnmount(() => { if (timer) clearInterval(timer) })

const comps = computed(() => rt.value?.components || [])
const dsNames = computed(() => (rt.value?.datasets || []).map((d: any) => d.name))

// active cross-filters flattened for the chip row
const filterChips = computed(() => {
  const chips: { col: string; value: string }[] = []
  for (const [col, vals] of Object.entries(crossFilters.value)) {
    for (const v of vals) chips.push({ col, value: v })
  }
  return chips
})
</script>

<template>
  <div class="min-h-screen pb-14 text-zinc-100">
    <header class="border-b border-zinc-800/80 bg-zinc-950/90">
      <div class="mx-auto flex max-w-6xl items-center gap-3 px-4 py-4 lg:px-6">
        <span class="flex h-9 w-9 items-center justify-center rounded-xl bg-cyan-500/15">
          <Gauge class="h-4 w-4 text-cyan-400" />
        </span>
        <div class="min-w-0 flex-1">
          <h1 class="truncate text-base font-bold leading-tight">{{ rt?.dashboard?.name || 'Dashboard' }}</h1>
          <p v-if="rt?.dashboard?.description" class="truncate text-xs text-zinc-500">{{ rt.dashboard.description }}</p>
        </div>
        <div class="flex items-center gap-2 text-[11px] text-zinc-500">
          <template v-if="lastRefresh">
            <span class="hidden sm:inline">updated {{ lastRefresh.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' }) }}</span>
            <span class="rounded-full bg-emerald-500/10 px-2 py-0.5 font-medium text-emerald-400">live · 60s</span>
          </template>
          <button class="rounded-lg border border-zinc-800 bg-zinc-900/60 p-1.5 text-zinc-400 transition hover:border-zinc-600 hover:text-zinc-200" title="Refresh now" @click="load">
            <RefreshCw class="h-3.5 w-3.5" :class="loading && 'animate-spin'" />
          </button>
        </div>
      </div>
    </header>

    <div v-if="loading && !rt" class="mt-24 flex justify-center text-zinc-500">
      <Loader2 class="h-6 w-6 animate-spin" />
    </div>

    <div v-else-if="error" class="mx-auto mt-16 max-w-md rounded-2xl border border-amber-500/30 bg-amber-500/10 p-6 text-center">
      <AlertTriangle class="mx-auto h-6 w-6 text-amber-400" />
      <p class="mt-3 text-sm font-medium text-amber-300">{{ error }}</p>
      <p class="mt-1 text-xs text-zinc-500">Check the link or contact the board owner.</p>
    </div>

    <div v-else-if="forbidden" class="mx-auto mt-16 max-w-md rounded-2xl border border-amber-500/30 bg-amber-500/10 p-6 text-center">
      <Lock class="mx-auto h-6 w-6 text-amber-400" />
      <p class="mt-3 text-sm font-medium text-amber-300">This link requires a valid share token</p>
      <p class="mt-1 text-xs text-zinc-500">Ask the board owner for a fresh link with ?t=… (regenerating revokes old ones).</p>
    </div>

    <div v-else class="mx-auto max-w-6xl px-4 pt-5 lg:px-6">
      <p v-if="dsNames.length" class="mb-3 flex items-center gap-2 text-[11px] text-zinc-500">
        <Database class="h-3 w-3 text-sky-400" />
        <span class="truncate">data: {{ dsNames.join(' · ') }}</span>
      </p>
      <!-- v47: active cross-filters as dismissible chips -->
      <div v-if="filterChips.length" class="mb-3 flex flex-wrap items-center gap-1.5">
        <Filter class="h-3 w-3 shrink-0 text-cyan-400" />
        <span
          v-for="chip in filterChips"
          :key="`${chip.col}:${chip.value}`"
          class="flex items-center gap-1 rounded-full border border-cyan-500/30 bg-cyan-500/10 px-2 py-0.5 text-[11px] font-medium text-cyan-300"
        >
          {{ chip.col }}: {{ chip.value }}
          <button class="text-cyan-400/60 transition hover:text-cyan-200" title="Remove filter" @click="toggleFilter(chip.col, chip.value)">
            <X class="h-3 w-3" />
          </button>
        </span>
        <button
          class="rounded-full border border-zinc-700 px-2 py-0.5 text-[11px] text-zinc-400 transition hover:border-zinc-500 hover:text-zinc-200"
          @click="clearFilters"
        >Clear all</button>
      </div>
      <div class="grid gap-3 sm:grid-cols-2">
        <DashboardBoard :components="comps" :group-bys="groupBys" :active-filters="crossFilters" @segment-click="onSegmentClick" />
      </div>
      <p v-if="comps.length === 0" class="mt-10 text-center text-sm text-zinc-600">This board has no components yet.</p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount, computed } from 'vue'
import { Gauge, Loader2, RefreshCw, Database, AlertTriangle } from 'lucide-vue-next'
import { useApi } from '~/composables/useApi'

definePageMeta({ layout: 'plain' })

const { api } = useApi()
const route = useRoute()
const slug = route.params.slug as string

const rt = ref<any | null>(null)
const loading = ref(true)
const error = ref<string | null>(null)
const lastRefresh = ref<Date | null>(null)
let timer: any = null

async function load() {
  try {
    rt.value = await api.get<any>(`/dashboards/${slug}/runtime`)
    error.value = null
    lastRefresh.value = new Date()
  } catch (e: any) {
    error.value = e?.data?.detail || e?.message || 'Dashboard not found (or not published)'
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

    <div v-else class="mx-auto max-w-6xl px-4 pt-5 lg:px-6">
      <p v-if="dsNames.length" class="mb-3 flex items-center gap-2 text-[11px] text-zinc-500">
        <Database class="h-3 w-3 text-sky-400" />
        <span class="truncate">data: {{ dsNames.join(' · ') }}</span>
      </p>
      <div class="grid gap-3 sm:grid-cols-2">
        <DashboardBoard :components="comps" />
      </div>
      <p v-if="comps.length === 0" class="mt-10 text-center text-sm text-zinc-600">This board has no components yet.</p>
    </div>
  </div>
</template>

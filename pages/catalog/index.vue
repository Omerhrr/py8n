<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import {
  BookOpen, Loader2, Search, Tag, Database, ShieldCheck, ShieldAlert,
  ArrowDownToLine, ArrowUpFromLine, RefreshCw, CircleDot,
} from 'lucide-vue-next'
import { useApi } from '~/composables/useApi'

// v50: the data catalog - one derived inventory of every dataset: identity,
// shape, freshness, contract, and the workflows that produce / consume it.

interface CatalogEntry {
  id: string
  name: string
  description: string
  owner: string | null
  tags: string[]
  source: string
  rows: number
  columns: number
  schema_preview: { name: string; dtype: string }[]
  freshness: { last_write_at: string | null; age_minutes: number | null; tier: string }
  versions: { count: number; latest: number }
  contract: { present: boolean; on_violation: string | null; version: number }
  producers: string[]
  consumers: string[]
}

const { api } = useApi()
const loading = ref(true)
const entries = ref<CatalogEntry[]>([])
const q = ref('')
const tag = ref('')
const pageError = ref('')
const refreshing = ref(false)

const allTags = computed(() => {
  const set = new Set<string>()
  for (const e of entries.value) for (const t of e.tags) set.add(t)
  return [...set].sort()
})

const tierMeta: Record<string, { dot: string; label: string }> = {
  fresh: { dot: 'bg-emerald-400', label: 'fresh (< 1h)' },
  hours: { dot: 'bg-lime-400', label: 'today' },
  stale: { dot: 'bg-amber-400', label: 'stale (> 1d)' },
  cold: { dot: 'bg-rose-400', label: 'cold (> 1w)' },
  never: { dot: 'bg-zinc-600', label: 'no writes yet' },
}

function tierOf(e: CatalogEntry) {
  return tierMeta[e.freshness.tier] || tierMeta.never
}

function fmtAge(e: CatalogEntry): string {
  const m = e.freshness.age_minutes
  if (m === null || m === undefined) return 'never written'
  if (m < 1) return 'just now'
  if (m < 60) return `${Math.round(m)} min ago`
  const h = m / 60
  if (h < 24) return `${Math.round(h)}h ago`
  return `${Math.round(h / 24)}d ago`
}

async function load() {
  refreshing.value = true
  pageError.value = ''
  try {
    const params = new URLSearchParams()
    if (q.value.trim()) params.set('q', q.value.trim())
    if (tag.value) params.set('tag', tag.value)
    const qs = params.toString()
    const res = await api.get<{ entries: CatalogEntry[]; count: number }>(`/catalog${qs ? `?${qs}` : ''}`)
    entries.value = res.entries
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'Could not load the catalog'
  } finally {
    loading.value = false
    refreshing.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="text-zinc-100">
    <header class="sticky top-0 z-20 border-b border-zinc-800/80 bg-zinc-950/90 backdrop-blur">
      <div class="mx-auto max-w-7xl px-4 py-3.5 sm:px-6">
        <div class="flex flex-wrap items-center gap-3">
          <div class="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-orange-500 to-rose-500 shadow-lg shadow-orange-500/20">
            <BookOpen class="h-4 w-4 text-white" />
          </div>
          <div class="min-w-0 flex-1">
            <h1 class="text-lg font-bold tracking-tight">Data catalog</h1>
            <p class="-mt-0.5 text-[11px] text-zinc-500">Every dataset: freshness, contract, producers and consumers</p>
          </div>
          <div class="flex items-center gap-2">
            <div class="relative">
              <Search class="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-zinc-600" />
              <input
                v-model="q"
                class="w-52 rounded-xl border border-zinc-800 bg-zinc-950 py-2 pl-8 pr-3 text-xs outline-none transition placeholder:text-zinc-600 focus:border-orange-500/60"
                placeholder="Search name or description..."
                @keyup.enter="load"
              />
            </div>
            <select
              v-model="tag"
              class="rounded-xl border border-zinc-800 bg-zinc-950 px-2.5 py-2 text-xs text-zinc-300 outline-none focus:border-orange-500/60"
              @change="load"
            >
              <option value="">All tags</option>
              <option v-for="t in allTags" :key="t" :value="t">{{ t }}</option>
            </select>
            <button
              class="flex items-center gap-1.5 rounded-xl border border-zinc-800 bg-zinc-900 px-3 py-2 text-xs font-medium text-zinc-300 transition hover:border-orange-500/40 hover:text-orange-300 disabled:opacity-50"
              :disabled="refreshing"
              title="Rebuild the catalog view"
              @click="load"
            >
              <Loader2 v-if="refreshing" class="h-3.5 w-3.5 animate-spin" />
              <RefreshCw v-else class="h-3.5 w-3.5" />
            </button>
          </div>
        </div>
      </div>
    </header>

    <main class="mx-auto max-w-7xl px-4 py-6 sm:px-6">
      <div class="mb-5 flex items-start gap-3 rounded-2xl border border-zinc-800 bg-zinc-900/40 p-4">
        <CircleDot class="mt-0.5 h-4 w-4 shrink-0 text-emerald-400" />
        <p class="text-xs leading-relaxed text-zinc-400">
          The catalog is <span class="text-zinc-200">derived, never stored</span> - freshness comes from the version
          timeline, the contract badge from the dataset's data contract, and producers / consumers from lineage and a
          scan of active workflow graphs. It cannot drift from what actually happened. Open a dataset to see its full
          health report and contract.
        </p>
      </div>

      <div v-if="pageError" class="mb-4 flex items-start gap-2 rounded-xl border border-rose-500/40 bg-rose-500/10 px-4 py-3 text-sm text-rose-300">
        {{ pageError }}
      </div>

      <div v-if="loading" class="grid place-items-center py-16 text-zinc-600">
        <Loader2 class="h-6 w-6 animate-spin" />
      </div>

      <div v-else-if="!entries.length" class="grid place-items-center rounded-2xl border border-dashed border-zinc-800 py-16 text-center">
        <Database class="mb-3 h-8 w-8 text-zinc-700" />
        <p class="text-sm font-medium text-zinc-400">Nothing in the catalog yet</p>
        <p class="mt-1 max-w-md text-xs text-zinc-600">
          Create a dataset (or land one with a workflow) and it shows up here with freshness, contract and lineage.
        </p>
      </div>

      <div v-else class="grid gap-3 lg:grid-cols-2">
        <NuxtLink
          v-for="e in entries"
          :key="e.id"
          :to="`/datasets/${e.id}`"
          class="group block rounded-2xl border border-zinc-800/80 bg-zinc-900/50 p-4 transition hover:border-orange-500/40 hover:bg-zinc-900"
        >
          <div class="flex items-start gap-3">
            <div class="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl border border-emerald-500/30 bg-emerald-500/10 text-emerald-400">
              <Database class="h-4 w-4" />
            </div>
            <div class="min-w-0 flex-1">
              <div class="flex flex-wrap items-center gap-2">
                <span class="truncate text-sm font-semibold group-hover:text-orange-200">{{ e.name }}</span>
                <span class="flex items-center gap-1 rounded-full bg-zinc-800/80 px-2 py-0.5 text-[10px] text-zinc-400">
                  <span class="h-1.5 w-1.5 rounded-full" :class="tierOf(e).dot" />
                  {{ tierOf(e).label }}
                </span>
                <span
                  v-if="e.contract.present"
                  class="flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-semibold"
                  :class="e.contract.on_violation === 'error' ? 'bg-rose-500/15 text-rose-400' : 'bg-amber-500/15 text-amber-400'"
                  :title="`Data contract v${e.contract.version} (${e.contract.on_violation} mode)`"
                >
                  <component :is="e.contract.on_violation === 'error' ? ShieldAlert : ShieldCheck" class="h-3 w-3" />
                  contract · {{ e.contract.on_violation }}
                </span>
              </div>
              <p v-if="e.description" class="mt-1 line-clamp-2 text-[11px] text-zinc-500">{{ e.description }}</p>

              <div class="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-zinc-500">
                <span>{{ e.rows.toLocaleString() }} rows × {{ e.columns }} cols</span>
                <span>·</span>
                <span>v{{ e.versions.latest }} ({{ e.versions.count }} versions)</span>
                <span>·</span>
                <span>written {{ fmtAge(e) }}</span>
                <span v-if="e.owner">· by {{ e.owner }}</span>
              </div>

              <div v-if="e.schema_preview.length" class="mt-2 flex flex-wrap gap-1">
                <span
                  v-for="c in e.schema_preview"
                  :key="c.name"
                  class="rounded-md bg-zinc-800/60 px-1.5 py-0.5 font-mono text-[10px] text-zinc-400"
                  :title="c.dtype"
                >
                  {{ c.name }}<span class="text-zinc-600">:{{ c.dtype[0] }}</span>
                </span>
                <span v-if="e.columns > e.schema_preview.length" class="px-1 text-[10px] text-zinc-600">+{{ e.columns - e.schema_preview.length }}</span>
              </div>

              <div class="mt-2 flex flex-wrap items-center gap-3 text-[11px]">
                <span class="flex items-center gap-1 text-zinc-500" title="Workflows that write this dataset">
                  <ArrowDownToLine class="h-3 w-3 text-lime-400" />
                  {{ e.producers.length ? e.producers.join(', ') : 'no producers' }}
                </span>
                <span class="flex items-center gap-1 text-zinc-500" title="Active workflows that reference this dataset">
                  <ArrowUpFromLine class="h-3 w-3 text-sky-400" />
                  {{ e.consumers.length ? e.consumers.join(', ') : 'no consumers' }}
                </span>
              </div>

              <div v-if="e.tags.length" class="mt-2 flex flex-wrap gap-1">
                <span v-for="t in e.tags" :key="t" class="flex items-center gap-0.5 rounded-full bg-orange-500/10 px-1.5 py-0.5 text-[10px] text-orange-300">
                  <Tag class="h-2.5 w-2.5" /> {{ t }}
                </span>
              </div>
            </div>
          </div>
        </NuxtLink>
      </div>
    </main>
  </div>
</template>

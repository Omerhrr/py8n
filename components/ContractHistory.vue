<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { History, Loader2, ChevronDown, GitCompare, Plus, Minus, Pencil } from 'lucide-vue-next'
import { useApi } from '~/composables/useApi'

// v54: contract version history + diff. One shared component for the dataset
// page's contract section and the catalog's contract editor modal: lists the
// newest-first revision trail and diffs any two versions (added / removed /
// changed with per-field old->new).

interface Revision {
  version: number
  on_violation: string
  columns: { name: string; dtype: string; nullable: boolean; allowed?: any[] | null }[]
  note: string
  created_at: string | null
}

interface DiffRow {
  name: string
  field: string
  old: any
  new: any
}

const props = defineProps<{ datasetId: string }>()

const { api } = useApi()
const loading = ref(true)
const err = ref('')
const revisions = ref<Revision[]>([])
const currentVersion = ref(0)
const fromV = ref<number | null>(null)
const toV = ref<number | null>(null)
const diff = ref<any>(null)
const diffing = ref(false)
const expanded = ref<number | null>(null)  // expanded revision shows its columns

function fmtWhen(ts: string | null): string {
  if (!ts) return ''
  const d = new Date(ts)
  const s = Math.max(0, (Date.now() - d.getTime()) / 1000)
  if (s < 60) return 'just now'
  if (s < 3600) return `${Math.round(s / 60)}m ago`
  if (s < 86400) return `${Math.round(s / 3600)}h ago`
  return `${Math.round(s / 86400)}d ago`
}

function fmtVal(v: any): string {
  if (v === null || v === undefined) return 'any'
  if (Array.isArray(v)) return v.length ? v.map(String).join(', ') : '(empty)'
  if (typeof v === 'boolean') return v ? 'yes' : 'no'
  return String(v)
}

async function load() {
  loading.value = true
  err.value = ''
  try {
    const res = await api.get<{ revisions: Revision[]; current_version: number }>(`/datasets/${props.datasetId}/contract/revisions`)
    revisions.value = res.revisions
    currentVersion.value = res.current_version
    if (res.revisions.length >= 2) {
      fromV.value = res.revisions[1].version
      toV.value = res.revisions[0].version
      await runDiff()
    } else {
      fromV.value = toV.value = res.revisions[0]?.version ?? null
      diff.value = null
    }
  } catch (e: any) {
    err.value = e?.data?.detail || e?.message || 'Could not load the history'
  } finally {
    loading.value = false
  }
}

async function runDiff() {
  if (!fromV.value || !toV.value) return
  diffing.value = true
  try {
    diff.value = await api.get<any>(`/datasets/${props.datasetId}/contract/diff?from=${fromV.value}&to=${toV.value}`)
  } catch (e: any) {
    err.value = e?.data?.detail || e?.message || 'Diff failed'
  } finally {
    diffing.value = false
  }
}

function toggleExpand(v: number) {
  expanded.value = expanded.value === v ? null : v
}

onMounted(load)

defineExpose({ load })
</script>

<template>
  <div class="rounded-xl border border-zinc-800/80 bg-zinc-950/60">
    <div class="flex flex-wrap items-center gap-2 border-b border-zinc-800/60 px-3 py-2">
      <History class="h-3.5 w-3.5 text-zinc-500" />
      <span class="text-[11px] font-bold uppercase tracking-wide text-zinc-400">Version history</span>
      <span v-if="revisions.length" class="text-[10px] text-zinc-600">{{ revisions.length }} version{{ revisions.length === 1 ? '' : 's' }} · current v{{ currentVersion || '-' }}</span>
      <div v-if="revisions.length >= 2" class="ml-auto flex items-center gap-1.5">
        <select v-model.number="fromV" class="rounded-lg border border-zinc-800 bg-zinc-950 px-1.5 py-1 text-[10px] outline-none focus:border-amber-500/60">
          <option v-for="r in revisions" :key="r.version" :value="r.version">v{{ r.version }}</option>
        </select>
        <GitCompare class="h-3 w-3 text-zinc-600" />
        <select v-model.number="toV" class="rounded-lg border border-zinc-800 bg-zinc-950 px-1.5 py-1 text-[10px] outline-none focus:border-amber-500/60">
          <option v-for="r in revisions" :key="r.version" :value="r.version">v{{ r.version }}</option>
        </select>
        <button
          class="rounded-lg border border-zinc-800 px-2 py-1 text-[10px] font-medium text-zinc-300 transition hover:border-amber-500/40 hover:text-amber-300"
          :disabled="diffing"
          @click="runDiff"
        >diff</button>
      </div>
    </div>

    <div v-if="loading" class="grid place-items-center py-5 text-zinc-600"><Loader2 class="h-4 w-4 animate-spin" /></div>
    <p v-else-if="err" class="px-3 py-2 text-[11px] text-rose-300">{{ err }}</p>
    <p v-else-if="!revisions.length" class="px-3 py-3 text-center text-[11px] text-zinc-600">
      No history yet - save a contract and every later change is diffable here.
    </p>

    <template v-else>
      <!-- diff result -->
      <div v-if="diff" class="border-b border-zinc-800/60 bg-zinc-900/40 px-3 py-2.5">
        <p class="text-[11px] font-semibold" :class="diff.summary === 'no changes' ? 'text-emerald-300' : 'text-amber-300'">
          v{{ diff.from }} → v{{ diff.to }}: {{ diff.summary }}
          <span v-if="diff.from_on_violation !== diff.to_on_violation" class="ml-1 text-zinc-500">(mode {{ diff.from_on_violation }} → {{ diff.to_on_violation }})</span>
        </p>
        <div v-if="diff.summary !== 'no changes'" class="mt-1.5 space-y-1">
          <p v-for="(c, i) in diff.added" :key="`a${i}`" class="flex items-center gap-1.5 font-mono text-[10px] text-emerald-300">
            <Plus class="h-2.5 w-2.5" /> {{ c.name }}:{{ c.dtype }}<span class="text-zinc-600">added to the promise</span>
          </p>
          <p v-for="(c, i) in diff.removed" :key="`r${i}`" class="flex items-center gap-1.5 font-mono text-[10px] text-rose-300">
            <Minus class="h-2.5 w-2.5" /> {{ c.name }}:{{ c.dtype }}<span class="text-zinc-600">no longer promised</span>
          </p>
          <p v-for="(c, i) in diff.changed" :key="`c${i}`" class="flex items-center gap-1.5 font-mono text-[10px] text-amber-300">
            <Pencil class="h-2.5 w-2.5" /> {{ c.name }}.{{ c.field }}: <span class="text-zinc-500">{{ fmtVal(c.old) }}</span> → <span>{{ fmtVal(c.new) }}</span>
          </p>
        </div>
      </div>

      <!-- revision list -->
      <div class="divide-y divide-zinc-800/40">
        <div v-for="r in revisions" :key="r.version" class="px-3 py-2">
          <button class="flex w-full items-center gap-2 text-left" @click="toggleExpand(r.version)">
            <span class="rounded bg-zinc-800 px-1.5 py-0.5 font-mono text-[10px] font-semibold" :class="r.version === currentVersion ? 'text-amber-300' : 'text-zinc-400'">v{{ r.version }}</span>
            <span
              class="rounded px-1.5 py-0.5 text-[9px] font-semibold uppercase"
              :class="r.on_violation === 'error' ? 'bg-rose-500/15 text-rose-300' : 'bg-zinc-800 text-zinc-400'"
            >{{ r.on_violation }}</span>
            <span class="text-[10px] text-zinc-600">{{ r.columns.length }} col{{ r.columns.length === 1 ? '' : 's' }} · {{ fmtWhen(r.created_at) }}</span>
            <span v-if="r.note" class="truncate text-[10px] text-zinc-600">{{ r.note }}</span>
            <ChevronDown class="ml-auto h-3 w-3 shrink-0 text-zinc-600 transition" :class="expanded === r.version && 'rotate-180'" />
          </button>
          <div v-if="expanded === r.version" class="mt-1.5 flex flex-wrap gap-1">
            <span v-for="c in r.columns" :key="c.name" class="rounded-md bg-zinc-800/60 px-1.5 py-0.5 font-mono text-[10px] text-zinc-400">
              {{ c.name }}:{{ c.dtype }}<span v-if="!c.nullable" class="text-rose-400/70">*</span><span v-if="c.allowed && c.allowed.length" class="text-zinc-600"> in({{ c.allowed.length }})</span>
            </span>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>

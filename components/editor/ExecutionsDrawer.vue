<script setup lang="ts">
import { computed } from 'vue'
import {
  CheckCircle2, XCircle, MinusCircle, Loader2, ChevronDown, ChevronUp,
  Clock, Webhook, Play, Timer, PauseCircle, Image as ImageIcon, Network,
} from 'lucide-vue-next'
import type { ExecEvent, ExecutionSummary, NodeRun } from '~/types/node'

const { srcUrl } = useApi()

const props = defineProps<{
  executions: ExecutionSummary[]
  lastRun: { id: string; status: string; duration_ms?: number | null; error?: string | null; node_runs: NodeRun[] } | null
  liveEvents: ExecEvent[]
}>()

const emit = defineEmits<{ (e: 'select-run', id: string): void }>()

const isOpen = defineModel<boolean>('open', { default: false })

const statusIcon = (status: string) => {
  switch (status) {
    case 'success': return CheckCircle2
    case 'error': return XCircle
    case 'skipped': return MinusCircle
    case 'waiting': return PauseCircle
    default: return Loader2
  }
}
const statusClass = (status: string) => {
  switch (status) {
    case 'success': return 'text-emerald-400'
    case 'error': return 'text-rose-400'
    case 'skipped': return 'text-zinc-600'
    case 'waiting': return 'text-violet-400 animate-pulse'
    default: return 'text-amber-400 animate-pulse'
  }
}

const runs = computed(() => props.lastRun?.node_runs || [])

// v28: chart artifacts render inline; model artifacts get a badge
const chartSrc = (run: NodeRun) => {
  const o: any = run?.output
  return o && o.chart_type && o.artifact_id && o.artifact_url ? srcUrl(o.artifact_url) : null
}
const modelBadge = (run: NodeRun) => {
  const o: any = run?.output
  return o && o.model_id && o.model ? { label: o.model, metrics: o.metrics } : null
}

const prettyJson = (v: any) => {
  try {
    return JSON.stringify(v, null, 2)
  } catch {
    return String(v)
  }
}

function fmtTime(iso: string | null) {
  if (!iso) return '-'
  return new Date(iso).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

const triggerIcon = (t: string) => (t === 'webhook' ? Webhook : t === 'schedule' ? Clock : Play)
</script>

<template>
  <div class="flex h-full flex-col border-t border-zinc-800 bg-zinc-950/95">
    <!-- drawer header -->
    <button class="flex items-center justify-between px-4 py-2 text-left transition hover:bg-zinc-900/60" @click="isOpen = !isOpen">
      <span class="flex items-center gap-2 text-xs font-semibold uppercase tracking-wider text-zinc-400">
        <Timer class="h-3.5 w-3.5" /> Executions
        <span v-if="lastRun" class="flex items-center gap-1.5 rounded-full bg-zinc-900 px-2 py-0.5 text-[10px] normal-case">
          <component :is="statusIcon(lastRun.status)" class="h-3 w-3" :class="statusClass(lastRun.status)" />
          {{ lastRun.status }}
          <span v-if="lastRun.duration_ms != null" class="text-zinc-600">· {{ lastRun.duration_ms }}ms</span>
        </span>
      </span>
      <component :is="isOpen ? ChevronDown : ChevronUp" class="h-4 w-4 text-zinc-500" />
    </button>

    <div v-show="isOpen" class="grid min-h-0 flex-1 grid-cols-1 gap-0 md:grid-cols-[260px_1fr]">
      <!-- run history list -->
      <div class="max-h-40 overflow-y-auto border-t border-zinc-800/60 md:max-h-none md:border-r md:border-t-0">
        <p class="sticky top-0 bg-zinc-950/95 px-3 py-1.5 text-[10px] font-bold uppercase tracking-widest text-zinc-600">History</p>
        <button
          v-for="run in executions"
          :key="run.id"
          class="flex w-full items-center gap-2 border-b border-zinc-900 px-3 py-2 text-left text-[11px] transition hover:bg-zinc-900/70"
          :class="lastRun?.id === run.id ? 'bg-zinc-900/80' : ''"
          @click="emit('select-run', run.id)"
        >
          <component :is="statusIcon(run.status)" class="h-3.5 w-3.5 shrink-0" :class="statusClass(run.status)" />
          <span class="min-w-0 flex-1">
            <span class="block font-mono text-[10px] text-zinc-500">{{ run.id.slice(0, 10) }}…</span>
            <span class="block text-[10px] text-zinc-600">{{ fmtTime(run.started_at) }} · {{ run.trigger_type }}</span>
          </span>
          <span class="text-[10px] text-zinc-600">{{ run.duration_ms != null ? run.duration_ms + 'ms' : '' }}</span>
        </button>
        <p v-if="executions.length === 0" class="px-3 py-4 text-[11px] text-zinc-600">No runs yet - press ▶ Run.</p>
      </div>

      <!-- node run log -->
      <div class="overflow-y-auto p-3">
        <template v-if="runs.length">
          <div
            v-for="(run, i) in runs"
            :key="`${run.node_id}-${run.batch_index ?? 'x'}-${i}`"
            class="mb-2 rounded-xl border bg-zinc-900/40"
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
              <span v-if="run.duration_ms != null" class="ml-auto text-[10px] text-zinc-600">{{ run.duration_ms }}ms</span>
            </div>
            <p v-if="run.error" class="border-t border-zinc-800/60 px-3 py-1.5 font-mono text-[10px] leading-relaxed text-rose-400">
              {{ run.error }}
            </p>
            <details v-if="run.input !== null && run.input !== undefined" class="border-t border-zinc-800/60">
              <summary class="cursor-pointer px-3 py-1 text-[10px] font-semibold uppercase tracking-wider text-zinc-600 hover:text-zinc-400">
                input
              </summary>
              <pre class="max-h-40 overflow-auto px-3 pb-2 font-mono text-[10px] leading-relaxed text-zinc-500">{{ prettyJson(run.input) }}</pre>
            </details>
            <details v-if="run.output !== null && run.output !== undefined" class="border-t border-zinc-800/60">
              <summary class="cursor-pointer px-3 py-1 text-[10px] font-semibold uppercase tracking-wider text-zinc-600 hover:text-zinc-400">
                output
              </summary>
              <img
                v-if="chartSrc(run)"
                :src="chartSrc(run)"
                :alt="run.output?.title || 'chart'"
                class="mx-auto mt-2 max-h-56 rounded-lg border border-zinc-800 bg-white px-1"
              />
              <div
                v-else-if="modelBadge(run)"
                class="mx-3 mt-2 flex items-center gap-2 rounded-lg border border-indigo-500/30 bg-indigo-500/10 px-2.5 py-1.5"
              >
                <Network class="h-3.5 w-3.5 shrink-0 text-indigo-400" />
                <span class="text-[10px] font-semibold text-indigo-300">model saved · {{ modelBadge(run)!.label }}</span>
                <span class="ml-auto font-mono text-[10px] text-indigo-400/80">
                  {{ Object.entries(modelBadge(run)!.metrics || {}).filter(([k]) => k !== 'feature_importances' && k !== 'coefficients').map(([k, v]) => `${k}=${v}`).join(' · ') }}
                </span>
              </div>
              <div v-else-if="run.output?.artifact_id && run.output?.artifact_url" class="mx-3 mt-2 flex items-center gap-2 rounded-lg border border-zinc-800 bg-zinc-900/60 px-2.5 py-1.5">
                <ImageIcon class="h-3.5 w-3.5 shrink-0 text-zinc-500" />
                <span class="text-[10px] text-zinc-400">artifact saved</span>
              </div>
              <pre class="max-h-40 overflow-auto px-3 pb-2 font-mono text-[10px] leading-relaxed text-zinc-400">{{ prettyJson(run.output) }}</pre>
            </details>
          </div>
        </template>
        <div v-else-if="liveEvents.length" class="space-y-1 font-mono text-[10px] text-zinc-500">
          <p v-for="(e, i) in liveEvents" :key="i" class="truncate">
            <span class="text-zinc-700">{{ e.ts?.slice(11, 19) }}</span>
            {{ e.event }} {{ e.node_name || '' }} {{ e.status || '' }}
          </p>
        </div>
        <p v-else class="px-1 py-6 text-center text-[11px] text-zinc-600">
          Run the workflow to see per-node logs, outputs and errors here.
        </p>
      </div>
    </div>
  </div>
</template>

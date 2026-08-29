<script setup lang="ts">
import { computed } from 'vue'
import { Handle, Position } from '@vue-flow/core'
import {
  Play, Webhook, Clock, Globe, GitBranch, Braces, Terminal,
  Hourglass, Brain, Box, Zap, Filter, Split, GitMerge, Ungroup, Sigma, Workflow, Repeat, Mail, Slack, Ban, Pin, Bot,
} from 'lucide-vue-next'
import type { NodeDefinition, NodeRunStatus } from '~/types/node'

const props = defineProps<{
  id: string
  data: {
    spec: { id: string; type: string; name: string; parameters: Record<string, any>; disabled?: boolean; pinned_data?: any | null }
    definition: NodeDefinition
  }
}>()

const store = usePy8nStore()

const def = computed(() => props.data.definition)

const icon = computed(() => {
  const map: Record<string, any> = {
    play: Play, webhook: Webhook, clock: Clock, globe: Globe,
    'git-branch': GitBranch, braces: Braces, terminal: Terminal,
    hourglass: Hourglass, brain: Brain,
    filter: Filter, split: Split, 'git-merge': GitMerge,
    ungroup: Ungroup, sigma: Sigma, workflow: Workflow,
    repeat: Repeat, mail: Mail, slack: Slack, bot: Bot,
  }
  return map[def.value?.icon] || Box
})

const status = computed<NodeRunStatus>(() => store.nodeStates[props.id] || 'idle')

const statusRing = computed(() => {
  switch (status.value) {
    case 'running': return 'ring-2 ring-amber-400 shadow-amber-400/40 node-pulse'
    case 'success': return 'ring-2 ring-emerald-500 shadow-emerald-500/30'
    case 'error': return 'ring-2 ring-rose-500 shadow-rose-500/40'
    case 'waiting': return 'ring-2 ring-violet-400 shadow-violet-400/40 node-pulse'
    case 'skipped': return 'opacity-40 saturate-0'
    default: return 'ring-1 ring-zinc-700/60'
  }
})

const isTrigger = computed(() => def.value?.category === 'triggers')
const isDisabled = computed(() => !!props.data.spec.disabled)
const isPinned = computed(() => props.data.spec.pinned_data !== undefined && props.data.spec.pinned_data !== null)

function sourceTop(index: number, total: number): string {
  return `${((index + 1) / (total + 1)) * 100}%`
}
</script>

<template>
  <div
    class="w-52 rounded-xl border bg-zinc-900/95 shadow-xl transition-all"
    :class="[
      statusRing,
      isTrigger ? 'border-zinc-600' : 'border-zinc-700/70',
      isDisabled ? 'opacity-50 saturate-50 border-dashed' : '',
    ]"
    :style="{ borderTopColor: isDisabled ? '#a16207' : def?.color }"
    :title="isDisabled ? 'Disabled — input passes through at run time' : undefined"
  >
    <!-- target handles (inputs) -->
    <Handle
      v-for="(h, i) in (def?.inputs || [])"
      :key="'t' + i"
      type="target"
      :position="Position.Left"
      :id="h.key"
      :style="{ top: sourceTop(i, def?.inputs?.length || 1) }"
      :title="h.label"
    />

    <div class="flex items-center gap-2.5 px-3 py-2.5">
      <div
        class="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg"
        :style="{ backgroundColor: (def?.color || '#71717a') + '22', color: def?.color || '#a1a1aa' }"
      >
        <component :is="icon" class="h-4 w-4" />
      </div>
      <div class="min-w-0">
        <p class="truncate text-[13px] font-semibold leading-tight text-zinc-100">
          {{ props.data.spec.name || def?.name }}
        </p>
        <p class="truncate text-[10px] leading-tight text-zinc-500">{{ def?.name }}</p>
      </div>
      <div class="ml-auto flex shrink-0 items-center gap-1">
        <Pin
          v-if="isPinned"
          class="h-3.5 w-3.5 text-amber-400"
          title="Pinned — manual runs &amp; test steps return this data without executing"
        />
        <Ban v-if="isDisabled" class="h-3.5 w-3.5 text-amber-500" title="Disabled" />
        <Zap v-if="status === 'running'" class="h-3.5 w-3.5 animate-pulse text-amber-400" />
      </div>
    </div>

    <!-- type-specific hint line -->
    <div class="border-t border-zinc-800/80 px-3 py-1.5">
      <p class="truncate text-[10px] text-zinc-500">
        <template v-if="props.data.spec.type === 'manual_trigger'">
          payload: {{ JSON.stringify(props.data.spec.parameters?.payload || {}).slice(0, 40) }}
        </template>
        <template v-else-if="props.data.spec.type === 'http_request'">
          {{ props.data.spec.parameters?.method || 'GET' }} {{ (props.data.spec.parameters?.url || '').slice(0, 30) }}
        </template>
        <template v-else-if="props.data.spec.type === 'if_condition'">
          {{ props.data.spec.parameters?.operator || 'equals' }}
        </template>
        <template v-else-if="props.data.spec.type === 'llm_chat'">
          {{ props.data.spec.parameters?.provider === 'sandbox_bridge' ? 'free bridge model' : props.data.spec.parameters?.model || 'custom' }}
        </template>
        <template v-else-if="props.data.spec.type === 'schedule_trigger'">
          {{ props.data.spec.parameters?.mode === 'cron' ? props.data.spec.parameters?.cron : `every ${(props.data.spec.parameters?.interval_seconds ?? 300) / 60}m` }}
        </template>
        <template v-else-if="props.data.spec.type === 'delay'">
          {{ props.data.spec.parameters?.seconds ?? 2 }}s pause
        </template>
        <template v-else-if="props.data.spec.type === 'webhook_trigger'">
          {{ props.data.spec.parameters?.response_mode === 'last_node' ? 'responds with last node' : 'responds immediately' }}
        </template>
        <template v-else>{{ def?.description?.slice(0, 46) }}</template>
      </p>
    </div>

    <!-- source handles (outputs) -->
    <Handle
      v-for="(h, i) in (def?.outputs || [])"
      :key="'s' + i"
      type="source"
      :position="Position.Right"
      :id="h.key"
      :style="{ top: sourceTop(i, def?.outputs?.length || 1) }"
      :title="h.label"
    />
    <div
      v-if="(def?.outputs?.length || 0) > 1"
      class="pointer-events-none absolute -right-1 h-full w-px"
    >
      <span
        v-for="(h, i) in (def?.outputs || [])"
        :key="'l' + i"
        class="absolute -translate-y-1/2 -translate-x-9 text-[9px] font-semibold"
        :style="{ top: sourceTop(i, def?.outputs?.length || 1), color: def?.color }"
      >{{ h.label }}</span>
    </div>
  </div>
</template>

<style scoped>
.node-pulse {
  animation: pulse-ring 1.1s ease-in-out infinite;
}
@keyframes pulse-ring {
  0%, 100% { box-shadow: 0 0 0 0 rgba(251, 191, 36, 0.35); }
  50% { box-shadow: 0 0 0 8px rgba(251, 191, 36, 0); }
}
</style>

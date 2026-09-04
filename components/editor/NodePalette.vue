<script setup lang="ts">
import { computed, ref } from 'vue'
import { Play, Webhook, Clock, Globe, GitBranch, Braces, Terminal, Hourglass, Brain, BrainCircuit, Languages, Sparkles, Film, Box, Search, Filter, Split, GitMerge, Ungroup, Sigma, Workflow, Repeat, Mail, Slack, CirclePause } from 'lucide-vue-next'
import type { NodeDefinition } from '~/types/node'

const props = defineProps<{ definitions: NodeDefinition[] }>()
const emit = defineEmits<{ (e: 'add', def: NodeDefinition): void }>()

const search = ref('')

const iconFor = (name: string) => {
  const map: Record<string, any> = {
    play: Play, webhook: Webhook, clock: Clock, globe: Globe,
    'git-branch': GitBranch, braces: Braces, terminal: Terminal,
    hourglass: Hourglass, brain: Brain,
    'brain-circuit': BrainCircuit, languages: Languages, sparkles: Sparkles,
    film: Film,  // v65: video_features
    filter: Filter, split: Split, 'git-merge': GitMerge,
    ungroup: Ungroup, sigma: Sigma, workflow: Workflow,
    repeat: Repeat, mail: Mail, slack: Slack,
    'pause-circle': CirclePause,
  }
  return map[name] || Box
}

const categoryLabels: Record<string, { label: string; hint: string }> = {
  triggers: { label: 'Triggers', hint: 'Start the workflow' },
  actions: { label: 'Actions', hint: 'Do things' },
  logic: { label: 'Logic', hint: 'Branch & transform' },
  ai: { label: 'AI', hint: 'LLM superpowers' },
}

const grouped = computed(() => {
  const q = search.value.trim().toLowerCase()
  const groups: Record<string, NodeDefinition[]> = {}
  for (const d of props.definitions) {
    if (q && !`${d.name} ${d.type} ${d.description}`.toLowerCase().includes(q)) continue
    groups[d.category] = groups[d.category] || []
    groups[d.category].push(d)
  }
  return groups
})

function onDragStart(event: DragEvent, def: NodeDefinition) {
  event.dataTransfer?.setData('application/py8n-node', def.type)
  event.dataTransfer!.effectAllowed = 'move'
}
</script>

<template>
  <aside class="flex h-full w-60 shrink-0 flex-col border-r border-zinc-800 bg-zinc-950/80">
    <div class="border-b border-zinc-800/70 p-3">
      <div class="relative">
        <Search class="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-zinc-600" />
        <input
          v-model="search"
          placeholder="Search nodes…"
          class="w-full rounded-lg border border-zinc-800 bg-zinc-900 py-1.5 pl-8 pr-2 text-xs outline-none transition focus:border-orange-500/60"
        />
      </div>
    </div>

    <div class="flex-1 overflow-y-auto p-3">
      <div v-for="(nodes, cat) in grouped" :key="cat" class="mb-4">
        <p class="mb-1.5 px-1 text-[10px] font-bold uppercase tracking-widest text-zinc-600">
          {{ categoryLabels[cat]?.label || cat }}
          <span class="ml-1 font-normal normal-case text-zinc-700">{{ categoryLabels[cat]?.hint }}</span>
        </p>
        <button
          v-for="def in nodes"
          :key="def.type"
          draggable="true"
          class="group mb-1.5 flex w-full cursor-grab items-center gap-2.5 rounded-lg border border-zinc-800/80 bg-zinc-900/60 px-2.5 py-2 text-left transition hover:border-orange-500/40 hover:bg-zinc-800/80 active:cursor-grabbing"
          :title="def.description"
          @dragstart="onDragStart($event, def)"
          @click="emit('add', def)"
        >
          <span
            class="flex h-7 w-7 shrink-0 items-center justify-center rounded-md"
            :style="{ backgroundColor: def.color + '1f', color: def.color }"
          >
            <component :is="iconFor(def.icon)" class="h-3.5 w-3.5" />
          </span>
          <span class="min-w-0">
            <span class="block truncate text-xs font-medium text-zinc-200">{{ def.name }}</span>
            <span class="block truncate text-[10px] text-zinc-600">{{ def.type }}</span>
          </span>
        </button>
      </div>
      <p v-if="Object.keys(grouped).length === 0" class="px-2 py-6 text-center text-xs text-zinc-600">
        No nodes match "{{ search }}"
      </p>
    </div>

    <div class="border-t border-zinc-800/70 p-3 text-[10px] leading-relaxed text-zinc-600">
      Drag a node onto the canvas or click to add. Connect ports to build the flow - execution order is computed automatically.
    </div>
  </aside>
</template>

<script setup lang="ts">
// v19: sticky note canvas element - annotation only, never executes.
// Rendered as a Vue Flow custom node type ("sticky"); edits mutate the
// node's spec in place (same pattern as PNodeCard) and the page commits
// history / dirty state on input.
import { computed } from 'vue'

const props = defineProps<{
  id: string
  selected?: boolean
  data: {
    spec: { parameters: { text?: string; color?: string } }
  }
}>()

const store = usePy8nStore()

const COLORS: Record<string, { bg: string; border: string; dot: string }> = {
  amber: { bg: 'bg-amber-500/15', border: 'border-amber-500/50', dot: 'bg-amber-400' },
  emerald: { bg: 'bg-emerald-500/15', border: 'border-emerald-500/50', dot: 'bg-emerald-400' },
  sky: { bg: 'bg-sky-500/15', border: 'border-sky-500/50', dot: 'bg-sky-400' },
  rose: { bg: 'bg-rose-500/15', border: 'border-rose-500/50', dot: 'bg-rose-400' },
  violet: { bg: 'bg-violet-500/15', border: 'border-violet-500/50', dot: 'bg-violet-400' },
}

const colors = computed(() => COLORS[props.data.spec.parameters?.color || 'amber'] || COLORS.amber)

function onInput(e: Event) {
  props.data.spec.parameters = {
    ...(props.data.spec.parameters || {}),
    text: (e.target as HTMLTextAreaElement).value,
  }
  store.markDirty()
}
</script>

<template>
  <div
    class="w-56 rounded-xl border shadow-lg transition"
    :class="[colors.bg, colors.border, selected ? 'ring-2 ring-orange-400/70' : '']"
  >
    <div class="flex items-center justify-between px-2.5 pt-2">
      <span class="flex items-center gap-1.5 text-[10px] font-bold uppercase tracking-wider text-zinc-400">
        <span class="h-1.5 w-1.5 rounded-full" :class="colors.dot" /> Note
      </span>
    </div>
    <textarea
      :value="data.spec.parameters?.text || ''"
      class="h-24 w-full resize-none bg-transparent px-2.5 py-1.5 text-xs leading-relaxed text-zinc-200 outline-none placeholder:text-zinc-600"
      placeholder="Write a note…"
      @input="onInput"
    />
  </div>
</template>

<script setup lang="ts">
// v19: expression input with autocomplete for Py8n's Jinja2 {{ }} templates.
// Suggestion sources: template context variables (input / nodes.*.output /
// env.KEY / workflow / execution) and common Jinja2 filters. Opens when the
// value contains "{{" and narrows to the token being typed.
import { computed, ref } from 'vue'

const props = withDefaults(
  defineProps<{
    modelValue: string
    multiline?: boolean
    rows?: number
    placeholder?: string
    nodeNames?: string[]
    envKeys?: string[]
  }>(),
  { multiline: false, rows: 3, placeholder: '', nodeNames: () => [], envKeys: () => [] },
)

const emit = defineEmits<{ (e: 'update:modelValue', v: string): void }>()

const inputEl = ref<HTMLInputElement | HTMLTextAreaElement | null>(null)
const active = ref(-1)
const showSuggest = ref(false)

interface Suggestion {
  text: string
  hint: string
  group: string
}

const VARIABLES: Suggestion[] = [
  { text: 'input', hint: 'current node input', group: 'Context' },
  { text: 'inputs', hint: 'all incoming inputs (merged)', group: 'Context' },
  { text: 'workflow.id', hint: 'workflow id', group: 'Context' },
  { text: 'workflow.name', hint: 'workflow name', group: 'Context' },
  { text: 'execution.id', hint: 'execution id', group: 'Context' },
  { text: 'execution.trigger_type', hint: 'manual | webhook | schedule', group: 'Context' },
]

const FILTERS: Suggestion[] = [
  { text: 'upper', hint: 'UPPERCASE', group: 'Filter' },
  { text: 'lower', hint: 'lowercase', group: 'Filter' },
  { text: 'trim', hint: 'strip whitespace', group: 'Filter' },
  { text: 'title', hint: 'Title Case', group: 'Filter' },
  { text: 'capitalize', hint: 'First letter up', group: 'Filter' },
  { text: 'length', hint: 'size of string/list/dict', group: 'Filter' },
  { text: 'first', hint: 'first item', group: 'Filter' },
  { text: 'last', hint: 'last item', group: 'Filter' },
  { text: 'join(", ")', hint: 'join list into string', group: 'Filter' },
  { text: 'tojson', hint: 'serialize to JSON', group: 'Filter' },
  { text: 'fromjson', hint: 'parse JSON string', group: 'Filter' },
  { text: 'default("-")', hint: 'fallback when undefined', group: 'Filter' },
  { text: 'replace("a", "b")', hint: 'substring replace', group: 'Filter' },
  { text: 'round(2)', hint: 'round number', group: 'Filter' },
  { text: 'int', hint: 'cast to int', group: 'Filter' },
  { text: 'abs', hint: 'absolute value', group: 'Filter' },
  { text: 'sum', hint: 'sum a list of numbers', group: 'Filter' },
  { text: 'unique', hint: 'dedupe a list', group: 'Filter' },
  { text: 'sort', hint: 'sort a list', group: 'Filter' },
  { text: 'keys', hint: 'dict keys', group: 'Filter' },
  { text: 'items', hint: 'dict as [key, value] pairs', group: 'Filter' },
]

const isExpression = computed(() => (props.modelValue || '').includes('{{'))

// token currently being typed = text after the last "{{" (or after the last
// "|" when a filter is being written)
const segment = computed(() => {
  const v = props.modelValue || ''
  const open = v.lastIndexOf('{{')
  if (open === -1) return { start: -1, text: '', inFilter: false }
  const after = v.slice(open + 2)
  const pipe = after.lastIndexOf('|')
  if (pipe !== -1) return { start: open + 2 + pipe + 1, text: after.slice(pipe + 1), inFilter: true }
  return { start: open + 2, text: after, inFilter: false }
})

const suggestions = computed<Suggestion[]>(() => {
  const seg = segment.value
  if (seg.start === -1 || !showSuggest.value) return []
  const q = seg.text.trim().toLowerCase()
  let pool: Suggestion[]
  if (seg.inFilter) {
    pool = FILTERS
  } else {
    pool = [
      ...VARIABLES,
      ...props.nodeNames.map<Suggestion>((n) => ({
        text: `nodes.${n}.output`,
        hint: `output of node "${n}"`,
        group: 'Nodes',
      })),
      ...props.envKeys.map<Suggestion>((k) => ({
        text: `env.${k}`,
        hint: 'environment variable',
        group: 'Env',
      })),
    ]
  }
  const matched = q ? pool.filter((s) => s.text.toLowerCase().includes(q)) : pool
  return matched.slice(0, 8)
})

function onInput(v: string) {
  emit('update:modelValue', v)
  showSuggest.value = true
  active.value = suggestions.value.length ? 0 : -1
}

function apply(s: Suggestion) {
  const seg = segment.value
  if (seg.start === -1) return
  const v = props.modelValue || ''
  const before = v.slice(0, seg.start)
  const rest = v.slice(seg.start)
  // keep any trailing part after a space (mid-sentence completions)
  const trailing = /^\s+\S/.test(seg.text) ? '' : rest.replace(/^[^\s|}]+/, '')
  let next = before + s.text + trailing
  // a fresh variable insertion right before "}}" keeps the template valid
  if (!next.includes('}}')) next += ' }}'
  emit('update:modelValue', next)
  showSuggest.value = false
  requestAnimationFrame(() => inputEl.value?.focus())
}

function onKeydown(e: KeyboardEvent) {
  if (!suggestions.value.length) return
  if (e.key === 'ArrowDown') {
    e.preventDefault()
    active.value = (active.value + 1) % suggestions.value.length
  } else if (e.key === 'ArrowUp') {
    e.preventDefault()
    active.value = (active.value - 1 + suggestions.value.length) % suggestions.value.length
  } else if (e.key === 'Enter' || (e.key === 'Tab' && suggestions.value.length)) {
    if (active.value >= 0) {
      e.preventDefault()
      apply(suggestions.value[active.value])
    }
  } else if (e.key === 'Escape') {
    showSuggest.value = false
  }
}
</script>

<template>
  <div class="relative">
    <div
      v-if="!multiline"
      class="flex items-stretch overflow-hidden rounded-lg border border-zinc-800 bg-zinc-900 transition focus-within:border-orange-500/60"
    >
      <span
        class="flex select-none items-center border-r border-zinc-800 px-2 font-mono text-[10px] font-bold"
        :class="isExpression ? 'bg-orange-500/15 text-orange-300' : 'bg-zinc-950 text-zinc-600'"
        title="Supports Jinja2 expressions - type {{"
        >fx</span
      >
      <input
        ref="inputEl"
        :value="modelValue"
        :placeholder="placeholder"
        class="min-w-0 flex-1 bg-transparent px-2.5 py-1.5 font-mono text-xs outline-none"
        @input="onInput(($event.target as HTMLInputElement).value)"
        @focus="showSuggest = true; active = 0"
        @blur="showSuggest = false"
        @keydown="onKeydown"
      />
    </div>
    <textarea
      v-else
      ref="inputEl as any"
      :value="modelValue"
      :rows="rows"
      :placeholder="placeholder"
      spellcheck="false"
      class="w-full rounded-lg border border-zinc-800 bg-zinc-900 px-2.5 py-2 font-mono text-[11px] outline-none transition focus:border-orange-500/60"
      @input="onInput(($event.target as HTMLTextAreaElement).value)"
      @focus="showSuggest = true; active = 0"
      @blur="showSuggest = false"
      @keydown="onKeydown"
    />
    <div
      v-if="suggestions.length"
      class="absolute left-0 right-0 top-full z-30 mt-1 overflow-hidden rounded-lg border border-zinc-700 bg-zinc-900 shadow-2xl"
    >
      <button
        v-for="(s, i) in suggestions"
        :key="s.group + s.text"
        type="button"
        class="flex w-full items-center justify-between gap-2 px-2.5 py-1.5 text-left text-xs transition"
        :class="i === active ? 'bg-orange-500/15 text-orange-200' : 'text-zinc-300 hover:bg-zinc-800'"
        @mousedown.prevent="apply(s)"
        @mousemove="active = i"
      >
        <span class="truncate font-mono">{{ s.text }}</span>
        <span class="flex shrink-0 items-center gap-1.5">
          <span class="text-[10px] text-zinc-500">{{ s.hint }}</span>
          <span class="rounded bg-zinc-800 px-1 py-0.5 text-[9px] uppercase tracking-wider text-zinc-500">{{ s.group }}</span>
        </span>
      </button>
      <p class="border-t border-zinc-800 bg-zinc-950/60 px-2.5 py-1 text-[9px] text-zinc-600">
        ↑↓ browse · Enter insert · Esc dismiss
      </p>
    </div>
  </div>
</template>

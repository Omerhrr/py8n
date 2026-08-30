<script setup lang="ts">
// v20: keyboard & mouse shortcut cheat sheet ("?" or the header help button).
import { onBeforeUnmount, onMounted } from 'vue'

const emit = defineEmits<{ (e: 'close'): void }>()

const GROUPS: { title: string; items: { keys: string; label: string }[] }[] = [
  {
    title: 'Canvas',
    items: [
      { keys: 'Left-drag', label: 'Marquee select nodes' },
      { keys: 'Space + drag', label: 'Pan the canvas' },
      { keys: 'Scroll', label: 'Zoom' },
      { keys: 'Del', label: 'Delete selection (nodes / connection)' },
      { keys: 'Right-click', label: 'Node / connection menu' },
      { keys: 'Drag port', label: 'Connect nodes' },
    ],
  },
  {
    title: 'Editing',
    items: [
      { keys: 'Ctrl + S', label: 'Save workflow' },
      { keys: 'Ctrl + Z', label: 'Undo' },
      { keys: 'Ctrl + Shift + Z', label: 'Redo' },
      { keys: 'Ctrl + C / V', label: 'Copy / paste nodes' },
      { keys: 'Ctrl + D', label: 'Duplicate selection' },
      { keys: 'Ctrl + B', label: 'Toggle sidebar' },
    ],
  },
  {
    title: 'Platform',
    items: [
      { keys: 'Ctrl + K', label: 'Command palette' },
      { keys: '?', label: 'This cheat sheet' },
      { keys: 'Esc', label: 'Close dialogs' },
    ],
  },
]

function onKey(e: KeyboardEvent) {
  if (e.key === 'Escape') emit('close')
}
onMounted(() => window.addEventListener('keydown', onKey))
onBeforeUnmount(() => window.removeEventListener('keydown', onKey))
</script>

<template>
  <Teleport to="body">
    <div class="fixed inset-0 z-[70] flex items-center justify-center bg-black/60 backdrop-blur-sm" @click.self="emit('close')">
      <div class="max-h-[80vh] w-[560px] max-w-[92vw] overflow-y-auto rounded-2xl border border-zinc-700 bg-zinc-900 p-5 shadow-2xl">
        <div class="mb-4 flex items-center justify-between">
          <h2 class="text-sm font-semibold text-zinc-100">Keyboard &amp; mouse shortcuts</h2>
          <button class="rounded-lg p-1 text-zinc-500 transition hover:text-zinc-200" title="Close" @click="emit('close')">
            <svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6 6 18M6 6l12 12" /></svg>
          </button>
        </div>
        <div class="grid gap-5 sm:grid-cols-2">
          <div v-for="group in GROUPS" :key="group.title">
            <p class="mb-2 text-[10px] font-bold uppercase tracking-wider text-orange-400">{{ group.title }}</p>
            <div class="space-y-1.5">
              <div v-for="item in group.items" :key="item.keys" class="flex items-center justify-between gap-2">
                <span class="text-xs text-zinc-400">{{ item.label }}</span>
                <kbd class="rounded-md border border-zinc-700 bg-zinc-950 px-1.5 py-0.5 font-mono text-[10px] text-zinc-300">{{ item.keys }}</kbd>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
// v20: right-click context menu (nodes + edges on the canvas).
// Teleported to <body>, viewport-clamped, closes on click elsewhere / Esc /
// scroll. The page owns the item list so actions reuse editor functions.
import { computed, onBeforeUnmount, onMounted } from 'vue'

export interface ContextMenuItem {
  label: string
  hint?: string
  danger?: boolean
  disabled?: boolean
  action: () => void
}

const props = defineProps<{ x: number; y: number; items: ContextMenuItem[] }>()
const emit = defineEmits<{ (e: 'close'): void }>()

const style = computed(() => {
  const w = 208
  const h = props.items.length * 30 + 12
  const x = props.x + w > window.innerWidth ? Math.max(8, window.innerWidth - w - 8) : props.x
  const y = props.y + h > window.innerHeight ? Math.max(8, window.innerHeight - h - 8) : props.y
  return { left: `${x}px`, top: `${y}px` }
})

function run(item: ContextMenuItem) {
  if (item.disabled) return
  emit('close')
  item.action()
}

function onKey(e: KeyboardEvent) {
  if (e.key === 'Escape') emit('close')
}
function onLeftClick() {
  emit('close')
}
function onElsewhereContext(e: MouseEvent) {
  if (!(e.target as HTMLElement)?.closest?.('[data-context-menu]')) emit('close')
}
function onWheel() {
  emit('close')
}

onMounted(() => {
  window.addEventListener('keydown', onKey)
  // defer so the opening event itself can't immediately close the menu
  setTimeout(() => {
    window.addEventListener('click', onLeftClick)
    window.addEventListener('contextmenu', onElsewhereContext)
    window.addEventListener('wheel', onWheel, { passive: true })
  }, 0)
})
onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKey)
  window.removeEventListener('click', onLeftClick)
  window.removeEventListener('contextmenu', onElsewhereContext)
  window.removeEventListener('wheel', onWheel)
})
</script>

<template>
  <Teleport to="body">
    <div
      data-context-menu
      class="fixed z-[80] w-52 overflow-hidden rounded-xl border border-zinc-700 bg-zinc-900 py-1 shadow-2xl"
      :style="style"
      @contextmenu.prevent
    >
      <button
        v-for="(item, i) in items"
        :key="i"
        class="flex w-full items-center justify-between gap-2 px-3 py-1.5 text-left text-xs transition"
        :class="[
          item.disabled ? 'cursor-not-allowed text-zinc-600' : item.danger ? 'text-rose-300 hover:bg-rose-500/10' : 'text-zinc-200 hover:bg-zinc-800',
        ]"
        @click.stop="run(item)"
      >
        <span class="truncate">{{ item.label }}</span>
        <span v-if="item.hint" class="shrink-0 text-[10px] text-zinc-500">{{ item.hint }}</span>
      </button>
    </div>
  </Teleport>
</template>

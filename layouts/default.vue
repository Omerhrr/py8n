<script setup lang="ts">
import { Menu } from 'lucide-vue-next'
import { useSidebar } from '~/composables/useSidebar'

const route = useRoute()
const { mobileOpen, openMobile } = useSidebar()

// navigating from the mobile drawer should close it
watch(
  () => route.path,
  () => {
    mobileOpen.value = false
  },
)
</script>

<template>
  <div class="flex h-screen overflow-hidden bg-zinc-950 text-zinc-100">
    <AppSidebar />

    <div class="flex min-w-0 flex-1 flex-col">
      <!-- mobile top bar (sidebar is drawer-only below lg) -->
      <header class="flex h-12 shrink-0 items-center gap-3 border-b border-zinc-800/80 bg-zinc-950/90 px-3 lg:hidden">
        <button
          class="rounded-lg p-1.5 text-zinc-400 transition hover:bg-zinc-900 hover:text-zinc-100"
          title="Open menu"
          @click="openMobile()"
        >
          <Menu class="h-5 w-5" />
        </button>
        <span class="text-sm font-bold tracking-tight">Py8n</span>
      </header>

      <!-- pages scroll here; the workflow editor fills it exactly (h-full) -->
      <main class="min-h-0 flex-1 overflow-y-auto">
        <slot />
      </main>
    </div>

    <!-- global Ctrl+K command palette (v14) -->
    <CommandPalette />
  </div>
</template>

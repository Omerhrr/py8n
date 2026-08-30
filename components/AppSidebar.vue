<script setup lang="ts">
import {
  Zap, LayoutDashboard, Activity, BarChart3, CalendarClock, Sparkles, Plus,
  PanelLeftClose, PanelLeftOpen, X, KeyRound, Search, Variable,
} from 'lucide-vue-next'
import { useSidebar } from '~/composables/useSidebar'
import { usePalette } from '~/composables/usePalette'

const route = useRoute()
const { collapsed, mobileOpen, toggle, closeMobile } = useSidebar()
const { openPalette } = usePalette()

const nav = [
  { to: '/', label: 'Dashboard', icon: LayoutDashboard, match: ['/', '/workflows'] },
  { to: '/executions', label: 'Executions', icon: Activity, match: ['/executions'] },
  { to: '/insights', label: 'Insights', icon: BarChart3, match: ['/insights'] },
  { to: '/schedules', label: 'Schedules', icon: CalendarClock, match: ['/schedules'] },
  { to: '/templates', label: 'Templates', icon: Sparkles, match: ['/templates'] },
  { to: '/credentials', label: 'Credentials', icon: KeyRound, match: ['/credentials'] },
  { to: '/env-vars', label: 'Variables', icon: Variable, match: ['/env-vars'] },
]

function isActive(item: (typeof nav)[number]) {
  if (item.to === '/') return route.path === '/' || route.path.startsWith('/workflows')
  return route.path.startsWith(item.to)
}

function newWorkflow() {
  closeMobile()
  navigateTo('/?new=1')
}

// Ctrl/Cmd + B toggles the sidebar (VS Code muscle memory)
function onKey(e: KeyboardEvent) {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'b') {
    e.preventDefault()
    toggle()
  }
}

onMounted(() => window.addEventListener('keydown', onKey))
onBeforeUnmount(() => window.removeEventListener('keydown', onKey))
</script>

<template>
  <aside
    class="flex h-full w-60 shrink-0 flex-col border-r border-zinc-800/80 bg-zinc-950/95 transition-[width,transform] duration-200 ease-in-out max-lg:fixed max-lg:inset-y-0 max-lg:left-0 max-lg:z-40 max-lg:shadow-2xl"
    :class="[
      collapsed && 'lg:w-[68px]',
      mobileOpen ? 'max-lg:translate-x-0' : 'max-lg:-translate-x-full',
    ]"
  >
    <!-- brand -->
    <div class="flex h-14 shrink-0 items-center gap-2.5 border-b border-zinc-800/80 px-3">
      <NuxtLink
        to="/"
        class="flex min-w-0 flex-1 items-center gap-2.5"
        :class="collapsed && 'justify-center'"
        title="Py8n home"
      >
        <span class="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-gradient-to-br from-orange-500 to-rose-500 shadow-lg shadow-orange-500/20">
          <Zap class="h-4 w-4 text-white" />
        </span>
        <span class="min-w-0" :class="collapsed && 'lg:hidden'">
          <span class="block text-sm font-bold leading-tight tracking-tight">Py8n</span>
          <span class="block truncate text-[10px] leading-tight text-zinc-500">Workflow automation</span>
        </span>
      </NuxtLink>
      <button
        v-if="!collapsed"
        class="rounded-lg p-1.5 text-zinc-500 transition hover:bg-zinc-900 hover:text-zinc-200 max-lg:hidden"
        title="Collapse sidebar (Ctrl+B)"
        @click="toggle()"
      >
        <PanelLeftClose class="h-4 w-4" />
      </button>
      <button
        class="rounded-lg p-1.5 text-zinc-500 transition hover:bg-zinc-900 hover:text-zinc-200 lg:hidden"
        title="Close menu"
        @click="closeMobile()"
      >
        <X class="h-4 w-4" />
      </button>
    </div>

    <!-- new workflow -->
    <div class="px-3 pb-2 pt-3">
      <button
        class="flex w-full items-center gap-2 rounded-xl bg-orange-500 px-3 py-2 text-sm font-semibold text-white shadow-lg shadow-orange-500/20 transition hover:bg-orange-400 active:scale-[0.98]"
        :class="collapsed && 'lg:justify-center lg:bg-orange-500/15 lg:p-2.5 lg:text-orange-400 lg:shadow-none lg:hover:bg-orange-500/25'"
        :title="collapsed ? 'New workflow' : undefined"
        @click="newWorkflow"
      >
        <Plus class="h-4 w-4 shrink-0" />
        <span :class="collapsed && 'lg:hidden'">New workflow</span>
      </button>
    </div>

    <!-- nav -->
    <nav class="mt-1 flex-1 space-y-1 px-3 pb-3">
      <NuxtLink
        v-for="item in nav"
        :key="item.to"
        :to="item.to"
        class="group relative flex items-center gap-2.5 rounded-xl px-3 py-2 text-sm font-medium transition"
        :class="[
          collapsed && 'lg:justify-center lg:px-0 lg:py-2.5',
          isActive(item)
            ? 'bg-orange-500/10 text-orange-400'
            : 'text-zinc-400 hover:bg-zinc-900 hover:text-zinc-100',
        ]"
        :title="collapsed ? item.label : undefined"
        @click="closeMobile()"
      >
        <!-- active indicator bar -->
        <span
          v-if="isActive(item)"
          class="absolute left-0 top-1/2 h-5 w-0.5 -translate-y-1/2 rounded-full bg-orange-500 max-lg:left-1"
        />
        <component :is="item.icon" class="h-4 w-4 shrink-0" />
        <span class="truncate" :class="collapsed && 'lg:hidden'">{{ item.label }}</span>
        <!-- hover tooltip in collapsed mode (desktop only) -->
        <span
          v-if="collapsed"
          class="pointer-events-none absolute left-full top-1/2 z-50 ml-2 hidden -translate-y-1/2 whitespace-nowrap rounded-lg border border-zinc-800 bg-zinc-900 px-2.5 py-1.5 text-xs font-medium text-zinc-200 opacity-0 shadow-xl transition group-hover:opacity-100 lg:block"
        >
          {{ item.label }}
        </span>
      </NuxtLink>
    </nav>

    <!-- footer -->
    <div class="shrink-0 border-t border-zinc-800/80 p-3">
      <!-- v14: quick-search trigger (Ctrl/Cmd+K) -->
      <button
        class="mb-2 flex w-full items-center gap-2 rounded-xl border border-zinc-800 bg-zinc-900/60 px-2.5 py-2 text-xs text-zinc-400 transition hover:border-zinc-600 hover:text-zinc-200"
        :class="collapsed && 'lg:justify-center lg:px-0'"
        :title="collapsed ? 'Quick search (Ctrl+K)' : 'Quick search (Ctrl+K)'"
        @click="openPalette()"
      >
        <Search class="h-3.5 w-3.5 shrink-0" />
        <span class="flex-1 text-left" :class="collapsed && 'lg:hidden'">Quick search</span>
        <kbd
          class="rounded border border-zinc-700 bg-zinc-950 px-1 py-0.5 text-[9px] font-sans text-zinc-500"
          :class="collapsed && 'lg:hidden'"
        >⌘K</kbd>
      </button>
      <div class="flex items-center justify-between gap-2 text-[10px] text-zinc-600" :class="collapsed && 'lg:justify-center'">
        <span class="truncate" :class="collapsed && 'lg:hidden'">v1.21 · 21 node types</span>
        <button
          v-if="!collapsed"
          class="rounded-lg p-1 text-zinc-600 transition hover:bg-zinc-900 hover:text-zinc-300 max-lg:hidden"
          title="Expand sidebar (Ctrl+B)"
          @click="toggle()"
        >
          <PanelLeftOpen class="h-3.5 w-3.5" />
        </button>
      </div>
      <button
        v-if="collapsed"
        class="mt-1 hidden w-full justify-center rounded-lg p-1 text-zinc-600 transition hover:bg-zinc-900 hover:text-zinc-300 lg:flex"
        title="Expand sidebar (Ctrl+B)"
        @click="toggle()"
      >
        <PanelLeftOpen class="h-3.5 w-3.5" />
      </button>
    </div>
  </aside>

  <!-- mobile backdrop -->
  <Teleport to="body">
    <Transition name="fade">
      <div
        v-if="mobileOpen"
        class="fixed inset-0 z-30 bg-black/60 backdrop-blur-sm lg:hidden"
        @click="closeMobile()"
      />
    </Transition>
  </Teleport>
</template>

<style scoped>
.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.2s ease;
}
.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}
</style>

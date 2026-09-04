<script setup lang="ts">
import {
  LayoutDashboard, Activity, BarChart3, CalendarClock, Sparkles, Plus,
  PanelLeftClose, PanelLeftOpen, X, KeyRound, Search, Variable, Database, Image as ImageIcon,
  LayoutGrid, Gauge, FileText, Bot, LogOut, KeySquare, CloudDownload, BellRing, Network,
  FileBarChart, BookOpen, Radio, Wand2, Store, Boxes, Trash2, Unlink, BrainCircuit,
  Globe, Rocket,
} from 'lucide-vue-next'
import { useSidebar } from '~/composables/useSidebar'
import { usePalette } from '~/composables/usePalette'

const route = useRoute()
const { collapsed, mobileOpen, toggle, closeMobile } = useSidebar()
const { openPalette } = usePalette()
const auth = useAuthStore()

onMounted(() => auth.boot())

const userInitial = computed(() => (auth.user?.email || '?').slice(0, 1).toUpperCase())
const userName = computed(() => auth.user?.name || auth.user?.email || '')

async function signOut() {
  await auth.logout()
  if (auth.requireAuth) navigateTo('/login')
}

const nav = [
  { to: '/', label: 'Dashboard', icon: LayoutDashboard, match: ['/', '/workflows'] },
  { to: '/platform', label: 'Platform', icon: Globe, match: ['/platform'] },  // v67 the five verbs
  { to: '/executions', label: 'Executions', icon: Activity, match: ['/executions'] },
  { to: '/insights', label: 'Insights', icon: BarChart3, match: ['/insights'] },
  { to: '/schedules', label: 'Schedules', icon: CalendarClock, match: ['/schedules'] },
  { to: '/reports', label: 'Reports', icon: FileBarChart, match: ['/reports'] },  // v48 scheduled exports
  { to: '/templates', label: 'Templates', icon: Sparkles, match: ['/templates'] },
  { to: '/marketplace', label: 'Marketplace', icon: Store, match: ['/marketplace'] },  // v60 solution marketplace
  { to: '/systems', label: 'Systems', icon: Boxes, match: ['/systems'] },  // v61 py8n systems
  { to: '/agents', label: 'Agents', icon: Bot, match: ['/agents'] },
  { to: '/datasets', label: 'Datasets', icon: Database, match: ['/datasets'] },
  { to: '/catalog', label: 'Catalog', icon: BookOpen, match: ['/catalog'] },  // v50 data catalog
  { to: '/observability', label: 'Observability', icon: Radio, match: ['/observability'] },  // v53 data observability
  { to: '/builder', label: 'System Builder', icon: Wand2, match: ['/builder'] },  // v59 AI system builder
  { to: '/models', label: 'Models', icon: Network, match: ['/models'] },  // v46 model registry
  { to: '/model-systems', label: 'Model Systems', icon: BrainCircuit, match: ['/model-systems'] },  // v63 model-building units
  { to: '/deployments', label: 'Deployments', icon: Rocket, match: ['/deployments'] },  // v67 live model endpoints
  { to: '/documents', label: 'Documents', icon: FileText, match: ['/documents'] },
  { to: '/apps', label: 'Apps', icon: LayoutGrid, match: ['/apps'] },
  { to: '/dashboards', label: 'Dashboards', icon: Gauge, match: ['/dashboards'] },
  { to: '/artifacts', label: 'Artifacts', icon: ImageIcon, match: ['/artifacts'] },
  { to: '/credentials', label: 'Credentials', icon: KeyRound, match: ['/credentials'] },
  { to: '/env-vars', label: 'Variables', icon: Variable, match: ['/env-vars'] },
  { to: '/keys', label: 'API keys', icon: KeySquare, match: ['/keys'] },  // v41
  { to: '/registries', label: 'Registries', icon: CloudDownload, match: ['/registries'] },  // v43
  { to: '/notifications', label: 'Notifications', icon: BellRing, match: ['/notifications'] },  // v44
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
        <Py8nLogo :size="32" />
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

    <!-- expand toggle - lives at the TOP of the sidebar in collapsed mode -->
    <div v-if="collapsed" class="hidden justify-center pb-1 lg:flex">
      <button
        class="rounded-lg p-1.5 text-zinc-500 transition hover:bg-zinc-900 hover:text-zinc-200"
        title="Expand sidebar (Ctrl+B)"
        @click="toggle()"
      >
        <PanelLeftOpen class="h-4 w-4" />
      </button>
    </div>

    <!-- nav (scrolls when it outgrows the viewport) -->
    <nav class="sidebar-scroll mt-1 min-h-0 flex-1 space-y-1 overflow-y-auto px-3 pb-3">
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
      <!-- v37: signed-in user chip + logout -->
      <div
        v-if="auth.user"
        class="mb-2 flex items-center gap-2 rounded-xl border border-zinc-800 bg-zinc-900/60 px-2.5 py-2"
        :class="collapsed && 'lg:justify-center lg:px-0'"
        :title="userName"
      >
        <span class="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-orange-500/15 text-[10px] font-bold text-orange-400">{{ userInitial }}</span>
        <span class="min-w-0 flex-1" :class="collapsed && 'lg:hidden'">
          <span class="block truncate text-xs font-medium text-zinc-300">{{ userName }}</span>
          <span class="block text-[10px] leading-tight text-zinc-600">{{ auth.user.role }}</span>
        </span>
        <button
          class="shrink-0 rounded-lg p-1 text-zinc-500 transition hover:bg-zinc-800 hover:text-rose-400"
          title="Sign out"
          @click="signOut"
        >
          <LogOut class="h-3.5 w-3.5" />
        </button>
      </div>
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
        <span class="truncate" :class="collapsed && 'lg:hidden'">v1.49 · 47 node types</span>
      </div>
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

/* slim dark scrollbar for the nav rail */
.sidebar-scroll {
  scrollbar-width: thin;
  scrollbar-color: rgb(63 63 70 / 0.6) transparent;
}
.sidebar-scroll::-webkit-scrollbar {
  width: 6px;
}
.sidebar-scroll::-webkit-scrollbar-track {
  background: transparent;
}
.sidebar-scroll::-webkit-scrollbar-thumb {
  background: rgb(63 63 70 / 0.6);
  border-radius: 3px;
}
.sidebar-scroll::-webkit-scrollbar-thumb:hover {
  background: rgb(82 82 91 / 0.8);
}
</style>

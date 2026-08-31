<script setup lang="ts">
import {
  Search, CornerDownLeft, LayoutDashboard, Activity, BarChart3, CalendarClock,
  Sparkles, KeyRound, Plus, PanelLeftClose, PanelLeftOpen, Command as CommandIcon,
  Workflow as WorkflowIcon, Zap, X, Variable, Database, Image as ImageIcon, LayoutGrid, Gauge, FileText, Bot,
} from 'lucide-vue-next'
import { usePalette } from '~/composables/usePalette'
import { useSidebar } from '~/composables/useSidebar'
import type { WorkflowListItem } from '~/types/node'

const { open, closePalette, togglePalette } = usePalette()
const { collapsed, toggle: toggleSidebar } = useSidebar()
const store = usePy8nStore()

const query = ref('')
const inputEl = ref<HTMLInputElement | null>(null)
const listEl = ref<HTMLElement | null>(null)
const selected = ref(0)

interface PaletteItem {
  id: string
  group: 'Actions' | 'Navigate' | 'Workflows'
  label: string
  hint?: string
  icon: any
  keywords?: string
  tags?: string[]
  wf?: WorkflowListItem
  run: () => void
}

// ------------------------------------------------------------------ items
function go(path: string) {
  navigateTo(path)
}

const actionItems = computed<PaletteItem[]>(() => [
  {
    id: 'act-new', group: 'Actions', label: 'New workflow', hint: 'Create from scratch',
    icon: Plus, keywords: 'create add build start',
    run: () => go('/?new=1'),
  },
  {
    id: 'act-sidebar', group: 'Actions',
    label: collapsed.value ? 'Expand sidebar' : 'Collapse sidebar',
    hint: 'Ctrl+B', icon: collapsed.value ? PanelLeftOpen : PanelLeftClose,
    keywords: 'toggle rail panel hide show menu',
    run: () => toggleSidebar(),
  },
])

const navItems = computed<PaletteItem[]>(() => [
  { id: 'nav-dashboard', group: 'Navigate', label: 'Dashboard', icon: LayoutDashboard, hint: 'Workflows overview', run: () => go('/') },
  { id: 'nav-executions', group: 'Navigate', label: 'Executions', icon: Activity, hint: 'Run history', keywords: 'runs logs history', run: () => go('/executions') },
  { id: 'nav-insights', group: 'Navigate', label: 'Insights', icon: BarChart3, hint: 'Analytics & trends', keywords: 'stats analytics charts', run: () => go('/insights') },
  { id: 'nav-schedules', group: 'Navigate', label: 'Schedules', icon: CalendarClock, hint: 'Cron & intervals', keywords: 'cron timer triggers', run: () => go('/schedules') },
  { id: 'nav-templates', group: 'Navigate', label: 'Templates', icon: Sparkles, hint: 'Ready-made workflows', keywords: 'gallery presets examples', run: () => go('/templates') },
  { id: 'nav-agents', group: 'Navigate', label: 'Agents', icon: Bot, hint: 'Agent console & playground', keywords: 'ai agent bot tools playground chat llm assistant', run: () => go('/agents') },
  { id: 'nav-datasets', group: 'Navigate', label: 'Datasets', icon: Database, hint: 'Stored tables & SQL', keywords: 'data excel csv parquet tables sql upload', run: () => go('/datasets') },
  { id: 'nav-documents', group: 'Navigate', label: 'Document AI', icon: FileText, hint: 'Extract text & tables from PDFs and scans', keywords: 'document pdf ocr scan extract invoice text tables word docx', run: () => go('/documents') },
  { id: 'nav-apps', group: 'Navigate', label: 'Apps', icon: LayoutGrid, hint: 'Excel → App builder & published apps', keywords: 'app builder excel crm dashboard form run publish', run: () => go('/apps') },
  { id: 'nav-dashboards', group: 'Navigate', label: 'Dashboards', icon: Gauge, hint: 'KPI boards over many datasets', keywords: 'dashboard kpi analytics board charts metrics wall publish', run: () => go('/dashboards') },
  { id: 'nav-artifacts', group: 'Navigate', label: 'Artifacts', icon: ImageIcon, hint: 'Charts & models from runs', keywords: 'charts images models gallery png outputs', run: () => go('/artifacts') },
  { id: 'nav-credentials', group: 'Navigate', label: 'Credentials', icon: KeyRound, hint: 'Vault & auth', keywords: 'secrets keys api tokens', run: () => go('/credentials') },
  { id: 'nav-env-vars', group: 'Navigate', label: 'Variables', icon: Variable, hint: 'Global env values', keywords: 'env environment globals config values', run: () => go('/env-vars') },
])

const workflowItems = computed<PaletteItem[]>(() =>
  store.workflows.map((wf) => ({
    id: `wf-${wf.id}`,
    group: 'Workflows' as const,
    label: wf.name,
    hint: wf.folder_name ? `${wf.folder_name} · ${wf.node_count} nodes` : `${wf.node_count} nodes`,
    icon: WorkflowIcon,
    keywords: (wf.description || '') + ' ' + (wf.folder_name || '') + ' ' + (wf.trigger_types || []).join(' '),
    tags: wf.tags || [],
    wf,
    run: () => go(`/workflows/${wf.id}`),
  })),
)

// ------------------------------------------------------------------ filtering
const q = computed(() => query.value.trim().toLowerCase())

function matches(item: PaletteItem) {
  if (!q.value) return true
  const haystack = `${item.label} ${item.keywords || ''} ${(item.tags || []).join(' ')}`.toLowerCase()
  // every whitespace-separated token must appear somewhere (AND search)
  return q.value.split(/\s+/).every((tok) => haystack.includes(tok))
}

const GROUP_ORDER = ['Actions', 'Navigate', 'Workflows'] as const

const filteredGroups = computed(() =>
  GROUP_ORDER.map((g) => {
    const source = g === 'Actions' ? actionItems.value : g === 'Navigate' ? navItems.value : workflowItems.value
    return { group: g, items: source.filter(matches) }
  }).filter((g) => g.items.length),
)

const flatItems = computed(() => filteredGroups.value.flatMap((g) => g.items))

watch([query, () => store.workflows.length], () => {
  selected.value = 0
})

// ------------------------------------------------------------------ open/close lifecycle
watch(open, async (o) => {
  if (o) {
    query.value = ''
    selected.value = 0
    // workflows may not be loaded yet (e.g. palette opened from the editor)
    if (!store.workflows.length) store.loadWorkflows().catch(() => {})
    document.body.style.overflow = 'hidden'
    await nextTick()
    inputEl.value?.focus()
  } else {
    document.body.style.overflow = ''
  }
})

onBeforeUnmount(() => {
  if (open.value) document.body.style.overflow = ''
})

// Ctrl/Cmd + K anywhere toggles the palette (VS Code / Slack muscle memory)
function onGlobalKey(e: KeyboardEvent) {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
    e.preventDefault()
    e.stopPropagation()
    togglePalette()
  }
}
onMounted(() => window.addEventListener('keydown', onGlobalKey, true))
onBeforeUnmount(() => window.removeEventListener('keydown', onGlobalKey, true))

// ------------------------------------------------------------------ keyboard nav inside the palette
function onInputKey(e: KeyboardEvent) {
  const n = flatItems.value.length
  if (e.key === 'ArrowDown') {
    e.preventDefault()
    if (n) selected.value = (selected.value + 1) % n
  } else if (e.key === 'ArrowUp') {
    e.preventDefault()
    if (n) selected.value = (selected.value - 1 + n) % n
  } else if (e.key === 'Enter') {
    e.preventDefault()
    const item = flatItems.value[selected.value]
    if (item) execute(item)
  } else if (e.key === 'Escape') {
    e.preventDefault()
    closePalette()
  }
}

function execute(item: PaletteItem) {
  closePalette()
  item.run()
}

// keep the highlighted row visible while arrowing through long workflow lists
watch(selected, async () => {
  await nextTick()
  listEl.value
    ?.querySelector(`[data-idx="${selected.value}"]`)
    ?.scrollIntoView({ block: 'nearest' })
})

// ------------------------------------------------------------------ match highlight
function chunks(label: string): { text: string; hit: boolean }[] {
  if (!q.value) return [{ text: label, hit: false }]
  const lower = label.toLowerCase()
  const idx = lower.indexOf(q.value)
  if (idx === -1) return [{ text: label, hit: false }]
  const out: { text: string; hit: boolean }[] = []
  if (idx > 0) out.push({ text: label.slice(0, idx), hit: false })
  out.push({ text: label.slice(idx, idx + q.value.length), hit: true })
  if (idx + q.value.length < label.length) out.push({ text: label.slice(idx + q.value.length), hit: false })
  return out
}

// deterministic tag chip color (matches the dashboard palette)
function tagColor(tag: string) {
  let h = 0
  for (let i = 0; i < tag.length; i++) h = (h * 31 + tag.charCodeAt(i)) >>> 0
  const palette = [
    'border-orange-500/30 text-orange-300',
    'border-sky-500/30 text-sky-300',
    'border-emerald-500/30 text-emerald-300',
    'border-violet-500/30 text-violet-300',
    'border-amber-500/30 text-amber-300',
    'border-rose-500/30 text-rose-300',
  ]
  return palette[h % palette.length]
}

const isMac = import.meta.client && /Mac|iPhone|iPad/.test(navigator.platform || '')
const modKey = computed(() => (isMac ? '⌘' : 'Ctrl'))
</script>

<template>
  <Teleport to="body">
    <Transition name="palette">
      <div
        v-if="open"
        class="fixed inset-0 z-[70] flex items-start justify-center bg-black/70 p-4 pt-[10vh] backdrop-blur-sm sm:pt-[14vh]"
        @click.self="closePalette()"
      >
        <div
          class="flex max-h-[70vh] w-full max-w-xl flex-col overflow-hidden rounded-2xl border border-zinc-700/80 bg-zinc-900 shadow-2xl shadow-black/60"
          role="dialog"
          aria-label="Command palette"
        >
          <!-- search input -->
          <div class="flex shrink-0 items-center gap-3 border-b border-zinc-800 px-4">
            <Search class="h-4 w-4 shrink-0 text-zinc-500" />
            <input
              ref="inputEl"
              v-model="query"
              class="h-12 w-full bg-transparent text-sm text-zinc-100 outline-none placeholder:text-zinc-600"
              placeholder="Search workflows, jump to a page, run a command…"
              spellcheck="false"
              @keydown="onInputKey"
            />
            <button
              v-if="query"
              class="rounded p-1 text-zinc-500 transition hover:text-zinc-200"
              title="Clear"
              @click="query = ''"
            >
              <X class="h-3.5 w-3.5" />
            </button>
          </div>

          <!-- results -->
          <div ref="listEl" class="min-h-0 flex-1 overflow-y-auto p-2">
            <div v-if="!flatItems.length" class="px-3 py-10 text-center">
              <CommandIcon class="mx-auto mb-2 h-6 w-6 text-zinc-600" />
              <p class="text-sm text-zinc-500">No matches for “{{ query }}”</p>
              <p class="mt-1 text-xs text-zinc-600">Try a workflow name, a tag, or “insights”.</p>
            </div>

            <div v-for="g in filteredGroups" :key="g.group" class="mb-1 last:mb-0">
              <p class="px-3 pb-1 pt-2 text-[10px] font-semibold uppercase tracking-wider text-zinc-600">
                {{ g.group }}
              </p>
              <template v-for="item in g.items" :key="item.id">
                <button
                  class="flex w-full items-center gap-3 rounded-xl px-3 py-2 text-left transition"
                  :class="flatItems[selected]?.id === item.id
                    ? 'bg-orange-500/15 text-zinc-100'
                    : 'text-zinc-300 hover:bg-zinc-800/70'"
                  :data-idx="flatItems.indexOf(item)"
                  @mousemove="selected = flatItems.indexOf(item)"
                  @click="execute(item)"
                >
                  <span
                    class="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg border"
                    :class="flatItems[selected]?.id === item.id
                      ? 'border-orange-500/40 bg-orange-500/10 text-orange-400'
                      : 'border-zinc-800 bg-zinc-950/60 text-zinc-400'"
                  >
                    <component :is="item.icon" class="h-3.5 w-3.5" />
                  </span>

                  <span class="min-w-0 flex-1">
                    <span class="block truncate text-sm font-medium">
                      <template v-for="(c, i) in chunks(item.label)" :key="i">
                        <span v-if="c.hit" class="rounded bg-orange-500/25 text-orange-300">{{ c.text }}</span>
                        <template v-else>{{ c.text }}</template>
                      </template>
                    </span>
                    <span v-if="item.hint || item.wf" class="block truncate text-[11px] text-zinc-600">
                      {{ item.hint }}
                    </span>
                  </span>

                  <!-- workflow meta: tags + active state -->
                  <span v-if="item.wf" class="flex shrink-0 items-center gap-1">
                    <span
                      v-for="tag in (item.tags || []).slice(0, 2)"
                      :key="tag"
                      class="rounded-md border px-1.5 py-0.5 text-[10px]"
                      :class="tagColor(tag)"
                    >{{ tag }}</span>
                    <span
                      class="ml-1 h-1.5 w-1.5 rounded-full"
                      :class="item.wf.is_active ? 'bg-emerald-400' : 'bg-zinc-600'"
                      :title="item.wf.is_active ? 'Active' : 'Paused'"
                    />
                  </span>

                  <CornerDownLeft
                    v-if="flatItems[selected]?.id === item.id"
                    class="h-3.5 w-3.5 shrink-0 text-orange-400/70"
                  />
                </button>
              </template>
            </div>
          </div>

          <!-- footer hints -->
          <div class="flex shrink-0 items-center justify-between border-t border-zinc-800 px-4 py-2 text-[10px] text-zinc-600">
            <span class="flex items-center gap-3">
              <span><kbd class="rounded border border-zinc-700 bg-zinc-950 px-1 py-0.5 font-sans">↑↓</kbd> navigate</span>
              <span><kbd class="rounded border border-zinc-700 bg-zinc-950 px-1 py-0.5 font-sans">↵</kbd> open</span>
              <span><kbd class="rounded border border-zinc-700 bg-zinc-950 px-1 py-0.5 font-sans">esc</kbd> close</span>
            </span>
            <span class="flex items-center gap-1 text-zinc-600">
              <Zap class="h-3 w-3 text-orange-500/60" /> Py8n
            </span>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.palette-enter-active,
.palette-leave-active {
  transition: opacity 0.15s ease;
}
.palette-enter-active > div,
.palette-leave-active > div {
  transition: transform 0.15s ease, opacity 0.15s ease;
}
.palette-enter-from,
.palette-leave-to {
  opacity: 0;
}
.palette-enter-from > div,
.palette-leave-to > div {
  transform: translateY(-8px) scale(0.98);
  opacity: 0;
}
</style>

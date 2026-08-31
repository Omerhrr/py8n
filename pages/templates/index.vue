<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount, watch } from 'vue'
import {
  Sparkles, Search, Loader2, Workflow as WorkflowIcon, X, Check,
  Brain, PauseCircle, Repeat, GitBranch, Globe, Slack, Sigma, Mail,
  FileSearch, Activity, Bot, Reply, Siren, MessageSquare, Table2, Inbox, LayoutTemplate,
} from 'lucide-vue-next'
import { useApi } from '~/composables/useApi'
import type { Workflow, WorkflowTemplate } from '~/types/node'

const { api } = useApi()

const ICONS: Record<string, unknown> = {
  brain: Brain, 'pause-circle': PauseCircle, repeat: Repeat, 'git-branch': GitBranch,
  globe: Globe, slack: Slack, sigma: Sigma, mail: Mail, 'file-search': FileSearch,
  activity: Activity, bot: Bot, reply: Reply, siren: Siren, 'message-square': MessageSquare,
  'table-2': Table2, inbox: Inbox,
}
function iconFor(key: string) {
  return ICONS[key] ?? LayoutTemplate
}

const templates = ref<WorkflowTemplate[]>([])
const loading = ref(true)
const search = ref('')
const category = ref('')

// install modal state
const installTarget = ref<WorkflowTemplate | null>(null)
const installName = ref('')
const installing = ref(false)
const installError = ref('')

const categories = computed(() => {
  const counts = new Map<string, number>()
  for (const t of templates.value) counts.set(t.category, (counts.get(t.category) || 0) + 1)
  return [...counts.entries()].map(([name, n]) => ({ name, n })).sort((a, b) => b.n - a.n)
})

const filtered = computed(() => {
  const q = search.value.trim().toLowerCase()
  return templates.value.filter((t) => {
    if (category.value && t.category !== category.value) return false
    if (!q) return true
    const hay = [t.name, t.description, t.category, ...(t.tags ?? [])].join(' ').toLowerCase()
    return hay.includes(q)
  })
})

function openInstall(tpl: WorkflowTemplate) {
  installTarget.value = tpl
  installName.value = tpl.name
  installError.value = ''
}
function closeInstall() {
  if (installing.value) return
  installTarget.value = null
}

async function confirmInstall() {
  if (!installTarget.value) return
  installing.value = true
  installError.value = ''
  try {
    const name = installName.value.trim()
    const wf = await api.post<Workflow>(`/templates/${installTarget.value.id}/use`, name ? { name } : undefined)
    installTarget.value = null
    navigateTo(`/workflows/${wf.id}`)
  } catch (e: unknown) {
    installError.value = e instanceof Error ? e.message : 'Install failed — try again.'
  } finally {
    installing.value = false
  }
}

function onKey(e: KeyboardEvent) {
  if (e.key === 'Escape' && installTarget.value) closeInstall()
}
onMounted(async () => {
  window.addEventListener('keydown', onKey)
  try {
    templates.value = await api.get<WorkflowTemplate[]>('/templates')
  } finally {
    loading.value = false
  }
})
onBeforeUnmount(() => window.removeEventListener('keydown', onKey))

watch(installTarget, (t) => {
  if (t) installName.value = t.name
})
</script>

<template>
  <div class="text-zinc-100">
    <!-- page header -->
    <header class="sticky top-0 z-20 border-b border-zinc-800/80 bg-zinc-950/90 backdrop-blur">
      <div class="mx-auto flex max-w-7xl flex-wrap items-center justify-between gap-3 px-4 py-3.5 sm:px-6">
        <div class="flex items-center gap-3">
          <div class="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-orange-500 to-rose-500 shadow-lg shadow-orange-500/20">
            <Sparkles class="h-4 w-4 text-white" />
          </div>
          <div>
            <h1 class="text-lg font-bold tracking-tight">Readymade Automations</h1>
            <p class="-mt-0.5 text-[11px] text-zinc-500">{{ templates.length }} gallery-tested blueprints — install, tweak, run</p>
          </div>
        </div>
        <label class="relative">
          <Search class="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-zinc-500" />
          <input
            v-model="search"
            class="w-56 rounded-xl border border-zinc-800 bg-zinc-900 py-2 pl-9 pr-3 text-sm outline-none transition placeholder:text-zinc-600 focus:border-orange-500/60"
            placeholder="Search gallery…"
          />
        </label>
      </div>
    </header>

    <main class="mx-auto max-w-7xl px-4 py-6 sm:px-6">
      <!-- category chips -->
      <section class="mb-5 flex flex-wrap items-center gap-2">
        <button
          class="rounded-full border px-3 py-1.5 text-xs font-semibold transition"
          :class="!category
            ? 'border-orange-500/50 bg-orange-500/10 text-orange-400'
            : 'border-zinc-800 text-zinc-400 hover:border-zinc-600 hover:text-zinc-200'"
          @click="category = ''"
        >
          All <span class="ml-1 text-[10px] opacity-70">{{ templates.length }}</span>
        </button>
        <button
          v-for="c in categories"
          :key="c.name"
          class="rounded-full border px-3 py-1.5 text-xs font-semibold transition"
          :class="category === c.name
            ? 'border-orange-500/50 bg-orange-500/10 text-orange-400'
            : 'border-zinc-800 text-zinc-400 hover:border-zinc-600 hover:text-zinc-200'"
          @click="category = c.name"
        >
          {{ c.name }} <span class="ml-1 text-[10px] opacity-70">{{ c.n }}</span>
        </button>
      </section>

      <!-- loading skeleton -->
      <div v-if="loading" class="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        <div v-for="i in 6" :key="i" class="h-44 animate-pulse rounded-2xl border border-zinc-800 bg-zinc-900/50" />
      </div>

      <!-- empty / no match -->
      <div
        v-else-if="filtered.length === 0"
        class="rounded-2xl border border-dashed border-zinc-800 p-12 text-center"
      >
        <Search class="mx-auto mb-3 h-8 w-8 text-zinc-600" />
        <p class="font-medium text-zinc-300">{{ search || category ? 'No automations match your filters.' : 'No automations available.' }}</p>
        <p class="mx-auto mt-2 max-w-md text-sm leading-relaxed text-zinc-500">
          Try a different keyword or category — every template validates against the engine and runs offline-safe unless it calls live integrations.
        </p>
      </div>

      <!-- gallery cards -->
      <div v-else class="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        <article
          v-for="tpl in filtered"
          :key="tpl.id"
          class="group flex flex-col overflow-hidden rounded-2xl border border-zinc-800 bg-zinc-900/40 transition hover:-translate-y-0.5 hover:border-zinc-600 hover:bg-zinc-900/70 hover:shadow-xl hover:shadow-black/30"
        >
          <!-- accent header band -->
          <div
            class="flex items-center gap-3 border-b border-zinc-800/60 px-5 py-4"
            :style="{ background: `linear-gradient(135deg, ${(tpl.accent || '#f97316')}26, transparent 70%)` }"
          >
            <span
              class="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl shadow-lg"
              :style="{ background: `linear-gradient(135deg, ${tpl.accent || '#f97316'}, ${(tpl.accent || '#f97316')}99)`, boxShadow: `0 8px 20px -6px ${(tpl.accent || '#f97316')}66` }"
            >
              <component :is="iconFor(tpl.icon)" class="h-5 w-5 text-zinc-950" />
            </span>
            <div class="min-w-0 flex-1">
              <h3 class="truncate font-semibold leading-snug">{{ tpl.name }}</h3>
              <p class="text-[10px] font-bold uppercase tracking-widest" :style="{ color: tpl.accent || '#f97316' }">{{ tpl.category }}</p>
            </div>
            <span
              v-if="tpl.badge"
              class="shrink-0 rounded-full border px-2 py-0.5 text-[10px] font-bold"
              :style="{ borderColor: `${tpl.accent}55`, color: tpl.accent, background: `${tpl.accent}14` }"
            >{{ tpl.badge }}</span>
          </div>

          <div class="flex flex-1 flex-col px-5 pb-4 pt-3">
            <p class="mb-3 text-xs leading-relaxed text-zinc-400">{{ tpl.description }}</p>
            <!-- node type chips -->
            <div class="mb-4 flex flex-wrap gap-1">
              <span
                v-for="nt in (tpl.node_types || []).slice(0, 4)"
                :key="nt"
                class="rounded-md border border-zinc-800 bg-zinc-950/80 px-1.5 py-0.5 font-mono text-[9px] text-zinc-500"
              >{{ nt }}</span>
              <span
                v-if="(tpl.node_types || []).length > 4"
                class="rounded-md border border-zinc-800 bg-zinc-950/80 px-1.5 py-0.5 text-[9px] text-zinc-500"
              >+{{ tpl.node_types.length - 4 }}</span>
            </div>
            <div class="mt-auto flex items-center justify-between border-t border-zinc-800/70 pt-3">
              <span class="inline-flex items-center gap-1.5 text-[11px] text-zinc-600">
                <WorkflowIcon class="h-3.5 w-3.5" /> {{ tpl.node_count }} steps · validated
              </span>
              <button
                class="inline-flex items-center gap-1.5 rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-1.5 text-xs font-semibold text-zinc-200 transition hover:border-orange-500/60 hover:bg-orange-500/10 hover:text-white disabled:opacity-50"
                @click="openInstall(tpl)"
              >
                <Check class="h-3.5 w-3.5" />
                Install
              </button>
            </div>
          </div>
        </article>
      </div>
    </main>

    <!-- install modal -->
    <Teleport to="body">
      <Transition name="fade">
        <div
          v-if="installTarget"
          class="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4 backdrop-blur-sm"
          @click.self="closeInstall()"
        >
          <div class="w-full max-w-lg rounded-2xl border border-zinc-800 bg-zinc-950 shadow-2xl">
            <!-- modal header -->
            <div
              class="flex items-center gap-3 border-b border-zinc-800/80 px-5 py-4"
              :style="{ background: `linear-gradient(135deg, ${(installTarget.accent || '#f97316')}1f, transparent 70%)` }"
            >
              <span
                class="flex h-9 w-9 items-center justify-center rounded-xl"
                :style="{ background: `linear-gradient(135deg, ${installTarget.accent || '#f97316'}, ${(installTarget.accent || '#f97316')}99)` }"
              >
                <component :is="iconFor(installTarget.icon)" class="h-4.5 w-4.5 text-zinc-950" />
              </span>
              <div class="min-w-0 flex-1">
                <h3 class="truncate font-semibold">Install automation</h3>
                <p class="truncate text-[11px] text-zinc-500">{{ installTarget.name }}</p>
              </div>
              <button
                class="rounded-lg p-1.5 text-zinc-500 transition hover:bg-zinc-900 hover:text-zinc-200"
                title="Close"
                @click="closeInstall()"
              >
                <X class="h-4 w-4" />
              </button>
            </div>

            <div class="space-y-4 px-5 py-4">
              <div>
                <label class="mb-1.5 block text-[11px] font-semibold uppercase tracking-wider text-zinc-500">Workflow name</label>
                <input
                  v-model="installName"
                  class="w-full rounded-xl border border-zinc-800 bg-zinc-900 px-3 py-2.5 text-sm outline-none transition placeholder:text-zinc-600 focus:border-orange-500/60"
                  placeholder="My automation"
                  @keydown.enter="confirmInstall()"
                />
              </div>

              <div>
                <p class="mb-1.5 text-[11px] font-semibold uppercase tracking-wider text-zinc-500">How it works</p>
                <p class="max-h-24 overflow-y-auto text-xs leading-relaxed text-zinc-400">{{ installTarget.docs }}</p>
              </div>

              <div>
                <p class="mb-1.5 text-[11px] font-semibold uppercase tracking-wider text-zinc-500">Steps</p>
                <ol class="space-y-1.5">
                  <li
                    v-for="(nt, i) in installTarget.node_types"
                    :key="i"
                    class="flex items-center gap-2 text-xs text-zinc-400"
                  >
                    <span
                      class="flex h-5 w-5 shrink-0 items-center justify-center rounded-md text-[10px] font-bold"
                      :style="{ background: `${installTarget.accent || '#f97316'}1f`, color: installTarget.accent || '#f97316' }"
                    >{{ i + 1 }}</span>
                    <span class="font-mono text-[10px] text-zinc-500">{{ nt }}</span>
                  </li>
                </ol>
              </div>

              <p v-if="installError" class="rounded-lg border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-xs text-rose-300">{{ installError }}</p>
            </div>

            <div class="flex items-center justify-end gap-2 border-t border-zinc-800/80 px-5 py-3.5">
              <button
                class="rounded-lg px-3 py-2 text-xs font-semibold text-zinc-400 transition hover:text-zinc-200"
                :disabled="installing"
                @click="closeInstall()"
              >Cancel</button>
              <button
                class="inline-flex items-center gap-1.5 rounded-lg px-4 py-2 text-xs font-bold text-zinc-950 transition hover:brightness-110 disabled:opacity-50"
                :style="{ background: installTarget.accent || '#f97316' }"
                :disabled="installing || !installName.trim()"
                @click="confirmInstall()"
              >
                <Loader2 v-if="installing" class="h-3.5 w-3.5 animate-spin" />
                <Check v-else class="h-3.5 w-3.5" />
                {{ installing ? 'Installing…' : 'Install workflow' }}
              </button>
            </div>
          </div>
        </div>
      </Transition>
    </Teleport>
  </div>
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

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { Sparkles, Search, Plus, Loader2, Workflow as WorkflowIcon } from 'lucide-vue-next'
import { useApi } from '~/composables/useApi'
import type { WorkflowTemplate } from '~/types/node'

const { api } = useApi()

const templates = ref<WorkflowTemplate[]>([])
const loading = ref(true)
const usingTemplate = ref<string | null>(null)
const search = ref('')
const category = ref('')

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
    return (
      t.name.toLowerCase().includes(q)
      || t.description.toLowerCase().includes(q)
      || t.category.toLowerCase().includes(q)
    )
  })
})

async function useTemplate(tpl: WorkflowTemplate) {
  usingTemplate.value = tpl.id
  try {
    const wf = await api.post<Workflow>(`/templates/${tpl.id}/use`)
    navigateTo(`/workflows/${wf.id}`)
  } finally {
    usingTemplate.value = null
  }
}

onMounted(async () => {
  try {
    templates.value = await api.get<WorkflowTemplate[]>('/templates')
  } finally {
    loading.value = false
  }
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
            <h1 class="text-lg font-bold tracking-tight">Templates</h1>
            <p class="-mt-0.5 text-[11px] text-zinc-500">Ready-made graphs — instantiate one, then edit &amp; run</p>
          </div>
        </div>
        <label class="relative">
          <Search class="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-zinc-500" />
          <input
            v-model="search"
            class="w-56 rounded-xl border border-zinc-800 bg-zinc-900 py-2 pl-9 pr-3 text-sm outline-none transition placeholder:text-zinc-600 focus:border-orange-500/60"
            placeholder="Search templates…"
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
          class="rounded-full border px-3 py-1.5 text-xs font-semibold capitalize transition"
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
        <p class="font-medium text-zinc-300">{{ search || category ? 'No templates match your filters.' : 'No templates available.' }}</p>
        <p class="mx-auto mt-2 max-w-md text-sm leading-relaxed text-zinc-500">
          Try a different keyword or category — every template validates against the engine and runs offline-safe unless it calls live integrations.
        </p>
      </div>

      <!-- cards -->
      <div v-else class="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        <article
          v-for="tpl in filtered"
          :key="tpl.id"
          class="group flex flex-col rounded-2xl border border-zinc-800 bg-zinc-900/40 p-5 transition hover:border-orange-500/40 hover:bg-zinc-900/70"
        >
          <div class="mb-3 flex items-start justify-between gap-2">
            <div class="min-w-0">
              <h3 class="truncate font-semibold leading-snug">{{ tpl.name }}</h3>
              <p class="mt-0.5 text-[10px] font-bold uppercase tracking-widest text-orange-400/80">{{ tpl.category }}</p>
            </div>
            <span class="shrink-0 rounded-full bg-zinc-800 px-2 py-0.5 text-[10px] text-zinc-400">{{ tpl.node_count }} nodes</span>
          </div>
          <p class="mb-3 text-xs leading-relaxed text-zinc-400">{{ tpl.description }}</p>
          <p class="mb-4 line-clamp-2 text-[11px] leading-relaxed text-zinc-600">{{ tpl.docs }}</p>
          <div class="mt-auto flex items-center justify-between border-t border-zinc-800/70 pt-3">
            <span class="inline-flex items-center gap-1.5 text-[11px] text-zinc-600">
              <WorkflowIcon class="h-3.5 w-3.5" /> Instantiated inactive
            </span>
            <button
              class="inline-flex items-center gap-1.5 rounded-lg bg-zinc-800 px-3 py-1.5 text-xs font-semibold text-zinc-200 transition hover:bg-orange-500 hover:text-white disabled:opacity-50"
              :disabled="usingTemplate !== null"
              @click="useTemplate(tpl)"
            >
              <Loader2 v-if="usingTemplate === tpl.id" class="h-3.5 w-3.5 animate-spin" />
              <Plus v-else class="h-3.5 w-3.5" />
              {{ usingTemplate === tpl.id ? 'Creating…' : 'Use template' }}
            </button>
          </div>
        </article>
      </div>
    </main>
  </div>
</template>

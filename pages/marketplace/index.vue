<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import {
  Loader2, Store, CheckCircle2, Download, Search, PackageOpen,
  AlertTriangle, ExternalLink, Sparkles, X, Layers, BrainCircuit, Boxes, Phone, Bot,
  Building2, Video, TrendingUp, HeartPulse, LayoutDashboard,
} from 'lucide-vue-next'
import { useApi } from '~/composables/useApi'

// v60: Solution Marketplace - the gallery + packs layer, sold as OUTCOMES.
// A solution says "Customer Support Automation" and shows what you GET
// (the capability checklist); installing imports the embedded py8n-pack
// (workflows land inactive, datasets carry sample rows) into your estate.
// v64: three install modes - plain, AS a Py8n System (v61), and AS a
// MODEL SYSTEM (datasets + training/serving workflows as one unit).
// v72: + AS A VOICE AGENT (knowledge dataset + handler + phone agent,
// one click -> a full phone-agent system).
// v83: OPERATORS - the shelf above solutions. "Install a business
// operator" not "Install a workflow template": one click composes the
// whole business (datasets + event-reactive workflows + agent + room +
// queue + campaign + staff dashboard) into a RUNNING system.

interface OperatorSummary {
  slug: string; name: string; tagline: string; category: string
  icon: string; color: string; outcomes: string[]
  topology: { datasets: number; workflows: number; agents: number
    rooms: number; queues: number; campaign: number; dashboard: number
    processes: number }
}
interface OperatorResult {
  datasets: any[]; workflows: any[]; agents: any[]; rooms: any[]
  queues: any[]; campaign: { id: string; name: string } | null
  dashboard: { id: string; name: string; slug: string } | null
  system: { id: string; name: string; lifecycle: string } | null
  notes: string[]
}

interface SolutionSummary {
  id: string; slug: string; name: string; tagline: string; category: string
  icon: string; color: string; outcomes: string[]
  installs: number; curated: boolean; model_system_ready?: boolean; voice_agent_ready?: boolean
  workflow_count: number; dataset_count: number
}
interface SolutionDetail extends SolutionSummary {
  docs: string
  pack: { workflows: { name: string; description: string }[]; datasets: { name: string; rows: number }[]; node_types: string[] }
}
interface InstallResult {
  slug: string; name: string; installs: number
  created_workflows?: any[]; created_datasets?: any[]; skipped?: any[]; warnings?: string[]
  system?: { id: string; name: string } | null
  model_system?: { id: string; name: string; modalities: string[] } | null
  voice_agent?: { id: string; name: string; handler_workflow_id?: string; knowledge?: any } | null
}

const { api } = useApi()
const loading = ref(true)
const pageError = ref('')
const solutions = ref<SolutionSummary[]>([])
const categories = ref<string[]>([])
const category = ref('')
const q = ref('')

// v83: the operators shelf above the solutions grid
const operators = ref<OperatorSummary[]>([])
const installingOp = ref('')
const opResults = ref<Record<string, OperatorResult>>({})
const opErrors = ref<Record<string, string>>({})

const OP_ICONS: Record<string, any> = {
  video: Video, 'trending-up': TrendingUp, 'heart-pulse': HeartPulse,
}
function opIcon(icon: string) {
  return OP_ICONS[icon] || Building2
}

async function loadOperators() {
  try {
    const res = await api.get<{ operators: OperatorSummary[] }>('/operators')
    operators.value = res.operators
  } catch {
    // the shelf stays empty honestly - the solutions grid still works
  }
}

async function installOperator(slug: string) {
  installingOp.value = slug
  opErrors.value = { ...opErrors.value, [slug]: '' }
  try {
    const res = await api.post<OperatorResult>(`/operators/${slug}/install`, {})
    opResults.value = { ...opResults.value, [slug]: res }
  } catch (e: any) {
    opErrors.value = { ...opErrors.value, [slug]: e?.data?.detail || e?.message || 'Install failed' }
  } finally {
    installingOp.value = ''
  }
}

const detail = ref<SolutionDetail | null>(null)
const detailLoading = ref(false)
const installing = ref(false)
const installResult = ref<InstallResult | null>(null)
const installError = ref('')

const filtered = computed(() => solutions.value)

async function loadShelf() {
  try {
    const params = new URLSearchParams()
    if (category.value) params.set('category', category.value)
    if (q.value.trim()) params.set('q', q.value.trim())
    const res = await api.get<{ solutions: SolutionSummary[]; categories: string[] }>(`/solutions?${params}`)
    solutions.value = res.solutions
    categories.value = res.categories
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'Could not load the marketplace'
  }
}

async function openDetail(slug: string) {
  detailLoading.value = true
  installResult.value = null
  installError.value = ''
  try {
    detail.value = await api.get<SolutionDetail>(`/solutions/${slug}`)
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'Could not load the solution'
  } finally {
    detailLoading.value = false
  }
}

async function install(mode: 'plain' | 'system' | 'model_system' | 'voice_agent' = 'plain', brain = 'scaffold') {
  if (!detail.value) return
  installing.value = true
  installError.value = ''
  try {
    installResult.value = await api.post<InstallResult>(`/solutions/${detail.value.slug}/install`, {
      as_system: mode === 'system',
      as_model_system: mode === 'model_system',
      as_voice_agent: mode === 'voice_agent',
      brain, // v73: scaffold = deterministic knowledge handler; ai_agent = LLM brain on the SAME binding
    })
    await loadShelf()
  } catch (e: any) {
    installError.value = e?.data?.detail || e?.message || 'Install failed'
  } finally {
    installing.value = false
  }
}

function closeDetail() {
  detail.value = null
  installResult.value = null
}

function wfRef(w: any): string {
  return w?.id ? `/workflows/${w.id}` : '/workflows'
}
function dsRef(d: any): string {
  return d?.id ? `/datasets/${d.id}` : '/datasets'
}

onMounted(async () => {
  await Promise.all([loadShelf(), loadOperators()])
  loading.value = false
})
</script>

<template>
  <div class="min-h-screen text-zinc-100">
    <header class="sticky top-0 z-20 border-b border-zinc-800/80 bg-zinc-950/90 backdrop-blur">
      <div class="mx-auto max-w-7xl px-4 py-3.5 sm:px-6">
        <div class="flex flex-wrap items-center gap-3">
          <div class="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-cyan-500 to-emerald-500 shadow-lg shadow-cyan-500/20">
            <Store class="h-4 w-4 text-white" />
          </div>
          <div class="min-w-0 flex-1">
            <h1 class="text-lg font-bold tracking-tight">Solution Marketplace</h1>
            <p class="-mt-0.5 text-[11px] text-zinc-500">Not "webhook workflow" - outcomes. Install a capability checklist, edit everything after.</p>
          </div>
          <div class="relative">
            <Search class="absolute left-2.5 top-2.5 h-3.5 w-3.5 text-zinc-600" />
            <input
              v-model="q"
              class="w-52 rounded-xl border border-zinc-800 bg-zinc-900 py-2 pl-8 pr-3 text-xs text-zinc-200 outline-none transition focus:border-cyan-500/60"
              placeholder="Search solutions…"
              @input="loadShelf"
            />
          </div>
        </div>
      </div>
    </header>

    <main class="mx-auto max-w-7xl px-4 py-6 sm:px-6">
      <div v-if="pageError" class="mb-4 flex items-start gap-2 rounded-xl border border-rose-500/40 bg-rose-500/10 px-4 py-3 text-sm text-rose-300">
        <AlertTriangle class="mt-0.5 h-4 w-4 shrink-0" /> {{ pageError }}
      </div>

      <div class="mb-4 flex flex-wrap gap-1.5">
        <button
          class="rounded-full border px-3 py-1 text-[11px] font-medium transition"
          :class="!category ? 'border-cyan-500/50 bg-cyan-500/10 text-cyan-300' : 'border-zinc-800 bg-zinc-900 text-zinc-400 hover:text-zinc-200'"
          @click="category = ''; loadShelf()"
        >All</button>
        <button
          v-for="c in categories" :key="c"
          class="rounded-full border px-3 py-1 text-[11px] font-medium transition"
          :class="category === c ? 'border-cyan-500/50 bg-cyan-500/10 text-cyan-300' : 'border-zinc-800 bg-zinc-900 text-zinc-400 hover:text-zinc-200'"
          @click="category = c; loadShelf()"
        >{{ c }}</button>
      </div>

      <div v-if="loading" class="grid place-items-center py-16 text-zinc-600">
        <Loader2 class="h-6 w-6 animate-spin" />
      </div>

      <template v-else>
        <!-- v83: OPERATORS - "Install a business operator" -->
        <section v-if="operators.length" class="mb-8">
          <div class="mb-3 flex items-center gap-2">
            <Building2 class="h-4 w-4 text-emerald-400" />
            <h2 class="text-xs font-bold uppercase tracking-widest text-emerald-300">Operators</h2>
            <span class="text-[10px] text-zinc-600">not a workflow template - a whole business, running from the first click</span>
          </div>
          <div class="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
            <div
              v-for="op in operators" :key="op.slug"
              class="flex flex-col rounded-2xl border border-emerald-900/60 bg-gradient-to-b from-zinc-900/80 to-zinc-950 p-5 transition hover:border-emerald-500/40"
            >
              <div class="flex items-start gap-3">
                <div class="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl shadow-lg" :style="{ background: `linear-gradient(135deg, ${op.color}, ${op.color}55)`, boxShadow: `0 8px 20px ${op.color}22` }">
                  <component :is="opIcon(op.icon)" class="h-5 w-5 text-zinc-950" />
                </div>
                <div class="min-w-0 flex-1">
                  <p class="text-sm font-bold leading-tight">{{ op.name }}</p>
                  <p class="mt-0.5 text-[10px] text-zinc-600">{{ op.category }} · business operator</p>
                </div>
              </div>
              <p class="mt-3 text-[11px] leading-relaxed text-zinc-400">{{ op.tagline }}</p>
              <div class="mt-3 flex flex-wrap gap-1">
                <span class="rounded-full bg-lime-500/10 px-2 py-0.5 text-[9px] font-semibold text-lime-300">{{ op.topology.datasets }} dataset{{ op.topology.datasets === 1 ? '' : 's' }}</span>
                <span class="rounded-full bg-orange-500/10 px-2 py-0.5 text-[9px] font-semibold text-orange-300">{{ op.topology.workflows }} reactive workflow{{ op.topology.workflows === 1 ? '' : 's' }}</span>
                <span v-if="op.topology.agents" class="rounded-full bg-amber-500/10 px-2 py-0.5 text-[9px] font-semibold text-amber-300">AI agent</span>
                <span v-if="op.topology.rooms" class="rounded-full bg-sky-500/10 px-2 py-0.5 text-[9px] font-semibold text-sky-300">{{ op.topology.rooms }} room{{ op.topology.rooms === 1 ? '' : 's' }}</span>
                <span v-if="op.topology.queues" class="rounded-full bg-cyan-500/10 px-2 py-0.5 text-[9px] font-semibold text-cyan-300">waiting queue</span>
                <span v-if="op.topology.campaign" class="rounded-full bg-violet-500/10 px-2 py-0.5 text-[9px] font-semibold text-violet-300">campaign</span>
                <span v-if="op.topology.processes" class="rounded-full bg-indigo-500/10 px-2 py-0.5 text-[9px] font-semibold text-indigo-300">{{ op.topology.processes }} pre-wired process{{ op.topology.processes === 1 ? '' : 'es' }}</span>
                <span class="rounded-full bg-emerald-500/10 px-2 py-0.5 text-[9px] font-semibold text-emerald-300">staff dashboard</span>
              </div>
              <div class="mt-3 space-y-1">
                <span v-for="o in op.outcomes.slice(0, 4)" :key="o" class="flex items-center gap-1 text-[10px] text-zinc-400">
                  <CheckCircle2 class="h-2.5 w-2.5 shrink-0" :style="{ color: op.color }" /> {{ o }}
                </span>
              </div>

              <template v-if="opResults[op.slug]">
                <div class="mt-3 space-y-1.5">
                  <p class="text-[10px] font-bold uppercase tracking-widest text-emerald-400">Running - the whole estate</p>
                  <NuxtLink v-if="opResults[op.slug].system" to="/systems" class="flex items-center justify-between rounded-xl border border-sky-500/30 bg-sky-500/5 px-3 py-2 transition hover:border-sky-500/50">
                    <span class="flex items-center gap-1.5 text-xs font-semibold text-sky-200"><Boxes class="h-3.5 w-3.5" /> {{ opResults[op.slug].system.name }} ({{ opResults[op.slug].system.lifecycle }})</span>
                    <span class="flex items-center gap-1 text-[10px] text-zinc-500">open systems <ExternalLink class="h-3 w-3" /></span>
                  </NuxtLink>
                  <NuxtLink v-if="opResults[op.slug].dashboard" :to="`/dashboards/${opResults[op.slug].dashboard?.id}`" class="flex items-center justify-between rounded-xl border border-emerald-500/30 bg-emerald-500/5 px-3 py-2 transition hover:border-emerald-500/50">
                    <span class="flex items-center gap-1.5 text-xs font-semibold text-emerald-200"><LayoutDashboard class="h-3.5 w-3.5" /> {{ opResults[op.slug].dashboard?.name }}</span>
                    <span class="flex items-center gap-1 text-[10px] text-zinc-500">open board <ExternalLink class="h-3 w-3" /></span>
                  </NuxtLink>
                  <NuxtLink v-for="w in opResults[op.slug].workflows" :key="w.id" :to="wfRef(w)" class="flex items-center justify-between rounded-xl border border-orange-500/30 bg-orange-500/5 px-3 py-2 transition hover:border-orange-500/50">
                    <span class="text-xs font-semibold text-orange-200">{{ w.name }}</span>
                    <span class="flex items-center gap-1 text-[10px] text-zinc-500">reacts to {{ w.trigger }} · inactive until boot <ExternalLink class="h-3 w-3" /></span>
                  </NuxtLink>
                  <p class="text-[10px] leading-relaxed text-zinc-500">{{ opResults[op.slug].notes?.[0] }}</p>
                </div>
              </template>
              <p v-else-if="opErrors[op.slug]" class="mt-3 rounded-xl border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-[11px] text-rose-300">{{ opErrors[op.slug] }}</p>

              <button
                v-if="!opResults[op.slug]"
                class="mt-4 flex w-full items-center justify-center gap-2 rounded-xl bg-emerald-500 px-4 py-2 text-sm font-bold text-zinc-950 transition hover:bg-emerald-400 disabled:opacity-50"
                :disabled="installingOp === op.slug"
                @click="installOperator(op.slug)"
              >
                <Loader2 v-if="installingOp === op.slug" class="h-4 w-4 animate-spin" />
                <Building2 v-else class="h-4 w-4" />
                Install operator
              </button>
            </div>
          </div>
        </section>

        <div class="mb-3 flex items-center gap-2">
          <PackageOpen class="h-4 w-4 text-cyan-400" />
          <h2 class="text-xs font-bold uppercase tracking-widest text-cyan-300">Solutions</h2>
          <span class="text-[10px] text-zinc-600">capability packs - install, then wire the credentials you own</span>
        </div>
        <div class="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        <button
          v-for="s in filtered" :key="s.slug"
          class="flex flex-col rounded-2xl border border-zinc-800/80 bg-zinc-900/50 p-5 text-left transition hover:border-cyan-500/40 hover:bg-zinc-900"
          @click="openDetail(s.slug)"
        >
          <div class="flex items-start gap-3">
            <div class="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl shadow-lg" :style="{ background: `linear-gradient(135deg, ${s.color}, ${s.color}55)`, boxShadow: `0 8px 20px ${s.color}22` }">
              <PackageOpen class="h-5 w-5 text-zinc-950" />
            </div>
            <div class="min-w-0 flex-1">
              <p class="text-sm font-bold leading-tight">{{ s.name }}</p>
              <p class="mt-0.5 text-[10px] text-zinc-600">
                {{ s.category }} · {{ s.workflow_count }} workflow{{ s.workflow_count === 1 ? '' : 's' }}
                · {{ s.dataset_count }} dataset{{ s.dataset_count === 1 ? '' : 's' }}
                · {{ s.installs }} install{{ s.installs === 1 ? '' : 's' }}
                <span v-if="s.curated" class="ml-1 rounded-full bg-cyan-500/15 px-1.5 py-0.5 text-[9px] font-bold text-cyan-300">curated</span>
                <span v-if="s.model_system_ready" class="ml-1 rounded-full bg-fuchsia-500/15 px-1.5 py-0.5 text-[9px] font-bold text-fuchsia-300">model system</span>
                <span v-if="s.voice_agent_ready" class="ml-1 rounded-full bg-orange-500/15 px-1.5 py-0.5 text-[9px] font-bold text-orange-300">voice agent</span>
              </p>
            </div>
          </div>
          <p class="mt-3 line-clamp-2 text-[11px] leading-relaxed text-zinc-400">{{ s.tagline }}</p>
          <div class="mt-3 grid grid-cols-2 gap-x-2 gap-y-1">
            <span v-for="o in s.outcomes.slice(0, 6)" :key="o" class="flex items-center gap-1 text-[10px] text-zinc-400">
              <CheckCircle2 class="h-2.5 w-2.5 shrink-0" :style="{ color: s.color }" /> {{ o }}
            </span>
          </div>
        </button>
        <p v-if="!filtered.length" class="col-span-full rounded-2xl border border-dashed border-zinc-800 py-12 text-center text-xs text-zinc-600">
          Nothing here yet - author a solution from your own workflows via POST /solutions.
        </p>
        </div>
      </template>
    </main>

    <!-- detail modal -->
    <Teleport to="body">
      <div
        v-if="detail || detailLoading"
        class="fixed inset-0 z-50 grid place-items-center bg-black/70 p-4 backdrop-blur-sm"
        @click.self="closeDetail"
      >
        <div class="flex max-h-[85vh] w-full max-w-2xl flex-col rounded-2xl border border-zinc-800 bg-zinc-900 shadow-2xl">
          <div class="flex items-start justify-between gap-3 border-b border-zinc-800 px-5 py-4">
            <div class="min-w-0 flex-1">
              <div class="flex items-center gap-2">
                <div class="flex h-8 w-8 items-center justify-center rounded-lg" :style="{ background: `linear-gradient(135deg, ${detail?.color || '#22d3ee'}, ${detail?.color || '#22d3ee'}55)` }">
                  <PackageOpen class="h-4 w-4 text-zinc-950" />
                </div>
                <h3 class="text-sm font-bold">{{ detail?.name }}</h3>
                <span v-if="detail?.curated" class="rounded-full bg-cyan-500/15 px-1.5 py-0.5 text-[9px] font-bold text-cyan-300">curated</span>
              </div>
              <p class="mt-1 text-[11px] text-zinc-500">{{ detail?.tagline }}</p>
            </div>
            <button class="rounded-lg p-1.5 text-zinc-500 transition hover:bg-zinc-800 hover:text-zinc-200" @click="closeDetail">
              <X class="h-4 w-4" />
            </button>
          </div>

          <div class="min-h-0 flex-1 overflow-y-auto p-5">
            <div v-if="detailLoading" class="flex h-32 items-center justify-center text-zinc-500">
              <Loader2 class="h-5 w-5 animate-spin" />
            </div>
            <template v-else-if="detail">
              <p class="text-[10px] font-bold uppercase tracking-widest text-zinc-500">What you get</p>
              <div class="mt-2 grid gap-1.5 sm:grid-cols-2">
                <span v-for="o in detail.outcomes" :key="o" class="flex items-center gap-1.5 text-[11px] text-zinc-300">
                  <CheckCircle2 class="h-3 w-3 shrink-0" :style="{ color: detail.color }" /> {{ o }}
                </span>
              </div>

              <p class="mt-4 text-[10px] font-bold uppercase tracking-widest text-zinc-500">Inside the pack</p>
              <div class="mt-2 space-y-1.5">
                <div v-for="w in detail.pack.workflows" :key="w.name" class="flex items-center gap-2 rounded-xl border border-zinc-800 bg-zinc-950/60 px-3 py-2">
                  <Layers class="h-3.5 w-3.5 shrink-0 text-orange-400" />
                  <span class="text-xs font-semibold">{{ w.name }}</span>
                  <span class="truncate text-[10px] text-zinc-600">{{ w.description }}</span>
                </div>
                <div v-for="d in detail.pack.datasets" :key="d.name" class="flex items-center gap-2 rounded-xl border border-zinc-800 bg-zinc-950/60 px-3 py-2">
                  <Layers class="h-3.5 w-3.5 shrink-0 text-lime-400" />
                  <span class="text-xs font-semibold">{{ d.name }}</span>
                  <span class="text-[10px] text-zinc-600">{{ d.rows }} sample rows</span>
                </div>
              </div>
              <p class="mt-2 text-[10px] text-zinc-600">node types: {{ detail.pack.node_types.join(', ') }}</p>

              <div class="mt-4 rounded-xl border border-zinc-800 bg-zinc-950/60 p-3">
                <p class="flex items-center gap-1.5 text-[10px] font-bold uppercase tracking-widest text-zinc-400"><Sparkles class="h-3 w-3" /> How to use it</p>
                <p class="mt-1 text-[11px] leading-relaxed text-zinc-400">{{ detail.docs }}</p>
              </div>

              <template v-if="installResult">
                <p class="mt-4 text-[10px] font-bold uppercase tracking-widest text-emerald-400">Installed</p>
                <div class="mt-2 space-y-1.5">
                  <NuxtLink v-for="w in installResult.created_workflows || []" :key="w.id" :to="wfRef(w)" class="flex items-center justify-between rounded-xl border border-emerald-500/30 bg-emerald-500/5 px-3 py-2 transition hover:border-emerald-500/50">
                    <span class="text-xs font-semibold text-emerald-200">{{ w.name }}</span>
                    <span class="flex items-center gap-1 text-[10px] text-zinc-500">open workflow <ExternalLink class="h-3 w-3" /></span>
                  </NuxtLink>
                  <NuxtLink v-for="d in installResult.created_datasets || []" :key="d.id" :to="dsRef(d)" class="flex items-center justify-between rounded-xl border border-lime-500/30 bg-lime-500/5 px-3 py-2 transition hover:border-lime-500/50">
                    <span class="text-xs font-semibold text-lime-200">{{ d.name }}</span>
                    <span class="flex items-center gap-1 text-[10px] text-zinc-500">open dataset <ExternalLink class="h-3 w-3" /></span>
                  </NuxtLink>
                </div>
                <p v-if="installResult.system" class="mt-2 flex items-center justify-between rounded-xl border border-sky-500/30 bg-sky-500/5 px-3 py-2">
                  <span class="flex items-center gap-1.5 text-xs font-semibold text-sky-200"><Boxes class="h-3.5 w-3.5" /> {{ installResult.system.name }}</span>
                  <NuxtLink to="/systems" class="flex items-center gap-1 text-[10px] text-zinc-500">open systems <ExternalLink class="h-3 w-3" /></NuxtLink>
                </p>
                <p v-if="installResult.model_system" class="mt-2 flex items-center justify-between rounded-xl border border-fuchsia-500/30 bg-fuchsia-500/5 px-3 py-2">
                  <span class="flex items-center gap-1.5 text-xs font-semibold text-fuchsia-200"><BrainCircuit class="h-3.5 w-3.5" /> {{ installResult.model_system.name }}</span>
                  <NuxtLink to="/model-systems" class="flex items-center gap-1 text-[10px] text-zinc-500">open model systems <ExternalLink class="h-3 w-3" /></NuxtLink>
                </p>
                <p v-if="installResult.voice_agent" class="mt-2 flex items-center justify-between rounded-xl border border-orange-500/30 bg-orange-500/5 px-3 py-2">
                  <span class="flex items-center gap-1.5 text-xs font-semibold text-orange-200"><Phone class="h-3.5 w-3.5" /> {{ installResult.voice_agent.name }}</span>
                  <NuxtLink to="/channels" class="flex items-center gap-1 text-[10px] text-zinc-500">open channels <ExternalLink class="h-3 w-3" /></NuxtLink>
                </p>
                <p v-if="installResult.skipped?.length" class="mt-2 text-[10px] text-amber-400/80">skipped: {{ installResult.skipped.length }} item(s)</p>
              </template>
              <p v-if="installError" class="mt-3 rounded-xl border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-[11px] text-rose-300">{{ installError }}</p>
            </template>
          </div>

          <div class="border-t border-zinc-800 px-5 py-3">
            <div v-if="!installResult" class="space-y-2">
            <button
              class="flex w-full items-center justify-center gap-2 rounded-xl bg-cyan-500 px-4 py-2.5 text-sm font-bold text-zinc-950 transition hover:bg-cyan-400 disabled:opacity-50"
              :disabled="installing"
              @click="install('plain')"
            >
              <Loader2 v-if="installing" class="h-4 w-4 animate-spin" />
              <Download v-else class="h-4 w-4" />
              Install solution
            </button>
            <div class="grid grid-cols-2 gap-2">
              <button
                class="flex items-center justify-center gap-1.5 rounded-xl border border-sky-500/40 bg-sky-500/10 px-3 py-2 text-[11px] font-bold text-sky-300 transition hover:bg-sky-500/20 disabled:opacity-50"
                :disabled="installing"
                @click="install('system')"
              >
                <Boxes class="h-3.5 w-3.5" /> as System
              </button>
              <button
                class="flex items-center justify-center gap-1.5 rounded-xl border border-fuchsia-500/40 bg-fuchsia-500/10 px-3 py-2 text-[11px] font-bold text-fuchsia-300 transition hover:bg-fuchsia-500/20 disabled:opacity-50"
                :disabled="installing"
                @click="install('model_system')"
              >
                <BrainCircuit class="h-3.5 w-3.5" /> as Model System
              </button>
            </div>
            <div v-if="detail.voice_agent_ready" class="grid grid-cols-2 gap-2">
              <button
                class="flex items-center justify-center gap-1.5 rounded-xl border border-orange-500/40 bg-orange-500/10 px-3 py-2 text-[11px] font-bold text-orange-300 transition hover:bg-orange-500/20 disabled:opacity-50"
                :disabled="installing"
                @click="install('voice_agent', 'scaffold')"
              >
                <Phone class="h-3.5 w-3.5" /> as Voice Agent
              </button>
              <button
                class="flex items-center justify-center gap-1.5 rounded-xl border border-violet-500/40 bg-violet-500/10 px-3 py-2 text-[11px] font-bold text-violet-300 transition hover:bg-violet-500/20 disabled:opacity-50"
                :disabled="installing"
                @click="install('voice_agent', 'ai_agent')"
              >
                <Bot class="h-3.5 w-3.5" /> + LLM brain (v73)
              </button>
            </div>
            </div>
            <p v-else class="text-center text-[10px] text-zinc-600">
              Workflows install INACTIVE - open them, run training, then activate triggers. {{ installResult.installs }} installs so far.
            </p>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

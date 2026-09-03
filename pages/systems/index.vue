<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import {
  Loader2, Boxes, Plus, CheckCircle2, XCircle, AlertTriangle, X,
  Workflow as WorkflowIcon, Database, LayoutGrid, Gauge, Network, FileBarChart,
  Unlink, RefreshCw, Trash2,
} from 'lucide-vue-next'
import { useApi } from '~/composables/useApi'

// v61: Py8n Systems - the operating unit above workflows. A system binds
// workflows + datasets + apps + dashboards + models + reports into one
// named, health-scored, ownable unit. Membership is curated; everything
// the system REPORTS (health, activity) is derived from the members.

interface SystemCard {
  id: string; name: string; description: string; icon: string; color: string
  components: Record<string, number>; total_components: number
  verdict: string; created_at: string | null
}
interface SystemDetail extends SystemCard {
  grouped: Record<string, { component_id: string; kind: string; ref_id: string; name: string; added_at: string | null }[]>
  health: {
    verdict: string
    workflows: { bound: number; runs_7d: number; failures_7d: number; failure_rate_7d: number; failing_workflows: { workflow_id: string; name: string; failures: number; last_error: string | null }[] }
    datasets: { total: number; healthy: number; degraded: number; unhealthy: number; unscored: number; worst: { dataset_id: string; name: string; score: number; status: string } | null }
    reports: { bound: number; ok_7d: number; error_7d: number }
    generated_at: string
  }
}

const { api } = useApi()
const loading = ref(true)
const pageError = ref('')
const systems = ref<SystemCard[]>([])
const detail = ref<SystemDetail | null>(null)
const detailLoading = ref(false)

const showCreate = ref(false)
const newName = ref('')
const newDesc = ref('')
const creating = ref(false)

const showAttach = ref(false)
const attachKind = ref<string>('workflow')
const attachCandidates = ref<any[]>([])
const attachLoading = ref(false)
const attaching = ref(false)

const KIND_META: Record<string, { label: string; icon: any; ref: (id: string) => string; color: string }> = {
  workflow: { label: 'Workflows', icon: WorkflowIcon, ref: (id) => `/workflows/${id}`, color: 'text-orange-400' },
  dataset: { label: 'Datasets', icon: Database, ref: (id) => `/datasets/${id}`, color: 'text-lime-400' },
  app: { label: 'Apps', icon: LayoutGrid, ref: (id) => `/apps/${id}`, color: 'text-violet-400' },
  dashboard: { label: 'Dashboards', icon: Gauge, ref: (id) => `/dashboards`, color: 'text-sky-400' },
  model: { label: 'Models', icon: Network, ref: () => `/models`, color: 'text-pink-400' },
  report: { label: 'Reports', icon: FileBarChart, ref: () => `/reports`, color: 'text-cyan-400' },
}
const KINDS = Object.keys(KIND_META)

const verdictMeta: Record<string, { chip: string; label: string }> = {
  healthy: { chip: 'bg-emerald-500/15 text-emerald-300', label: 'healthy' },
  degraded: { chip: 'bg-amber-500/15 text-amber-300', label: 'degraded' },
  unhealthy: { chip: 'bg-rose-500/15 text-rose-300', label: 'unhealthy' },
}

async function loadSystems() {
  try {
    systems.value = await api.get<SystemCard[]>('/systems')
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'Could not load systems'
  }
}

async function openDetail(id: string) {
  detailLoading.value = true
  try {
    detail.value = await api.get<SystemDetail>(`/systems/${id}`)
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'Could not load the system'
  } finally {
    detailLoading.value = false
  }
}

async function createSystem() {
  if (!newName.value.trim()) return
  creating.value = true
  try {
    const created = await api.post<any>('/systems', { name: newName.value.trim(), description: newDesc.value.trim() })
    showCreate.value = false
    newName.value = ''
    newDesc.value = ''
    await loadSystems()
    await openDetail(created.id)
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'Create failed'
  } finally {
    creating.value = false
  }
}

async function openAttach(kind: string) {
  attachKind.value = kind
  showAttach.value = true
  attachLoading.value = true
  attachCandidates.value = []
  const endpoints: Record<string, string> = {
    workflow: '/workflows', dataset: '/datasets', app: '/apps',
    dashboard: '/dashboards', model: '/models', report: '/reports',
  }
  try {
    const res = await api.get<any>(endpoints[kind])
    attachCandidates.value = Array.isArray(res) ? res : (res.items || res.workflows || res.datasets || res.apps || res.dashboards || res.models || res.reports || [])
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'Could not load candidates'
  } finally {
    attachLoading.value = false
  }
}

async function attach(refId: string) {
  if (!detail.value) return
  attaching.value = true
  try {
    await api.post(`/systems/${detail.value.id}/components`, { kind: attachKind.value, ref_id: refId })
    showAttach.value = false
    await openDetail(detail.value.id)
    await loadSystems()
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'Attach failed'
  } finally {
    attaching.value = false
  }
}

async function detach(componentId: string) {
  if (!detail.value) return
  try {
    await api.delete(`/systems/${detail.value.id}/components/${componentId}`)
    await openDetail(detail.value.id)
    await loadSystems()
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'Detach failed'
  }
}

async function dissolve(s: SystemCard) {
  if (!confirm(`Dissolve "${s.name}"? The member workflows, datasets and apps are NOT deleted.`)) return
  try {
    await api.delete(`/systems/${s.id}`)
    if (detail.value?.id === s.id) detail.value = null
    await loadSystems()
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'Dissolve failed'
  }
}

const boundIds = computed(() => {
  const ids = new Set<string>()
  for (const k of KINDS) for (const c of detail.value?.grouped[k] || []) ids.add(c.ref_id)
  return ids
})

onMounted(async () => {
  await loadSystems()
  loading.value = false
})
</script>

<template>
  <div class="min-h-screen text-zinc-100">
    <header class="sticky top-0 z-20 border-b border-zinc-800/80 bg-zinc-950/90 backdrop-blur">
      <div class="mx-auto max-w-7xl px-4 py-3.5 sm:px-6">
        <div class="flex flex-wrap items-center gap-3">
          <div class="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-orange-500 to-amber-500 shadow-lg shadow-orange-500/20">
            <Boxes class="h-4 w-4 text-white" />
          </div>
          <div class="min-w-0 flex-1">
            <h1 class="text-lg font-bold tracking-tight">Systems</h1>
            <p class="-mt-0.5 text-[11px] text-zinc-500">Workflows + datasets + apps + agents as one operating unit - health derived, never stored</p>
          </div>
          <button
            class="flex items-center gap-1.5 rounded-xl bg-orange-500 px-3.5 py-2 text-xs font-bold text-white transition hover:bg-orange-400"
            @click="showCreate = true"
          >
            <Plus class="h-3.5 w-3.5" /> New system
          </button>
        </div>
      </div>
    </header>

    <main class="mx-auto max-w-7xl px-4 py-6 sm:px-6">
      <div v-if="pageError" class="mb-4 flex items-start gap-2 rounded-xl border border-rose-500/40 bg-rose-500/10 px-4 py-3 text-sm text-rose-300">
        <AlertTriangle class="mt-0.5 h-4 w-4 shrink-0" /> {{ pageError }}
      </div>

      <div v-if="loading" class="grid place-items-center py-16 text-zinc-600">
        <Loader2 class="h-6 w-6 animate-spin" />
      </div>

      <template v-else>
        <!-- create dialog -->
        <div v-if="showCreate" class="mb-5 rounded-2xl border border-orange-500/30 bg-orange-500/5 p-5">
          <h2 class="text-sm font-bold">Name your system</h2>
          <p class="mt-0.5 text-[11px] text-zinc-500">e.g. "Customer Operations", "E-commerce Analytics" - the unit the business knows.</p>
          <input
            v-model="newName"
            class="mt-3 w-full max-w-md rounded-xl border border-zinc-800 bg-zinc-950 px-3 py-2 text-sm text-zinc-200 outline-none focus:border-orange-500/60"
            placeholder="Customer Operations"
          />
          <textarea
            v-model="newDesc"
            rows="2"
            class="mt-2 w-full max-w-2xl resize-y rounded-xl border border-zinc-800 bg-zinc-950 px-3 py-2 text-xs text-zinc-300 outline-none focus:border-orange-500/60"
            placeholder="What part of the business does this system run?"
          />
          <div class="mt-3 flex gap-2">
            <button class="rounded-xl bg-orange-500 px-4 py-2 text-xs font-bold text-white transition hover:bg-orange-400 disabled:opacity-50" :disabled="creating || !newName.trim()" @click="createSystem">
              <Loader2 v-if="creating" class="mr-1 inline h-3 w-3 animate-spin" /> Create
            </button>
            <button class="rounded-xl border border-zinc-800 px-4 py-2 text-xs text-zinc-400 transition hover:text-zinc-200" @click="showCreate = false">Cancel</button>
          </div>
        </div>

        <!-- cards -->
        <div v-if="!detail" class="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          <button
            v-for="s in systems" :key="s.id"
            class="rounded-2xl border border-zinc-800/80 bg-zinc-900/50 p-5 text-left transition hover:border-orange-500/40"
            @click="openDetail(s.id)"
          >
            <div class="flex items-start gap-3">
              <div class="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl" :style="{ background: `linear-gradient(135deg, ${s.color}, ${s.color}55)` }">
                <Boxes class="h-5 w-5 text-zinc-950" />
              </div>
              <div class="min-w-0 flex-1">
                <p class="text-sm font-bold leading-tight">{{ s.name }}</p>
                <span class="mt-1 inline-block rounded-full px-2 py-0.5 text-[9px] font-bold uppercase" :class="verdictMeta[s.verdict]?.chip">{{ verdictMeta[s.verdict]?.label || s.verdict }}</span>
              </div>
            </div>
            <p class="mt-2 line-clamp-2 text-[11px] text-zinc-500">{{ s.description || 'no description' }}</p>
            <div class="mt-3 flex flex-wrap gap-1.5 text-[10px]">
              <template v-for="k in KINDS" :key="k">
                <span v-if="s.components[k]" class="rounded-full bg-zinc-800 px-2 py-0.5 text-zinc-300">{{ s.components[k] }} {{ KIND_META[k].label.toLowerCase() }}</span>
              </template>
              <span v-if="!s.total_components" class="rounded-full bg-zinc-800 px-2 py-0.5 text-zinc-500">empty - open to bind</span>
            </div>
          </button>
          <p v-if="!systems.length" class="col-span-full rounded-2xl border border-dashed border-zinc-800 py-12 text-center text-xs text-zinc-600">
            No systems yet - or install a Marketplace solution with "as system".
          </p>
        </div>

        <!-- detail -->
        <div v-else>
          <div class="mb-4 flex flex-wrap items-center gap-3">
            <button class="rounded-lg p-1.5 text-zinc-500 transition hover:bg-zinc-800 hover:text-zinc-200" title="Back" @click="detail = null; loadSystems()">
              <X class="h-4 w-4" />
            </button>
            <div class="flex h-10 w-10 items-center justify-center rounded-xl" :style="{ background: `linear-gradient(135deg, ${detail.color}, ${detail.color}55)` }">
              <Boxes class="h-5 w-5 text-zinc-950" />
            </div>
            <div class="min-w-0 flex-1">
              <h2 class="text-base font-bold">{{ detail.name }}</h2>
              <p class="text-[11px] text-zinc-500">{{ detail.description || 'no description' }}</p>
            </div>
            <span class="rounded-full px-2.5 py-1 text-[10px] font-bold uppercase" :class="verdictMeta[detail.health.verdict]?.chip">{{ verdictMeta[detail.health.verdict]?.label }}</span>
            <button class="rounded-xl border border-rose-500/30 bg-rose-500/5 p-2 text-rose-300 transition hover:bg-rose-500/15" title="Dissolve system (members stay)" @click="dissolve(detail)">
              <Trash2 class="h-3.5 w-3.5" />
            </button>
          </div>

          <!-- health strip -->
          <div class="mb-5 grid gap-3 sm:grid-cols-3">
            <div class="rounded-2xl border border-zinc-800/80 bg-zinc-900/50 p-4">
              <p class="text-[10px] uppercase tracking-wide text-zinc-500">Pipelines (7d)</p>
              <p class="mt-1 text-xl font-bold">{{ detail.health.workflows.runs_7d }} <span class="text-xs font-normal text-zinc-500">runs</span></p>
              <p class="text-[10px]" :class="detail.health.workflows.failures_7d ? 'text-rose-300' : 'text-emerald-300'">
                {{ detail.health.workflows.failures_7d }} failures · {{ detail.health.workflows.failure_rate_7d }}%
              </p>
            </div>
            <div class="rounded-2xl border border-zinc-800/80 bg-zinc-900/50 p-4">
              <p class="text-[10px] uppercase tracking-wide text-zinc-500">Datasets</p>
              <p class="mt-1 text-xl font-bold">{{ detail.health.datasets.total }}</p>
              <p class="text-[10px] text-zinc-500">
                <span class="text-emerald-300">{{ detail.health.datasets.healthy }}</span> /
                <span class="text-amber-300">{{ detail.health.datasets.degraded }}</span> /
                <span class="text-rose-300">{{ detail.health.datasets.unhealthy }}</span> healthy/degraded/unhealthy
              </p>
              <p v-if="detail.health.datasets.worst" class="mt-0.5 text-[10px] text-zinc-600">worst: {{ detail.health.datasets.worst.name }} ({{ detail.health.datasets.worst.score }})</p>
            </div>
            <div class="rounded-2xl border border-zinc-800/80 bg-zinc-900/50 p-4">
              <p class="text-[10px] uppercase tracking-wide text-zinc-500">Report deliveries (7d)</p>
              <p class="mt-1 text-xl font-bold">{{ detail.health.reports.ok_7d }} <span class="text-xs font-normal text-emerald-400">ok</span></p>
              <p class="text-[10px]" :class="detail.health.reports.error_7d ? 'text-rose-300' : 'text-zinc-500'">{{ detail.health.reports.error_7d }} errors</p>
            </div>
          </div>

          <!-- failing pipelines -->
          <div v-if="detail.health.workflows.failing_workflows.length" class="mb-5 rounded-2xl border border-rose-500/30 bg-rose-500/5 p-4">
            <h3 class="text-xs font-bold text-rose-300">Failing pipelines</h3>
            <div class="mt-2 space-y-1.5">
              <div v-for="w in detail.health.workflows.failing_workflows" :key="w.workflow_id" class="flex items-center gap-2 text-[11px]">
                <XCircle class="h-3 w-3 shrink-0 text-rose-400" />
                <span class="font-semibold">{{ w.name }}</span>
                <span class="text-zinc-500">{{ w.failures }} failures</span>
                <span class="truncate text-zinc-600" :title="w.last_error">{{ w.last_error }}</span>
              </div>
            </div>
          </div>

          <!-- grouped components -->
          <div class="space-y-4">
            <div v-for="k in KINDS" :key="k">
              <div class="mb-2 flex items-center gap-2">
                <component :is="KIND_META[k].icon" class="h-3.5 w-3.5" :class="KIND_META[k].color" />
                <h3 class="text-xs font-bold">{{ KIND_META[k].label }}</h3>
                <button class="ml-auto flex items-center gap-1 rounded-lg border border-zinc-800 px-2 py-1 text-[10px] text-zinc-400 transition hover:border-orange-500/40 hover:text-orange-300" @click="openAttach(k)">
                  <Plus class="h-3 w-3" /> bind
                </button>
              </div>
              <div v-if="detail.grouped[k].length" class="space-y-1.5">
                <div v-for="c in detail.grouped[k]" :key="c.component_id" class="flex items-center gap-2 rounded-xl border border-zinc-800/80 bg-zinc-900/50 px-3 py-2">
                  <CheckCircle2 class="h-3 w-3 shrink-0 text-emerald-400" />
                  <NuxtLink :to="KIND_META[k].ref(c.ref_id)" class="min-w-0 flex-1 truncate text-xs font-semibold text-zinc-200 hover:text-orange-300">{{ c.name }}</NuxtLink>
                  <span class="text-[10px] text-zinc-600">added {{ new Date(c.added_at).toLocaleDateString() }}</span>
                  <button class="rounded-lg p-1 text-zinc-600 transition hover:bg-zinc-800 hover:text-rose-300" title="Unbind" @click="detach(c.component_id)">
                    <Unlink class="h-3 w-3" />
                  </button>
                </div>
              </div>
              <p v-else class="rounded-xl border border-dashed border-zinc-800 px-3 py-3 text-center text-[10px] text-zinc-600">none bound</p>
            </div>
          </div>
        </div>
      </template>
    </main>

    <!-- attach modal -->
    <Teleport to="body">
      <div v-if="showAttach" class="fixed inset-0 z-50 grid place-items-center bg-black/70 p-4 backdrop-blur-sm" @click.self="showAttach = false">
        <div class="flex max-h-[70vh] w-full max-w-md flex-col rounded-2xl border border-zinc-800 bg-zinc-900 shadow-2xl">
          <div class="flex items-center justify-between border-b border-zinc-800 px-5 py-4">
            <h3 class="text-sm font-bold">Bind {{ KIND_META[attachKind]?.label.toLowerCase() }}</h3>
            <button class="rounded-lg p-1.5 text-zinc-500 hover:bg-zinc-800 hover:text-zinc-200" @click="showAttach = false"><X class="h-4 w-4" /></button>
          </div>
          <div class="min-h-0 flex-1 overflow-y-auto p-4">
            <div v-if="attachLoading" class="grid place-items-center py-8"><Loader2 class="h-5 w-5 animate-spin text-zinc-600" /></div>
            <p v-else-if="!attachCandidates.length" class="py-8 text-center text-xs text-zinc-600">Nothing to bind - create one first.</p>
            <div v-else class="space-y-1.5">
              <button
                v-for="cand in attachCandidates" :key="cand.id"
                class="flex w-full items-center gap-2 rounded-xl border px-3 py-2 text-left text-xs transition disabled:opacity-40"
                :class="boundIds.has(cand.id) ? 'border-zinc-800 bg-zinc-950/40 text-zinc-500' : 'border-zinc-800 bg-zinc-950/60 hover:border-orange-500/40'"
                :disabled="boundIds.has(cand.id) || attaching"
                @click="attach(cand.id)"
              >
                <CheckCircle2 v-if="boundIds.has(cand.id)" class="h-3 w-3 shrink-0 text-emerald-400" />
                <span class="min-w-0 flex-1 truncate font-semibold">{{ cand.name }}</span>
                <span v-if="boundIds.has(cand.id)" class="text-[10px] text-zinc-600">already bound</span>
              </button>
            </div>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

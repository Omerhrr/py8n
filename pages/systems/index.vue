<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import {
  Loader2, Boxes, Plus, CheckCircle2, XCircle, AlertTriangle, X,
  Workflow as WorkflowIcon, Database, LayoutGrid, Gauge, Network, FileBarChart,
  Unlink, RefreshCw, Trash2, Sparkles, Users, BrainCircuit, Share2, UserPlus, ShieldCheck,
} from 'lucide-vue-next'
import { useApi } from '~/composables/useApi'

// v61: Py8n Systems - the operating unit above workflows. A system binds
// workflows + datasets + apps + dashboards + models + reports into one
// named, health-scored, ownable unit. Membership is curated; everything
// the system REPORTS (health, activity) is derived from the members.
// v62 governance: role-specific templates, per-system roles (owner /
// editor / viewer) and the cross-system dependency map.

interface SystemCard {
  id: string; name: string; description: string; icon: string; color: string
  components: Record<string, number>; total_components: number
  verdict: string; created_at: string | null; my_role?: string
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
interface TemplateCard {
  slug: string; name: string; role: string; tagline: string; icon: string; color: string
  outcomes: string[]; workflows: string[]; datasets: string[]
}
interface MemberRow { user_id: string; email: string | null; name: string | null; role: string; added_at: string | null; is_owner: boolean }
interface DepNode { id: string; name: string; icon: string; color: string; total_components: number; verdict: string; owner: string | null }
interface DepEdge { from: string; to: string; type: string; weight: number; evidence: any[] }
interface DepGraph { nodes: DepNode[]; edges: DepEdge[]; summary: { systems: number; edges: number; by_type: Record<string, number> } }

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

// v62: templates
const ROLE_META: Record<string, { label: string }> = {
  data_engineer: { label: 'Data engineer' },
  ml_engineer: { label: 'ML engineer' },
  ops_lead: { label: 'Ops lead' },
  support_lead: { label: 'Support lead' },
}
const roleFilter = ref('')
const templates = ref<TemplateCard[]>([])
const instantiating = ref('')

// v62: members
const members = ref<MemberRow[]>([])
const inviteEmail = ref('')
const inviteRole = ref('viewer')
const memberBusy = ref(false)

// v62: dependency map
const depGraph = ref<DepGraph | null>(null)
const depLoading = ref(false)
const selectedEdge = ref<DepEdge | null>(null)
const EDGE_STYLE: Record<string, { color: string; label: string }> = {
  shared_object: { color: '#38bdf8', label: 'shared object' },
  data_flow: { color: '#34d399', label: 'data flow' },
  model_flow: { color: '#f472b6', label: 'model flow' },
}

const KIND_META: Record<string, { label: string; icon: any; ref: (id: string) => string; color: string }> = {
  workflow: { label: 'Workflows', icon: WorkflowIcon, ref: (id) => `/workflows/${id}`, color: 'text-orange-400' },
  dataset: { label: 'Datasets', icon: Database, ref: (id) => `/datasets/${id}`, color: 'text-lime-400' },
  app: { label: 'Apps', icon: LayoutGrid, ref: (id) => `/apps/${id}`, color: 'text-violet-400' },
  dashboard: { label: 'Dashboards', icon: Gauge, ref: (id) => `/dashboards`, color: 'text-sky-400' },
  model: { label: 'Models', icon: Network, ref: () => `/models`, color: 'text-pink-400' },
  report: { label: 'Reports', icon: FileBarChart, ref: () => `/reports`, color: 'text-cyan-400' },
  model_system: { label: 'Model systems', icon: BrainCircuit, ref: (id) => `/model-systems`, color: 'text-indigo-400' },
}
const KINDS = Object.keys(KIND_META)

const verdictMeta: Record<string, { chip: string; label: string }> = {
  healthy: { chip: 'bg-emerald-500/15 text-emerald-300', label: 'healthy' },
  degraded: { chip: 'bg-amber-500/15 text-amber-300', label: 'degraded' },
  unhealthy: { chip: 'bg-rose-500/15 text-rose-300', label: 'unhealthy' },
  unscored: { chip: 'bg-zinc-500/15 text-zinc-400', label: 'unscored' },
}

const canEdit = computed(() => detail.value && ['owner', 'editor'].includes(detail.value.my_role || ''))
const isOwner = computed(() => detail.value?.my_role === 'owner')

async function loadSystems() {
  try {
    systems.value = await api.get<SystemCard[]>('/systems')
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'Could not load systems'
  }
}

async function loadTemplates() {
  try {
    const res = await api.get<any>(`/systems/templates${roleFilter.value ? `?role=${roleFilter.value}` : ''}`)
    templates.value = res.templates || []
  } catch { /* the strip degrades silently */ }
}

async function loadDeps() {
  depLoading.value = true
  selectedEdge.value = null
  try {
    depGraph.value = await api.get<DepGraph>('/systems/dependencies')
  } catch { depGraph.value = null } finally { depLoading.value = false }
}

async function openDetail(id: string) {
  detailLoading.value = true
  try {
    detail.value = await api.get<SystemDetail>(`/systems/${id}`)
    const m = await api.get<any>(`/systems/${id}/members`)
    members.value = m.members || []
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

async function instantiate(slug: string) {
  instantiating.value = slug
  try {
    const created = await api.post<any>(`/systems/templates/${slug}/instantiate`, {})
    await loadSystems()
    await openDetail(created.id)
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'Instantiate failed'
  } finally {
    instantiating.value = ''
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
    model_system: '/model-systems',
  }
  try {
    const res = await api.get<any>(endpoints[kind])
    attachCandidates.value = Array.isArray(res) ? res : (res.items || res.workflows || res.datasets || res.apps || res.dashboards || res.models || res.reports || res.model_systems || [])
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
    await loadDeps()
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'Dissolve failed'
  }
}

// v62: member management
async function invite() {
  if (!detail.value || !inviteEmail.value.trim()) return
  memberBusy.value = true
  try {
    await api.post(`/systems/${detail.value.id}/members`, { email: inviteEmail.value.trim(), role: inviteRole.value })
    inviteEmail.value = ''
    const m = await api.get<any>(`/systems/${detail.value.id}/members`)
    members.value = m.members || []
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'Invite failed'
  } finally {
    memberBusy.value = false
  }
}

async function setRole(m: MemberRow, role: string) {
  if (!detail.value) return
  try {
    await api.put(`/systems/${detail.value.id}/members/${m.user_id}`, { role })
    const res = await api.get<any>(`/systems/${detail.value.id}/members`)
    members.value = res.members || []
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'Role change failed'
  }
}

async function kick(m: MemberRow) {
  if (!detail.value) return
  try {
    await api.delete(`/systems/${detail.value.id}/members/${m.user_id}`)
    const res = await api.get<any>(`/systems/${detail.value.id}/members`)
    members.value = res.members || []
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'Remove failed'
  }
}

// ---- dependency map geometry: deterministic circular layout ----
const depViewBox = '0 0 440 440'
const nodePos = computed(() => {
  const pos: Record<string, { x: number; y: number }> = {}
  const nodes = depGraph.value?.nodes || []
  const R = nodes.length <= 1 ? 0 : 150
  const cx = 220, cy = 220
  nodes.forEach((n, i) => {
    const a = (2 * Math.PI * i) / Math.max(nodes.length, 1) - Math.PI / 2
    pos[n.id] = { x: cx + R * Math.cos(a), y: cy + R * Math.sin(a) }
  })
  return pos
})

const boundIds = computed(() => {
  const ids = new Set<string>()
  for (const k of KINDS) for (const c of detail.value?.grouped[k] || []) ids.add(c.ref_id)
  return ids
})

onMounted(async () => {
  await Promise.all([loadSystems(), loadTemplates(), loadDeps()])
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

        <!-- ============ v62: role templates ============ -->
        <div v-if="!detail" class="mb-8">
          <div class="mb-3 flex flex-wrap items-center gap-2">
            <Sparkles class="h-3.5 w-3.5 text-amber-300" />
            <h2 class="text-xs font-bold uppercase tracking-wide text-zinc-400">Start from your role</h2>
            <div class="ml-auto flex flex-wrap gap-1.5">
              <button
                class="rounded-full px-2.5 py-1 text-[10px] font-bold uppercase transition"
                :class="!roleFilter ? 'bg-orange-500/20 text-orange-300' : 'bg-zinc-800/80 text-zinc-400 hover:text-zinc-200'"
                @click="roleFilter = ''; loadTemplates()"
              >all</button>
              <button
                v-for="(meta, role) in ROLE_META" :key="role"
                class="rounded-full px-2.5 py-1 text-[10px] font-bold uppercase transition"
                :class="roleFilter === role ? 'bg-orange-500/20 text-orange-300' : 'bg-zinc-800/80 text-zinc-400 hover:text-zinc-200'"
                @click="roleFilter = role; loadTemplates()"
              >{{ meta.label }}</button>
            </div>
          </div>
          <div class="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
            <div v-for="t in templates" :key="t.slug" class="flex flex-col rounded-2xl border border-zinc-800/80 bg-zinc-900/50 p-4">
              <div class="flex items-start gap-2.5">
                <div class="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg" :style="{ background: `linear-gradient(135deg, ${t.color}, ${t.color}55)` }">
                  <Boxes class="h-4 w-4 text-zinc-950" />
                </div>
                <div class="min-w-0">
                  <p class="text-xs font-bold leading-tight">{{ t.name }}</p>
                  <span class="text-[9px] font-bold uppercase tracking-wide" :style="{ color: t.color }">{{ ROLE_META[t.role]?.label || t.role }}</span>
                </div>
              </div>
              <p class="mt-2 text-[10px] leading-relaxed text-zinc-500">{{ t.tagline }}</p>
              <ul class="mt-2 flex-1 space-y-1">
                <li v-for="o in t.outcomes" :key="o" class="flex items-start gap-1.5 text-[10px] text-zinc-400">
                  <CheckCircle2 class="mt-0.5 h-2.5 w-2.5 shrink-0 text-emerald-400" /> {{ o }}
                </li>
              </ul>
              <button
                class="mt-3 flex items-center justify-center gap-1.5 rounded-xl bg-zinc-800 px-3 py-2 text-[11px] font-bold text-zinc-100 transition hover:bg-orange-500 hover:text-white disabled:opacity-50"
                :disabled="instantiating === t.slug"
                @click="instantiate(t.slug)"
              >
                <Loader2 v-if="instantiating === t.slug" class="h-3 w-3 animate-spin" /> Create this system
              </button>
            </div>
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
                <span v-if="s.my_role && s.my_role !== 'owner'" class="ml-1 inline-block rounded-full bg-zinc-800 px-2 py-0.5 text-[9px] font-bold uppercase text-zinc-400">{{ s.my_role }}</span>
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
            No systems yet - pick a role template above, or install a Marketplace solution with "as system".
          </p>
        </div>

        <!-- ============ v62: dependency map ============ -->
        <div v-if="!detail" class="mt-8">
          <div class="mb-3 flex flex-wrap items-center gap-2">
            <Share2 class="h-3.5 w-3.5 text-sky-300" />
            <h2 class="text-xs font-bold uppercase tracking-wide text-zinc-400">Cross-system dependencies</h2>
            <span class="text-[10px] text-zinc-600">derived from live bindings - never stored</span>
            <button class="ml-auto rounded-lg border border-zinc-800 p-1.5 text-zinc-500 transition hover:text-zinc-200" title="Refresh" @click="loadDeps">
              <RefreshCw class="h-3 w-3" :class="depLoading ? 'animate-spin' : ''" />
            </button>
          </div>
          <div class="grid gap-4 lg:grid-cols-[440px_1fr]">
            <div class="rounded-2xl border border-zinc-800/80 bg-zinc-900/50 p-4">
              <div v-if="depLoading" class="grid h-[380px] place-items-center text-zinc-600"><Loader2 class="h-5 w-5 animate-spin" /></div>
              <svg v-else-if="depGraph && depGraph.nodes.length" :viewBox="depViewBox" class="mx-auto max-w-full">
                <defs>
                  <marker v-for="(st, t) in EDGE_STYLE" :id="`arrow-${t}`" :key="t" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse">
                    <path d="M 0 0 L 10 5 L 0 10 z" :fill="st.color" />
                  </marker>
                </defs>
                <g v-for="e in depGraph.edges" :key="`${e.from}-${e.to}-${e.type}`" class="cursor-pointer" @click="selectedEdge = e">
                  <line
                    :x1="nodePos[e.from]?.x" :y1="nodePos[e.from]?.y"
                    :x2="nodePos[e.to]?.x" :y2="nodePos[e.to]?.y"
                    :stroke="EDGE_STYLE[e.type]?.color" stroke-width="1.5"
                    :stroke-opacity="selectedEdge === e ? 0.95 : 0.4"
                    :stroke-dasharray="e.type === 'shared_object' ? '4 3' : undefined"
                    :marker-end="e.type === 'shared_object' ? undefined : `url(#arrow-${e.type})`"
                  />
                </g>
                <g v-for="n in depGraph.nodes" :key="n.id">
                  <circle :cx="nodePos[n.id]?.x" :cy="nodePos[n.id]?.y" r="26"
                          :fill="n.color" fill-opacity="0.2" :stroke="n.color" stroke-width="1.5" />
                  <text :x="nodePos[n.id]?.x" :y="nodePos[n.id]?.y + 3" text-anchor="middle" class="fill-zinc-100" style="font-size: 9px; font-weight: 700">
                    {{ n.name.length > 10 ? n.name.slice(0, 9) + '…' : n.name }}
                  </text>
                  <circle v-if="n.verdict === 'degraded' || n.verdict === 'unhealthy'" :cx="nodePos[n.id]?.x + 20" :cy="nodePos[n.id]?.y - 20" r="5" :fill="n.verdict === 'unhealthy' ? '#f43f5e' : '#f59e0b'" />
                </g>
              </svg>
              <p v-else class="grid h-[380px] place-items-center text-xs text-zinc-600">
                No systems to map yet - dependencies appear as systems share objects and data.
              </p>
            </div>
            <div class="rounded-2xl border border-zinc-800/80 bg-zinc-900/50 p-4">
              <div class="mb-3 flex flex-wrap gap-2">
                <span v-for="(st, t) in EDGE_STYLE" :key="t" class="flex items-center gap-1.5 rounded-full bg-zinc-800/70 px-2.5 py-1 text-[10px]">
                  <span class="h-2 w-2 rounded-full" :style="{ background: st.color }" /> {{ st.label }}
                  <span class="font-bold text-zinc-500">{{ depGraph?.summary.by_type[t] ?? 0 }}</span>
                </span>
              </div>
              <template v-if="selectedEdge">
                <p class="text-[11px] font-bold">
                  {{ depGraph?.nodes.find(n => n.id === selectedEdge.from)?.name }}
                  <span class="text-zinc-500">→</span>
                  {{ depGraph?.nodes.find(n => n.id === selectedEdge.to)?.name }}
                  <span class="ml-1 rounded-full px-2 py-0.5 text-[9px] font-bold uppercase" :style="{ background: `${EDGE_STYLE[selectedEdge.type]?.color}22`, color: EDGE_STYLE[selectedEdge.type]?.color }">
                    {{ EDGE_STYLE[selectedEdge.type]?.label }}
                  </span>
                </p>
                <div class="mt-2 space-y-1.5">
                  <div v-for="(ev, i) in selectedEdge.evidence" :key="i" class="rounded-xl border border-zinc-800 bg-zinc-950/60 px-3 py-2 text-[10px] text-zinc-400">
                    <template v-if="ev.kind"><span class="font-bold text-zinc-200">{{ ev.name }}</span> <span class="text-zinc-600">({{ ev.kind }})</span> is bound to both systems<template v-if="ev.via"> <span class="text-indigo-300">via model system {{ ev.via }}</span></template></template>
                    <template v-else-if="ev.dataset"><span class="font-bold text-zinc-200">{{ ev.workflow }}</span> {{ ev.direction === 'write' ? 'writes' : 'reads' }} <span class="font-bold text-zinc-200">{{ ev.dataset }}</span><template v-if="ev.via"> <span class="text-indigo-300">via {{ ev.via }}</span></template></template>
                    <template v-else-if="ev.model"><span class="font-bold text-zinc-200">{{ ev.workflow }}</span> scores with <span class="font-bold text-zinc-200">{{ ev.model }}</span><template v-if="ev.via"> <span class="text-indigo-300">via {{ ev.via }}</span></template></template>
                    <template v-else>{{ JSON.stringify(ev) }}</template>
                  </div>
                </div>
              </template>
              <p v-else class="text-[11px] text-zinc-600">Click an edge to see the evidence: which shared objects, dataset reads/writes or model scoring connect the two systems.</p>
            </div>
          </div>
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
            <span v-if="detail.my_role" class="flex items-center gap-1 rounded-full bg-zinc-800 px-2.5 py-1 text-[10px] font-bold uppercase text-zinc-300">
              <ShieldCheck class="h-3 w-3" /> you are {{ detail.my_role }}
            </span>
            <span class="rounded-full px-2.5 py-1 text-[10px] font-bold uppercase" :class="verdictMeta[detail.health.verdict]?.chip">{{ verdictMeta[detail.health.verdict]?.label }}</span>
            <button v-if="isOwner" class="rounded-xl border border-rose-500/30 bg-rose-500/5 p-2 text-rose-300 transition hover:bg-rose-500/15" title="Dissolve system (members stay)" @click="dissolve(detail)">
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

          <div class="grid gap-5 lg:grid-cols-[1fr_340px]">
            <!-- grouped components -->
            <div class="space-y-4">
              <div v-for="k in KINDS" :key="k">
                <div class="mb-2 flex items-center gap-2">
                  <component :is="KIND_META[k].icon" class="h-3.5 w-3.5" :class="KIND_META[k].color" />
                  <h3 class="text-xs font-bold">{{ KIND_META[k].label }}</h3>
                  <button v-if="canEdit" class="ml-auto flex items-center gap-1 rounded-lg border border-zinc-800 px-2 py-1 text-[10px] text-zinc-400 transition hover:border-orange-500/40 hover:text-orange-300" @click="openAttach(k)">
                    <Plus class="h-3 w-3" /> bind
                  </button>
                </div>
                <div v-if="(detail.grouped[k] || []).length" class="space-y-1.5">
                  <div v-for="c in detail.grouped[k]" :key="c.component_id" class="flex items-center gap-2 rounded-xl border border-zinc-800/80 bg-zinc-900/50 px-3 py-2">
                    <CheckCircle2 class="h-3 w-3 shrink-0 text-emerald-400" />
                    <NuxtLink :to="KIND_META[k].ref(c.ref_id)" class="min-w-0 flex-1 truncate text-xs font-semibold text-zinc-200 hover:text-orange-300">{{ c.name }}</NuxtLink>
                    <span class="text-[10px] text-zinc-600">added {{ new Date(c.added_at).toLocaleDateString() }}</span>
                    <button v-if="canEdit" class="rounded-lg p-1 text-zinc-600 transition hover:bg-zinc-800 hover:text-rose-300" title="Unbind" @click="detach(c.component_id)">
                      <Unlink class="h-3 w-3" />
                    </button>
                  </div>
                </div>
                <p v-else class="rounded-xl border border-dashed border-zinc-800 px-3 py-3 text-center text-[10px] text-zinc-600">none bound</p>
              </div>
            </div>

            <!-- ============ v62: team roster ============ -->
            <div class="rounded-2xl border border-zinc-800/80 bg-zinc-900/50 p-4">
              <div class="mb-3 flex items-center gap-2">
                <Users class="h-3.5 w-3.5 text-sky-300" />
                <h3 class="text-xs font-bold">Team</h3>
                <span class="text-[10px] text-zinc-600">owner / editor / viewer</span>
              </div>
              <div class="space-y-1.5">
                <div v-for="m in members" :key="m.user_id" class="flex items-center gap-2 rounded-xl border border-zinc-800 bg-zinc-950/60 px-3 py-2">
                  <div class="min-w-0 flex-1">
                    <p class="truncate text-[11px] font-semibold text-zinc-200">{{ m.name || m.email || m.user_id.slice(0, 8) }}</p>
                    <p class="truncate text-[9px] text-zinc-600">{{ m.email }}</p>
                  </div>
                  <span class="rounded-full px-2 py-0.5 text-[9px] font-bold uppercase"
                        :class="m.role === 'owner' ? 'bg-orange-500/20 text-orange-300' : m.role === 'editor' ? 'bg-sky-500/20 text-sky-300' : 'bg-zinc-700/40 text-zinc-400'">
                    {{ m.role }}
                  </span>
                  <template v-if="isOwner && !m.is_owner">
                    <button class="rounded-lg border border-zinc-800 px-1.5 py-0.5 text-[9px] text-zinc-400 transition hover:text-zinc-100"
                            :title="m.role === 'viewer' ? 'Make editor' : 'Make viewer'"
                            @click="setRole(m, m.role === 'viewer' ? 'editor' : 'viewer')">
                      {{ m.role === 'viewer' ? '→ editor' : '→ viewer' }}
                    </button>
                    <button class="rounded-lg p-1 text-zinc-600 transition hover:text-rose-300" title="Remove" @click="kick(m)">
                      <X class="h-3 w-3" />
                    </button>
                  </template>
                </div>
              </div>
              <div v-if="isOwner" class="mt-3 border-t border-zinc-800 pt-3">
                <p class="mb-2 flex items-center gap-1.5 text-[10px] font-bold uppercase tracking-wide text-zinc-500"><UserPlus class="h-3 w-3" /> invite</p>
                <input v-model="inviteEmail" type="email" placeholder="teammate@company.com"
                       class="w-full rounded-xl border border-zinc-800 bg-zinc-950 px-3 py-2 text-xs text-zinc-200 outline-none focus:border-orange-500/60" />
                <div class="mt-2 flex gap-2">
                  <select v-model="inviteRole" class="flex-1 rounded-xl border border-zinc-800 bg-zinc-950 px-2 py-2 text-xs text-zinc-300 outline-none">
                    <option value="viewer">viewer - read only</option>
                    <option value="editor">editor - bind + edit</option>
                  </select>
                  <button class="rounded-xl bg-orange-500 px-3 py-2 text-xs font-bold text-white transition hover:bg-orange-400 disabled:opacity-50"
                          :disabled="memberBusy || !inviteEmail.trim()" @click="invite">Add</button>
                </div>
                <p class="mt-2 text-[9px] leading-relaxed text-zinc-600">Viewers read the system, its health and the dependency map. Editors also bind components and edit metadata. Ownership stays with you.</p>
              </div>
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

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import {
  Loader2, BrainCircuit, Plus, CheckCircle2, XCircle, AlertTriangle, X,
  Workflow as WorkflowIcon, Database, Network, FileBarChart, Unlink,
  Trash2, Layers, Radio, Repeat, Gauge, Sparkles, Languages, Play,
} from 'lucide-vue-next'
import { useApi } from '~/composables/useApi'

// v63: Model Systems - the AI model-building operating unit. Where a
// system runs a part of the business, a model system BUILDS AND OPERATES
// A MODEL: datasets in, training out, evaluation, registry, deployment,
// monitoring, retraining. Membership is curated; every section the model
// system reports is derived from the member objects at read time.

interface ModelSystemCard {
  id: string; name: string; description: string; icon: string; color: string
  modalities: string[]
  components: Record<string, number>; total_components: number
  verdict: string; created_at: string | null
}
interface Detail extends ModelSystemCard {
  datasets: { id: string; name: string; rows: number; columns: (string | null)[] }[]
  training: {
    classical_versions: number; neural_versions: number; fine_tuned_versions: number
    language_versions?: number; continued_pretrained_versions?: number
    distinct_models: number; total_versions: number
    latest: { id: string; name: string; version: number; algorithm: string; family: string; task: string; active: boolean; metrics: Record<string, any>; fine_tuned_from: string | null }[]
  }
  modalities: { declared: string[]; evidence: string[]; capabilities: { modality: string; available: boolean; extractor?: string; note?: string }[] }
  composition: { id: string; name: string; chain_length: number }[]
  evaluation: { model: string; version: number; family: string; task: string; metrics: Record<string, any> }[]
  registry: { id: string; name: string; version: number; algorithm: string; family: string; active: boolean }[]
  deployment: { id: string; name: string; models_scored: number; active: boolean }[]
  monitoring: { versions: number; with_reference_stats: number; coverage_pct: number; drift_capable: boolean }
  retraining: { id: string; name: string; trainer: string[]; schedule: string; active: boolean }[]
  reports: { id: string; name: string; cron: string; fmt: string; enabled: boolean }[]
  lifecycle: {
    stages: { position: number; stage: string; workflow_id: string; workflow_name: string; active: boolean }[]
    skipped: { workflow_id: string; workflow_name: string; reason: string }[]
    lm_workflows: number
    sequence: string
  }
  health: {
    verdict: string
    workflows: { bound: number; runs_7d: number; failures_7d: number; failure_rate_7d: number }
    datasets: { total: number; healthy: number; degraded: number; unhealthy: number; unscored: number }
    models: { bound: number; active: number; with_reference_stats: number }
    reports: { bound: number; ok_7d: number; error_7d: number }
  }
}

const { api } = useApi()
const loading = ref(true)
const pageError = ref('')
const systems = ref<ModelSystemCard[]>([])
const detail = ref<Detail | null>(null)

const showCreate = ref(false)
const newName = ref('')
const newDesc = ref('')
const newModalities = ref<string[]>(['tabular'])
const creating = ref(false)

const showAttach = ref(false)
const attachKind = ref('dataset')
const attachCandidates = ref<any[]>([])
const attaching = ref(false)

// v65: the LM lifecycle runner - one click runs pretrain -> continue ->
// generate IN SEQUENCE through the real engine and reports what the model
// produced at every stage.
const lcRunning = ref(false)
const lcResult = ref<any>(null)
const stageChip: Record<string, string> = {
  pretrain: 'bg-violet-500/20 text-violet-300',
  continue: 'bg-purple-500/20 text-purple-300',
  generate: 'bg-fuchsia-500/20 text-fuchsia-300',
}

const MODALITY_OPTIONS = ['tabular', 'text', 'image', 'audio', 'document', 'video', 'multimodal']
const KIND_META: Record<string, { label: string; icon: any; color: string; endpoint: string }> = {
  dataset: { label: 'Datasets', icon: Database, color: 'text-lime-400', endpoint: '/datasets' },
  model: { label: 'Models', icon: Network, color: 'text-pink-400', endpoint: '/models' },
  workflow: { label: 'Workflows', icon: WorkflowIcon, color: 'text-orange-400', endpoint: '/workflows' },
  report: { label: 'Reports', icon: FileBarChart, color: 'text-cyan-400', endpoint: '/reports' },
}
const KINDS = Object.keys(KIND_META)

const verdictMeta: Record<string, { chip: string; label: string }> = {
  healthy: { chip: 'bg-emerald-500/15 text-emerald-300', label: 'healthy' },
  degraded: { chip: 'bg-amber-500/15 text-amber-300', label: 'degraded' },
  unhealthy: { chip: 'bg-rose-500/15 text-rose-300', label: 'unhealthy' },
}
const familyChip: Record<string, string> = {
  neural: 'bg-indigo-500/20 text-indigo-300',
  classical: 'bg-zinc-700/40 text-zinc-300',
  language: 'bg-fuchsia-500/20 text-fuchsia-300',
}

const metricLabel: Record<string, string> = {
  accuracy: 'acc', f1_weighted: 'f1', r2: 'R2', mae: 'MAE', rmse: 'RMSE',
  architecture: 'arch', params_count: 'params', epochs_run: 'epochs',
}

async function load() {
  try {
    systems.value = await api.get<ModelSystemCard[]>('/model-systems')
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'Could not load model systems'
  }
}

async function openDetail(id: string) {
  try {
    detail.value = await api.get<Detail>(`/model-systems/${id}`)
    lcResult.value = null
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'Could not load the model system'
  }
}

async function runLifecycle() {
  if (!detail.value || lcRunning.value) return
  lcRunning.value = true
  lcResult.value = null
  try {
    lcResult.value = await api.post<any>(`/model-systems/${detail.value.id}/run-lifecycle`, {})
    await openDetailKeepResult(detail.value.id)
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'Lifecycle run failed'
  } finally {
    lcRunning.value = false
  }
}

async function openDetailKeepResult(id: string) {
  try {
    detail.value = await api.get<Detail>(`/model-systems/${id}`)
  } catch { /* keep the previous detail on refresh failure */ }
}

function toggleModality(m: string) {
  if (newModalities.value.includes(m)) newModalities.value = newModalities.value.filter(x => x !== m)
  else newModalities.value.push(m)
}

async function create() {
  if (!newName.value.trim()) return
  creating.value = true
  try {
    const created = await api.post<any>('/model-systems', {
      name: newName.value.trim(), description: newDesc.value.trim(), modalities: newModalities.value,
    })
    showCreate.value = false
    newName.value = ''
    newDesc.value = ''
    newModalities.value = ['tabular']
    await load()
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
  attachCandidates.value = []
  try {
    const res = await api.get<any>(KIND_META[kind].endpoint)
    attachCandidates.value = Array.isArray(res) ? res : (res.items || res.models || res.workflows || res.datasets || res.reports || [])
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'Could not load candidates'
  }
}

async function attach(refId: string) {
  if (!detail.value) return
  attaching.value = true
  try {
    await api.post(`/model-systems/${detail.value.id}/components`, { kind: attachKind.value, ref_id: refId })
    showAttach.value = false
    await openDetail(detail.value.id)
    await load()
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'Attach failed'
  } finally {
    attaching.value = false
  }
}

function sectionFor(k: string): { id: string; label: string; component_id: string | null }[] {
  if (!detail.value) return []
  if (k === 'dataset') return detail.value.datasets.map(d => ({ id: d.id, label: d.name, component_id: (d as any).component_id }))
  if (k === 'model') return detail.value.registry.map(m => ({ id: m.id, label: `${m.name} v${m.version} · ${m.algorithm}`, component_id: (m as any).component_id }))
  if (k === 'report') return detail.value.reports.map(r => ({ id: r.id, label: `${r.name} · ${r.cron} · ${r.fmt}`, component_id: (r as any).component_id }))
  // workflows: merge deployment + composition + retraining (unique by id)
  const seen = new Map<string, { id: string; label: string; component_id: string | null }>()
  for (const d of detail.value.deployment) seen.set(d.id, { id: d.id, label: d.name, component_id: (d as any).component_id })
  for (const c of detail.value.composition) if (!seen.has(c.id)) seen.set(c.id, { id: c.id, label: c.name, component_id: (c as any).component_id })
  for (const r of detail.value.retraining) if (!seen.has(r.id)) seen.set(r.id, { id: r.id, label: r.name, component_id: (r as any).component_id })
  return [...seen.values()]
}

async function detachMember(componentId: string | null) {
  if (!detail.value || !componentId) return
  try {
    await api.delete(`/model-systems/${detail.value.id}/components/${componentId}`)
    await openDetail(detail.value.id)
    await load()
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'Detach failed'
  }
}

async function dissolve(s: ModelSystemCard) {
  if (!confirm(`Dissolve "${s.name}"? The datasets, models and workflows are NOT deleted.`)) return
  try {
    await api.delete(`/model-systems/${s.id}`)
    if (detail.value?.id === s.id) detail.value = null
    await load()
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'Dissolve failed'
  }
}

const boundIds = computed(() => new Set<string>())
void boundIds

onMounted(async () => {
  await load()
  loading.value = false
})
</script>

<template>
  <div class="min-h-screen text-zinc-100">
    <header class="sticky top-0 z-20 border-b border-zinc-800/80 bg-zinc-950/90 backdrop-blur">
      <div class="mx-auto max-w-7xl px-4 py-3.5 sm:px-6">
        <div class="flex flex-wrap items-center gap-3">
          <div class="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-indigo-500 to-violet-500 shadow-lg shadow-indigo-500/20">
            <BrainCircuit class="h-4 w-4 text-white" />
          </div>
          <div class="min-w-0 flex-1">
            <h1 class="text-lg font-bold tracking-tight">Model Systems</h1>
            <p class="-mt-0.5 text-[11px] text-zinc-500">Dataset → training → evaluation → registry → deployment → monitoring - one health-scored unit</p>
          </div>
          <button
            class="flex items-center gap-1.5 rounded-xl bg-indigo-500 px-3.5 py-2 text-xs font-bold text-white transition hover:bg-indigo-400"
            @click="showCreate = true"
          >
            <Plus class="h-3.5 w-3.5" /> New model system
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
        <div v-if="showCreate" class="mb-5 rounded-2xl border border-indigo-500/30 bg-indigo-500/5 p-5">
          <h2 class="text-sm font-bold">Name your model system</h2>
          <p class="mt-0.5 text-[11px] text-zinc-500">e.g. "Churn Vision", "Support Triage Models" - the unit that builds and operates one model family.</p>
          <input
            v-model="newName"
            class="mt-3 w-full max-w-md rounded-xl border border-zinc-800 bg-zinc-950 px-3 py-2 text-sm text-zinc-200 outline-none focus:border-indigo-500/60"
            placeholder="Churn Vision"
          />
          <textarea
            v-model="newDesc"
            rows="2"
            class="mt-2 w-full max-w-2xl resize-y rounded-xl border border-zinc-800 bg-zinc-950 px-3 py-2 text-xs text-zinc-300 outline-none focus:border-indigo-500/60"
            placeholder="What does this model system build and operate?"
          />
          <div class="mt-3 flex flex-wrap gap-1.5">
            <button
              v-for="m in MODALITY_OPTIONS" :key="m"
              class="rounded-full px-2.5 py-1 text-[10px] font-bold uppercase transition"
              :class="newModalities.includes(m) ? 'bg-indigo-500/25 text-indigo-200' : 'bg-zinc-800/80 text-zinc-400 hover:text-zinc-200'"
              @click="toggleModality(m)"
            >{{ m }}</button>
          </div>
          <div class="mt-3 flex gap-2">
            <button class="rounded-xl bg-indigo-500 px-4 py-2 text-xs font-bold text-white transition hover:bg-indigo-400 disabled:opacity-50" :disabled="creating || !newName.trim()" @click="create">
              <Loader2 v-if="creating" class="mr-1 inline h-3 w-3 animate-spin" /> Create
            </button>
            <button class="rounded-xl border border-zinc-800 px-4 py-2 text-xs text-zinc-400 transition hover:text-zinc-200" @click="showCreate = false">Cancel</button>
          </div>
        </div>

        <!-- cards -->
        <div v-if="!detail" class="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          <button
            v-for="s in systems" :key="s.id"
            class="rounded-2xl border border-zinc-800/80 bg-zinc-900/50 p-5 text-left transition hover:border-indigo-500/40"
            @click="openDetail(s.id)"
          >
            <div class="flex items-start gap-3">
              <div class="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl" :style="{ background: `linear-gradient(135deg, ${s.color}, ${s.color}55)` }">
                <BrainCircuit class="h-5 w-5 text-zinc-950" />
              </div>
              <div class="min-w-0 flex-1">
                <p class="text-sm font-bold leading-tight">{{ s.name }}</p>
                <span class="mt-1 inline-block rounded-full px-2 py-0.5 text-[9px] font-bold uppercase" :class="verdictMeta[s.verdict]?.chip">{{ verdictMeta[s.verdict]?.label || s.verdict }}</span>
              </div>
            </div>
            <p class="mt-2 line-clamp-2 text-[11px] text-zinc-500">{{ s.description || 'no description' }}</p>
            <div class="mt-3 flex flex-wrap gap-1.5 text-[10px]">
              <span v-for="m in s.modalities" :key="m" class="rounded-full bg-indigo-500/15 px-2 py-0.5 text-indigo-300">{{ m }}</span>
              <template v-for="k in KINDS" :key="k">
                <span v-if="s.components[k]" class="rounded-full bg-zinc-800 px-2 py-0.5 text-zinc-300">{{ s.components[k] }} {{ KIND_META[k].label.toLowerCase() }}</span>
              </template>
              <span v-if="!s.total_components" class="rounded-full bg-zinc-800 px-2 py-0.5 text-zinc-500">empty - open to bind</span>
            </div>
          </button>
          <p v-if="!systems.length" class="col-span-full rounded-2xl border border-dashed border-zinc-800 py-12 text-center text-xs text-zinc-600">
            No model systems yet - create one, train a model through a workflow, and bind it here.
          </p>
        </div>

        <!-- detail -->
        <div v-else>
          <div class="mb-4 flex flex-wrap items-center gap-3">
            <button class="rounded-lg p-1.5 text-zinc-500 transition hover:bg-zinc-800 hover:text-zinc-200" title="Back" @click="detail = null; load()">
              <X class="h-4 w-4" />
            </button>
            <div class="flex h-10 w-10 items-center justify-center rounded-xl" :style="{ background: `linear-gradient(135deg, ${detail.color}, ${detail.color}55)` }">
              <BrainCircuit class="h-5 w-5 text-zinc-950" />
            </div>
            <div class="min-w-0 flex-1">
              <h2 class="text-base font-bold">{{ detail.name }}</h2>
              <p class="text-[11px] text-zinc-500">{{ detail.description || 'no description' }}</p>
            </div>
            <span class="rounded-full px-2.5 py-1 text-[10px] font-bold uppercase" :class="verdictMeta[detail.health.verdict]?.chip">{{ verdictMeta[detail.health.verdict]?.label }}</span>
            <button class="rounded-xl border border-rose-500/30 bg-rose-500/5 p-2 text-rose-300 transition hover:bg-rose-500/15" title="Dissolve (members stay)" @click="dissolve(detail)">
              <Trash2 class="h-3.5 w-3.5" />
            </button>
          </div>

          <!-- health strip -->
          <div class="mb-5 grid gap-3 sm:grid-cols-4">
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
                <span class="text-rose-300">{{ detail.health.datasets.unhealthy }}</span>
              </p>
            </div>
            <div class="rounded-2xl border border-zinc-800/80 bg-zinc-900/50 p-4">
              <p class="text-[10px] uppercase tracking-wide text-zinc-500">Models</p>
              <p class="mt-1 text-xl font-bold">{{ detail.health.models.active }} <span class="text-xs font-normal text-zinc-500">active of {{ detail.health.models.bound }}</span></p>
              <p class="text-[10px]" :class="detail.monitoring.drift_capable ? 'text-emerald-300' : 'text-amber-300'">
                {{ detail.monitoring.coverage_pct }}% drift-monitored
              </p>
            </div>
            <div class="rounded-2xl border border-zinc-800/80 bg-zinc-900/50 p-4">
              <p class="text-[10px] uppercase tracking-wide text-zinc-500">Report deliveries (7d)</p>
              <p class="mt-1 text-xl font-bold">{{ detail.health.reports.ok_7d }} <span class="text-xs font-normal text-emerald-400">ok</span></p>
              <p class="text-[10px]" :class="detail.health.reports.error_7d ? 'text-rose-300' : 'text-zinc-500'">{{ detail.health.reports.error_7d }} errors</p>
            </div>
          </div>

          <!-- modality banner -->
          <div class="mb-5 rounded-2xl border border-indigo-500/25 bg-indigo-500/5 p-4">
            <div class="flex flex-wrap items-center gap-2">
              <Sparkles class="h-3.5 w-3.5 text-indigo-300" />
              <h3 class="text-xs font-bold text-indigo-200">Modalities</h3>
              <span class="text-[10px] text-zinc-500">declared + derived from bound pipelines</span>
              <div class="ml-auto flex flex-wrap gap-1.5">
                <span v-for="m in detail.modalities.declared" :key="m" class="rounded-full bg-indigo-500/20 px-2 py-0.5 text-[10px] font-bold text-indigo-200">{{ m }}</span>
              </div>
            </div>
            <div class="mt-2 flex flex-wrap items-center gap-1.5 text-[10px]">
              <span class="text-zinc-500">evidence in bound workflows:</span>
              <span v-for="e in detail.modalities.evidence" :key="e" class="rounded-full bg-zinc-800 px-2 py-0.5 text-zinc-300">{{ e }}</span>
              <span v-if="!detail.modalities.evidence.length" class="text-zinc-600">none yet - add a text_features / image_features / audio_features node</span>
            </div>
            <div v-if="detail.modalities.capabilities.length" class="mt-2 grid gap-1 sm:grid-cols-2">
              <div v-for="c in detail.modalities.capabilities" :key="c.modality" class="flex items-center gap-1.5 text-[10px]">
                <CheckCircle2 v-if="c.available" class="h-3 w-3 shrink-0 text-emerald-400" />
                <XCircle v-else class="h-3 w-3 shrink-0 text-rose-400" />
                <span class="font-bold">{{ c.modality }}</span>
                <span class="truncate text-zinc-500" :title="c.extractor || c.note">{{ c.extractor || c.note }}</span>
              </div>
            </div>
          </div>

          <!-- v65: language-model lifecycle -->
          <div v-if="detail.lifecycle?.stages?.length" class="mb-5 rounded-2xl border border-fuchsia-500/25 bg-fuchsia-500/5 p-4">
            <div class="flex flex-wrap items-center gap-2">
              <Languages class="h-3.5 w-3.5 text-fuchsia-300" />
              <h3 class="text-xs font-bold text-fuchsia-200">Language model lifecycle</h3>
              <span class="text-[10px] text-zinc-500">derived from the bound graphs - runs in this order</span>
              <button
                class="ml-auto flex items-center gap-1.5 rounded-xl bg-fuchsia-500 px-3.5 py-2 text-xs font-bold text-white transition hover:bg-fuchsia-400 disabled:opacity-50"
                :disabled="lcRunning"
                @click="runLifecycle"
              >
                <Loader2 v-if="lcRunning" class="h-3.5 w-3.5 animate-spin" />
                <Play v-else class="h-3.5 w-3.5" />
                {{ lcRunning ? 'Running the lifecycle...' : 'Run lifecycle' }}
              </button>
            </div>
            <div class="mt-2 flex flex-wrap items-center gap-1.5">
              <template v-for="(s, i) in detail.lifecycle.stages" :key="s.workflow_id">
                <NuxtLink :to="`/workflows/${s.workflow_id}`"
                          class="flex items-center gap-1.5 rounded-xl border border-zinc-800 bg-zinc-950/60 px-2.5 py-1.5 text-[11px] transition hover:border-fuchsia-500/40">
                  <span class="grid h-4 w-4 place-items-center rounded-full bg-fuchsia-500/25 text-[9px] font-bold text-fuchsia-200">{{ s.position }}</span>
                  <span class="rounded-full px-1.5 py-0.5 text-[9px] font-bold uppercase" :class="stageChip[s.stage] || 'bg-zinc-800 text-zinc-300'">{{ s.stage }}</span>
                  <span class="font-semibold">{{ s.workflow_name }}</span>
                </NuxtLink>
                <span v-if="i < detail.lifecycle.stages.length - 1" class="text-zinc-600">→</span>
              </template>
            </div>
            <p v-if="detail.lifecycle.skipped?.length" class="mt-2 text-[10px] text-zinc-600">
              not part of the LM lifecycle: {{ detail.lifecycle.skipped.map(s => s.workflow_name).join(', ') }}
            </p>

            <!-- lifecycle run results -->
            <div v-if="lcResult" class="mt-3 space-y-2">
              <div class="flex flex-wrap items-center gap-2 rounded-xl border border-zinc-800 bg-zinc-950/70 px-3 py-2 text-[11px]">
                <CheckCircle2 v-if="lcResult.summary?.stages_succeeded === lcResult.summary?.stages_total" class="h-3.5 w-3.5 text-emerald-400" />
                <AlertTriangle v-else class="h-3.5 w-3.5 text-amber-400" />
                <span class="font-bold">{{ lcResult.summary?.stages_succeeded }}/{{ lcResult.summary?.stages_total }} stages succeeded</span>
                <template v-if="lcResult.summary?.perplexity_chain?.length">
                  <span class="text-zinc-500">perplexity:</span>
                  <span v-for="(p, i) in lcResult.summary.perplexity_chain" :key="i" class="rounded-full bg-fuchsia-500/15 px-2 py-0.5 text-fuchsia-200">ppl {{ p }}</span>
                </template>
                <span v-if="lcResult.summary?.total_seconds != null" class="ml-auto text-zinc-500">{{ lcResult.summary.total_seconds }}s</span>
              </div>
              <div v-for="s in lcResult.stages" :key="s.execution_id || s.position"
                   class="rounded-xl border px-3 py-2 text-[11px]"
                   :class="s.status === 'success' ? 'border-zinc-800 bg-zinc-950/60' : s.status === 'not_run' ? 'border-dashed border-zinc-800 text-zinc-500' : 'border-rose-500/40 bg-rose-500/5'">
                <div class="flex flex-wrap items-center gap-2">
                  <span class="grid h-4 w-4 place-items-center rounded-full bg-fuchsia-500/25 text-[9px] font-bold text-fuchsia-200">{{ s.position }}</span>
                  <span class="rounded-full px-1.5 py-0.5 text-[9px] font-bold uppercase" :class="stageChip[s.stage] || 'bg-zinc-800 text-zinc-300'">{{ s.stage }}</span>
                  <span class="font-semibold">{{ s.workflow_name }}</span>
                  <CheckCircle2 v-if="s.status === 'success'" class="h-3 w-3 text-emerald-400" />
                  <XCircle v-else class="h-3 w-3 text-rose-400" />
                  <span v-if="s.duration_ms != null" class="ml-auto text-[10px] text-zinc-500">{{ (s.duration_ms / 1000).toFixed(1) }}s</span>
                </div>
                <div v-if="s.status === 'success'" class="mt-1 flex flex-wrap gap-2 text-[10px] text-zinc-500">
                  <span v-if="s.mode">{{ s.mode }}</span>
                  <span v-if="s.perplexity != null">perplexity: <span class="text-zinc-300">{{ s.perplexity }}</span></span>
                  <span v-if="s.vocabulary != null">vocab: <span class="text-zinc-300">{{ s.vocabulary }}</span></span>
                  <span v-if="s.tokenizer">tokenizer: <span class="text-zinc-300">{{ s.tokenizer }}</span></span>
                  <span v-if="s.continued_from" class="rounded-full bg-sky-500/15 px-1.5 py-0.5 text-sky-300">continued from {{ s.continued_from }}</span>
                  <span v-if="s.tokens_generated != null">tokens: <span class="text-zinc-300">{{ s.tokens_generated }}</span></span>
                </div>
                <p v-if="s.generated_text" class="mt-1 rounded-lg bg-zinc-900/80 px-2 py-1 font-mono text-[10px] text-fuchsia-200">"{{ s.generated_text }}"</p>
                <p v-if="s.error" class="mt-1 text-[10px] text-rose-300">{{ s.error }}</p>
              </div>
              <p v-if="lcResult.note" class="text-[10px] text-zinc-500">{{ lcResult.note }}</p>
            </div>
            <p v-else-if="!lcRunning" class="mt-2 text-[10px] text-zinc-600">one click runs every stage through the real engine: pretrain → continued pretraining → sample text from the registered model</p>
          </div>

          <div class="grid gap-5 lg:grid-cols-2">
            <!-- training -->
            <div class="rounded-2xl border border-zinc-800/80 bg-zinc-900/50 p-4">
              <div class="mb-3 flex items-center gap-2">
                <Layers class="h-3.5 w-3.5 text-indigo-300" />
                <h3 class="text-xs font-bold">Training</h3>
                <div class="ml-auto flex gap-1.5 text-[10px]">
                  <span class="rounded-full bg-zinc-800 px-2 py-0.5">{{ detail.training.classical_versions }} classical</span>
                  <span class="rounded-full bg-indigo-500/20 px-2 py-0.5 text-indigo-300">{{ detail.training.neural_versions }} neural</span>
                  <span v-if="detail.training.language_versions" class="rounded-full bg-fuchsia-500/20 px-2 py-0.5 text-fuchsia-300">{{ detail.training.language_versions }} language</span>
                  <span v-if="detail.training.fine_tuned_versions" class="rounded-full bg-sky-500/20 px-2 py-0.5 text-sky-300">{{ detail.training.fine_tuned_versions }} fine-tuned</span>
                  <span v-if="detail.training.continued_pretrained_versions" class="rounded-full bg-purple-500/20 px-2 py-0.5 text-purple-300">{{ detail.training.continued_pretrained_versions }} continued</span>
                </div>
              </div>
              <div v-if="detail.training.latest.length" class="space-y-1.5">
                <div v-for="m in detail.training.latest" :key="m.id" class="rounded-xl border border-zinc-800 bg-zinc-950/60 px-3 py-2">
                  <div class="flex flex-wrap items-center gap-2 text-[11px]">
                    <span class="font-bold">{{ m.name }} <span class="text-zinc-500">v{{ m.version }}</span></span>
                    <span class="rounded-full px-1.5 py-0.5 text-[9px] font-bold uppercase" :class="familyChip[m.family]">{{ m.family }}</span>
                    <span v-if="m.fine_tuned_from" class="rounded-full bg-sky-500/15 px-1.5 py-0.5 text-[9px] text-sky-300">from {{ m.fine_tuned_from }}</span>
                    <span v-if="m.active" class="rounded-full bg-emerald-500/15 px-1.5 py-0.5 text-[9px] text-emerald-300">active</span>
                    <span class="ml-auto text-zinc-500">{{ m.algorithm }}</span>
                  </div>
                  <div class="mt-1 flex flex-wrap gap-2 text-[10px]">
                    <span v-for="(v, k) in m.metrics" :key="k" class="text-zinc-500">{{ metricLabel[k as string] || k }}: <span class="text-zinc-300">{{ v }}</span></span>
                  </div>
                </div>
              </div>
              <p v-else class="rounded-xl border border-dashed border-zinc-800 px-3 py-3 text-center text-[10px] text-zinc-600">no models yet - run model_train or neural_train in a workflow</p>
            </div>

            <!-- evaluation + monitoring -->
            <div class="space-y-5">
              <div class="rounded-2xl border border-zinc-800/80 bg-zinc-900/50 p-4">
                <div class="mb-3 flex items-center gap-2">
                  <Gauge class="h-3.5 w-3.5 text-emerald-300" />
                  <h3 class="text-xs font-bold">Evaluation</h3>
                  <span class="ml-auto text-[10px] text-zinc-500">active versions</span>
                </div>
                <div v-if="detail.evaluation.length" class="space-y-1.5">
                  <div v-for="e in detail.evaluation" :key="`${e.model}-${e.version}`" class="flex flex-wrap items-center gap-2 rounded-xl border border-zinc-800 bg-zinc-950/60 px-3 py-2 text-[11px]">
                    <span class="font-bold">{{ e.model }} v{{ e.version }}</span>
                    <span class="text-zinc-500">{{ e.task }}</span>
                    <span v-for="(v, k) in e.metrics" :key="k" class="text-zinc-500">{{ metricLabel[k as string] || k }}: <span class="text-zinc-300">{{ v }}</span></span>
                  </div>
                </div>
                <p v-else class="rounded-xl border border-dashed border-zinc-800 px-3 py-3 text-center text-[10px] text-zinc-600">nothing active</p>
              </div>
              <div class="rounded-2xl border border-zinc-800/80 bg-zinc-900/50 p-4">
                <div class="mb-2 flex items-center gap-2">
                  <Radio class="h-3.5 w-3.5 text-amber-300" />
                  <h3 class="text-xs font-bold">Monitoring</h3>
                  <span class="ml-auto rounded-full px-2 py-0.5 text-[9px] font-bold uppercase" :class="detail.monitoring.drift_capable ? 'bg-emerald-500/15 text-emerald-300' : 'bg-amber-500/15 text-amber-300'">
                    {{ detail.monitoring.drift_capable ? 'drift-gate ready' : 'coverage gap' }}
                  </span>
                </div>
                <p class="text-[10px] text-zinc-500">{{ detail.monitoring.with_reference_stats }} of {{ detail.monitoring.versions }} versions carry reference stats - PSI drift checks and the /drift endpoint work against them.</p>
              </div>
            </div>

            <!-- deployment + composition + retraining -->
            <div class="rounded-2xl border border-zinc-800/80 bg-zinc-900/50 p-4">
              <div class="mb-3 flex items-center gap-2">
                <WorkflowIcon class="h-3.5 w-3.5 text-orange-300" />
                <h3 class="text-xs font-bold">Deployment & composition</h3>
              </div>
              <p class="mb-1 text-[10px] uppercase tracking-wide text-zinc-600">serving workflows (model_predict)</p>
              <div v-if="detail.deployment.length" class="space-y-1.5">
                <NuxtLink v-for="d in detail.deployment" :key="d.id" :to="`/workflows/${d.id}`"
                          class="flex items-center gap-2 rounded-xl border border-zinc-800 bg-zinc-950/60 px-3 py-2 text-[11px] transition hover:border-orange-500/40">
                  <span class="min-w-0 flex-1 truncate font-semibold">{{ d.name }}</span>
                  <span class="text-[10px] text-zinc-500">{{ d.models_scored }} model(s)</span>
                  <span class="rounded-full px-1.5 py-0.5 text-[9px]" :class="d.active ? 'bg-emerald-500/15 text-emerald-300' : 'bg-zinc-800 text-zinc-500'">{{ d.active ? 'active' : 'inactive' }}</span>
                </NuxtLink>
              </div>
              <p v-else class="rounded-xl border border-dashed border-zinc-800 px-3 py-2 text-center text-[10px] text-zinc-600">no serving surface yet</p>
              <p class="mb-1 mt-3 text-[10px] uppercase tracking-wide text-zinc-600">model chains (2+ predicts)</p>
              <div v-if="detail.composition.length" class="space-y-1.5">
                <NuxtLink v-for="c in detail.composition" :key="c.id" :to="`/workflows/${c.id}`"
                          class="flex items-center gap-2 rounded-xl border border-indigo-500/25 bg-indigo-500/5 px-3 py-2 text-[11px] transition hover:border-indigo-500/50">
                  <Layers class="h-3 w-3 text-indigo-300" />
                  <span class="min-w-0 flex-1 truncate font-semibold">{{ c.name }}</span>
                  <span class="text-[10px] text-indigo-300">chain of {{ c.chain_length }}</span>
                </NuxtLink>
              </div>
              <p v-else class="rounded-xl border border-dashed border-zinc-800 px-3 py-2 text-center text-[10px] text-zinc-600">single-model workflows only</p>
            </div>

            <div class="rounded-2xl border border-zinc-800/80 bg-zinc-900/50 p-4">
              <div class="mb-3 flex items-center gap-2">
                <Repeat class="h-3.5 w-3.5 text-sky-300" />
                <h3 class="text-xs font-bold">Retraining & reports</h3>
              </div>
              <p class="mb-1 text-[10px] uppercase tracking-wide text-zinc-600">training pipelines</p>
              <div v-if="detail.retraining.length" class="space-y-1.5">
                <NuxtLink v-for="r in detail.retraining" :key="r.id" :to="`/workflows/${r.id}`"
                          class="flex items-center gap-2 rounded-xl border border-zinc-800 bg-zinc-950/60 px-3 py-2 text-[11px] transition hover:border-sky-500/40">
                  <span class="min-w-0 flex-1 truncate font-semibold">{{ r.name }}</span>
                  <span class="rounded-full bg-zinc-800 px-1.5 py-0.5 text-[9px] text-zinc-400">{{ r.trainer.join(', ') }}</span>
                  <span class="text-[10px]" :class="r.schedule === 'manual' ? 'text-zinc-500' : 'text-sky-300'">{{ r.schedule }}</span>
                </NuxtLink>
              </div>
              <p v-else class="rounded-xl border border-dashed border-zinc-800 px-3 py-2 text-center text-[10px] text-zinc-600">no training pipeline bound</p>
              <p class="mb-1 mt-3 text-[10px] uppercase tracking-wide text-zinc-600">reports</p>
              <div v-if="detail.reports.length" class="space-y-1.5">
                <div v-for="r in detail.reports" :key="r.id" class="flex items-center gap-2 rounded-xl border border-zinc-800 bg-zinc-950/60 px-3 py-2 text-[11px]">
                  <span class="min-w-0 flex-1 truncate font-semibold">{{ r.name }}</span>
                  <span class="text-[10px] text-zinc-500">{{ r.cron }} · {{ r.fmt }}</span>
                </div>
              </div>
              <p v-else class="rounded-xl border border-dashed border-zinc-800 px-3 py-2 text-center text-[10px] text-zinc-600">no reports bound</p>
            </div>
          </div>

          <!-- bound members + bind/unbind -->
          <div class="mt-5 space-y-4">
            <div v-for="k in KINDS" :key="k">
              <div class="mb-2 flex items-center gap-2">
                <component :is="KIND_META[k].icon" class="h-3.5 w-3.5" :class="KIND_META[k].color" />
                <h3 class="text-xs font-bold">{{ KIND_META[k].label }}</h3>
                <button class="ml-auto flex items-center gap-1 rounded-lg border border-zinc-800 px-2 py-1 text-[10px] text-zinc-400 transition hover:border-indigo-500/40 hover:text-indigo-300" @click="openAttach(k)">
                  <Plus class="h-3 w-3" /> bind
                </button>
              </div>
              <div v-if="sectionFor(k).length" class="space-y-1.5">
                <div v-for="row in sectionFor(k)" :key="`${k}-${row.id}`" class="flex items-center gap-2 rounded-xl border border-zinc-800/80 bg-zinc-900/50 px-3 py-2">
                  <CheckCircle2 class="h-3 w-3 shrink-0 text-emerald-400" />
                  <span class="min-w-0 flex-1 truncate text-xs font-semibold text-zinc-200">{{ row.label }}</span>
                  <button class="rounded-lg p-1 text-zinc-600 transition hover:bg-zinc-800 hover:text-rose-300" title="Unbind" @click="detachMember(row.component_id)">
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
            <p v-if="!attachCandidates.length" class="py-8 text-center text-xs text-zinc-600">Nothing to bind - create one first.</p>
            <div v-else class="space-y-1.5">
              <button
                v-for="cand in attachCandidates" :key="cand.id"
                class="flex w-full items-center gap-2 rounded-xl border border-zinc-800 bg-zinc-950/60 px-3 py-2 text-left text-xs transition hover:border-indigo-500/40 disabled:opacity-40"
                :disabled="attaching"
                @click="attach(cand.id)"
              >
                <span class="min-w-0 flex-1 truncate font-semibold">{{ cand.name }}<span v-if="cand.version" class="text-zinc-500"> v{{ cand.version }}</span></span>
              </button>
            </div>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

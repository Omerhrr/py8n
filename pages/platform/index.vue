<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { Loader2, Globe, Boxes, Wand2, BrainCircuit, Rocket, Gauge, CheckCircle2, XCircle, Store, Database, Activity } from 'lucide-vue-next'
import { useApi } from '~/composables/useApi'

// v67: the Platform console - one derived answer to the vision sentence.
// "A platform for composing, building, training, deploying and operating
// data, AI and software systems." Every number is read off the live tables
// at request time (GET /platform) - nothing is stored.

interface Platform {
  vision: string
  composing: { systems: number; model_systems: number }
  building: { drafts: number; built: number; solutions: number; installs: number }
  training: { registry_versions: number; active_models: number; language_versions: number; versions_7d: number }
  deploying: { deployments: number; live: number; serving_invocations_7d: number }
  operating: { workflows: number; datasets: number; dashboards: number; scheduled_reports: number; executions_7d: number; failures_7d: number; failure_rate_7d: number }
  verdicts: Record<string, boolean>
  ready: boolean
}

const { api } = useApi()
const loading = ref(true)
const pageError = ref('')
const p = ref<Platform | null>(null)

const VERBS = [
  {
    key: 'composing', label: 'Compose', color: 'text-amber-300', bg: 'bg-amber-500/10', icon: Boxes,
    blurb: 'Operating units that bind workflows, datasets, models and reports into one health-scored thing.',
  },
  {
    key: 'building', label: 'Build', color: 'text-fuchsia-300', bg: 'bg-fuchsia-500/10', icon: Wand2,
    blurb: 'Describe a system in plain language, interview, build real primitives - or install a curated solution.',
  },
  {
    key: 'training', label: 'Train', color: 'text-violet-300', bg: 'bg-violet-500/10', icon: BrainCircuit,
    blurb: 'From-scratch numpy and torch training: classical ML, MLPs and causal language models with lineage.',
  },
  {
    key: 'deploying', label: 'Deploy', color: 'text-rose-300', bg: 'bg-rose-500/10', icon: Rocket,
    blurb: 'Registry rows become live webhook endpoints that answer with the model\'s output.',
  },
  {
    key: 'operating', label: 'Operate', color: 'text-emerald-300', bg: 'bg-emerald-500/10', icon: Gauge,
    blurb: 'Executions, dashboards, scheduled reports, drift monitoring and honest health verdicts.',
  },
] as const

const verbLink: Record<string, string> = {
  composing: '/systems', building: '/builder', training: '/model-systems',
  deploying: '/deployments', operating: '/observability',
}

async function load() {
  loading.value = true
  try {
    p.value = await api.get<Platform>('/platform')
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'Could not load the platform overview'
  } finally {
    loading.value = false
  }
}

function stat(verb: string): { label: string; value: string | number }[] {
  const d = p.value as any
  if (!d) return []
  switch (verb) {
    case 'composing':
      return [
        { label: 'systems', value: d.composing.systems },
        { label: 'model systems', value: d.composing.model_systems },
      ]
    case 'building':
      return [
        { label: 'drafts built', value: `${d.building.built}/${d.building.drafts}` },
        { label: 'solutions', value: d.building.solutions },
        { label: 'installs', value: d.building.installs },
      ]
    case 'training':
      return [
        { label: 'registry versions', value: d.training.registry_versions },
        { label: 'active models', value: d.training.active_models },
        { label: 'language versions', value: d.training.language_versions },
        { label: 'new / 7d', value: d.training.versions_7d },
      ]
    case 'deploying':
      return [
        { label: 'deployments', value: d.deploying.deployments },
        { label: 'live', value: d.deploying.live },
        { label: 'calls / 7d', value: d.deploying.serving_invocations_7d },
      ]
    case 'operating':
      return [
        { label: 'workflows', value: d.operating.workflows },
        { label: 'datasets', value: d.operating.datasets },
        { label: 'executions / 7d', value: d.operating.executions_7d },
        { label: 'failure rate / 7d', value: `${d.operating.failure_rate_7d}%` },
      ]
    default:
      return []
  }
}

onMounted(load)
</script>

<template>
  <div class="mx-auto max-w-6xl px-4 py-8 lg:px-8">
    <div class="mb-6 flex flex-wrap items-center gap-3">
      <div class="flex h-10 w-10 items-center justify-center rounded-xl bg-sky-500/15">
        <Globe class="h-5 w-5 text-sky-400" />
      </div>
      <div class="min-w-0 flex-1">
        <h1 class="text-xl font-bold text-zinc-100">Platform</h1>
        <p class="max-w-2xl text-xs leading-relaxed text-zinc-500">{{ p?.vision || 'A platform for composing, building, training, deploying and operating data, AI and software systems.' }}</p>
      </div>
      <div
        v-if="p"
        class="flex items-center gap-1.5 rounded-full px-3 py-1.5 text-xs font-bold"
        :class="p.ready ? 'bg-emerald-500/15 text-emerald-300' : 'bg-zinc-800/80 text-zinc-400'"
      >
        <CheckCircle2 v-if="p.ready" class="h-4 w-4" />
        <XCircle v-else class="h-4 w-4" />
        {{ p.ready ? 'all five verbs active' : 'verbs pending' }}
      </div>
    </div>

    <p v-if="pageError" class="mb-4 rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-xs text-rose-300">{{ pageError }}</p>

    <div v-if="loading" class="flex items-center justify-center py-20 text-zinc-500">
      <Loader2 class="h-5 w-5 animate-spin" />
    </div>

    <div v-else-if="p" class="space-y-3">
      <div
        v-for="v in VERBS"
        :key="v.key"
        class="flex flex-wrap items-center gap-4 rounded-2xl border p-4"
        :class="p.verdicts[v.key] ? 'border-zinc-800 bg-zinc-900/60' : 'border-dashed border-zinc-800 bg-zinc-900/30'"
      >
        <div class="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl" :class="v.bg">
          <component :is="v.icon" class="h-5 w-5" :class="v.color" />
        </div>
        <div class="min-w-[200px] flex-1">
          <div class="flex items-center gap-2">
            <h2 class="text-sm font-bold text-zinc-100">{{ v.label }}</h2>
            <span
              class="flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-bold uppercase"
              :class="p.verdicts[v.key] ? 'bg-emerald-500/15 text-emerald-300' : 'bg-zinc-700/30 text-zinc-500'"
            >
              <CheckCircle2 v-if="p.verdicts[v.key]" class="h-3 w-3" />
              <XCircle v-else class="h-3 w-3" />
              {{ p.verdicts[v.key] ? 'active' : 'no evidence yet' }}
            </span>
          </div>
          <p class="mt-0.5 max-w-xl text-[11px] leading-relaxed text-zinc-500">{{ v.blurb }}</p>
        </div>
        <div class="flex flex-wrap gap-2">
          <div v-for="s in stat(v.key)" :key="s.label" class="rounded-xl bg-zinc-950/70 px-3 py-2 text-center">
            <div class="text-base font-bold" :class="v.color">{{ s.value }}</div>
            <div class="text-[10px] uppercase tracking-wide text-zinc-500">{{ s.label }}</div>
          </div>
          <NuxtLink
            :to="verbLink[v.key]"
            class="flex items-center self-center rounded-lg bg-zinc-800 px-3 py-2 text-[11px] font-semibold text-zinc-300 hover:bg-zinc-700"
          >
            open {{ verbLink[v.key].slice(1) }}
          </NuxtLink>
        </div>
      </div>

      <div class="rounded-2xl border border-zinc-800 bg-zinc-900/40 p-4">
        <p class="text-[11px] leading-relaxed text-zinc-500">
          Every number here is DERIVED at read time from the live estate - systems, drafts, registry rows,
          deployment invocations, execution logs - and owner-scoped to you. When all five verbs show
          evidence, the platform verdict is <span class="font-semibold text-emerald-300">ready</span>.
        </p>
      </div>
    </div>
  </div>
</template>

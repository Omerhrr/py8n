<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import {
  Loader2, Wand2, Send, CheckCircle2, Circle, Layers, Cpu, Hammer,
  AlertTriangle, ExternalLink, User, Bot, RefreshCw,
} from 'lucide-vue-next'
import { useApi } from '~/composables/useApi'

// v59: AI System Builder - Describe -> Clarify -> Design -> Build -> Review.
// The description synthesizes a SystemSpec (deterministic, so it can never
// propose a component py8n cannot build); the interview + component toggles
// refine it; Build translates the selected components into REAL primitives
// (dataset, workflow graph, contract, policy, dashboard, report, alert rule).

interface SpecComponent { id: string; label: string; tier: string; selected: boolean; note: string }
interface SpecQuestion { id: string; question: string; key: string; answered: boolean; llm?: boolean }
interface SystemSpec {
  title: string; purpose: string; persona: string
  source: { kind: string; backend: string; label: string; table: string; connection: string }
  schedule: Record<string, any>
  fields: { name: string; dtype: string }[]
  dedupe_keys: string[]
  lookback_hours: number
  webhook_url: string
  report_fmt: string
  components: SpecComponent[]
  questions: SpecQuestion[]
  notes: string[]
}
interface Draft {
  id: string; name: string; description: string; persona: string; status: string
  spec: SystemSpec; messages: any[]; built: BuiltRefs | null
  created_at: string | null; updated_at: string | null
}
interface BuiltRefs {
  workflow_id: string | null; workflow_name: string | null
  dataset_id: string | null; dataset_name: string | null
  contract_version: number | null; on_violation: string | null
  dashboard_id: string | null; report_id: string | null; report_cron: string | null
  notification_rule_id: string | null; policy: Record<string, any> | null
  notes: string[]
}

const { api } = useApi()
const loading = ref(true)
const pageError = ref('')

const description = ref('')
const useLlm = ref(false)
const creating = ref(false)
const draft = ref<Draft | null>(null)
const drafts = ref<any[]>([])

// interview answer inputs (keyed by question key)
const answers = ref<Record<string, string>>({})
const answering = ref(false)
const toggling = ref<string | null>(null)
const building = ref(false)
const buildError = ref('')

const personaMeta: Record<string, { chip: string; label: string }> = {
  data_engineer: { chip: 'bg-sky-500/15 text-sky-300', label: 'data engineer depth' },
  business: { chip: 'bg-violet-500/15 text-violet-300', label: 'business language' },
}
const tierMeta: Record<string, { label: string; text: string }> = {
  core: { label: 'CORE', text: 'text-orange-300' },
  recommended: { label: 'RECOMMENDED', text: 'text-sky-300' },
  optional: { label: 'OPTIONAL', text: 'text-zinc-400' },
}

const selectedCount = computed(() => draft.value?.spec.components.filter(c => c.selected).length || 0)
const groupedComponents = computed(() => {
  const order = ['core', 'recommended', 'optional']
  const groups: Record<string, SpecComponent[]> = {}
  for (const c of draft.value?.spec.components || []) {
    (groups[c.tier] = groups[c.tier] || []).push(c)
  }
  return order.filter(t => groups[t]?.length).map(t => ({ tier: t, items: groups[t] }))
})

function fmtSchedule(s: Record<string, any>): string {
  if (!s || !Object.keys(s).length) return 'manual'
  if (s.mode === 'cron') return `cron: ${s.cron}`
  const sec = Number(s.interval_seconds || 0)
  if (sec >= 3600) return `every ${sec % 3600 === 0 ? sec / 3600 + 'h' : Math.round(sec / 3600) + 'h'}`
  return `every ${Math.round(sec / 60)}m`
}

async function loadDrafts() {
  try {
    drafts.value = await api.get('/builder/systems')
  } catch { /* the create form is the primary path */ }
}

async function openDraft(id: string) {
  loading.value = true
  pageError.value = ''
  try {
    draft.value = await api.get<Draft>(`/builder/systems/${id}`)
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'Could not load the draft'
  } finally {
    loading.value = false
  }
}

async function createDraft() {
  if (description.value.trim().length < 8) {
    pageError.value = 'Describe the system in a sentence or two first.'
    return
  }
  creating.value = true
  pageError.value = ''
  try {
    draft.value = await api.post('/builder/systems', { description: description.value.trim(), use_llm: useLlm.value })
    answers.value = {}
    await loadDrafts()
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'Could not design the system'
  } finally {
    creating.value = false
  }
}

async function submitAnswers() {
  if (!draft.value) return
  const payload: Record<string, string> = {}
  for (const [k, v] of Object.entries(answers.value)) {
    if (v && v.trim()) payload[k] = v.trim()
  }
  if (!Object.keys(payload).length) return
  answering.value = true
  try {
    draft.value = await api.post(`/builder/systems/${draft.value.id}/answers`, { answers: payload })
    answers.value = {}
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'Could not save the answers'
  } finally {
    answering.value = false
  }
}

async function toggle(c: SpecComponent) {
  if (!draft.value || draft.value.status === 'built') return
  toggling.value = c.id
  try {
    draft.value = await api.post(`/builder/systems/${draft.value.id}/components`, {
      component_id: c.id, selected: !c.selected,
    })
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'Toggle failed'
  } finally {
    toggling.value = null
  }
}

async function build() {
  if (!draft.value) return
  building.value = true
  buildError.value = ''
  try {
    draft.value = await api.post(`/builder/systems/${draft.value.id}/build`)
    await loadDrafts()
  } catch (e: any) {
    buildError.value = e?.data?.detail || e?.message || 'Build failed'
  } finally {
    building.value = false
  }
}

function resetForm() {
  draft.value = null
  description.value = ''
  answers.value = {}
  buildError.value = ''
}

onMounted(async () => {
  await loadDrafts()
  loading.value = false
})
</script>

<template>
  <div class="text-zinc-100">
    <header class="sticky top-0 z-20 border-b border-zinc-800/80 bg-zinc-950/90 backdrop-blur">
      <div class="mx-auto max-w-5xl px-4 py-3.5 sm:px-6">
        <div class="flex flex-wrap items-center gap-3">
          <div class="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-pink-500 to-violet-500 shadow-lg shadow-pink-500/20">
            <Wand2 class="h-4 w-4 text-white" />
          </div>
          <div class="min-w-0 flex-1">
            <h1 class="text-lg font-bold tracking-tight">AI System Builder</h1>
            <p class="-mt-0.5 text-[11px] text-zinc-500">Describe → Clarify → Design → Build - py8n becomes your systems architect</p>
          </div>
          <button
            v-if="draft"
            class="flex items-center gap-1.5 rounded-xl border border-zinc-800 bg-zinc-900 px-3 py-2 text-xs font-medium text-zinc-300 transition hover:text-zinc-100"
            @click="resetForm"
          >
            <RefreshCw class="h-3.5 w-3.5" /> New system
          </button>
        </div>
      </div>
    </header>

    <main class="mx-auto max-w-5xl px-4 py-6 sm:px-6">
      <div v-if="pageError" class="mb-4 flex items-start gap-2 rounded-xl border border-rose-500/40 bg-rose-500/10 px-4 py-3 text-sm text-rose-300">
        <AlertTriangle class="mt-0.5 h-4 w-4 shrink-0" /> {{ pageError }}
      </div>

      <!-- describe -->
      <div v-if="!draft" class="rounded-2xl border border-zinc-800/80 bg-zinc-900/50 p-5">
        <h2 class="flex items-center gap-2 text-sm font-bold"><Bot class="h-4 w-4 text-pink-400" /> What should py8n build for you?</h2>
        <p class="mt-1 text-[11px] leading-relaxed text-zinc-500">
          Describe the outcome in your own words - "every hour pull orders from Postgres, validate the schema,
          dedupe them, write to a curated dataset and alert me if quality drops" or "send me a daily report of
          yesterday's sales". The builder proposes a component checklist, interviews you on the missing pieces,
          then builds the real workflows, datasets, contracts and dashboards.
        </p>
        <textarea
          v-model="description"
          rows="3"
          class="mt-3 w-full resize-y rounded-xl border border-zinc-800 bg-zinc-950 px-3 py-2.5 text-sm text-zinc-200 outline-none transition focus:border-pink-500/60"
          placeholder="I need a pipeline that pulls orders from Postgres every hour, validates the schema, handles late-arriving records, deduplicates them, writes to a curated dataset, and alerts me if quality drops."
        />
        <div class="mt-3 flex flex-wrap items-center gap-3">
          <button
            class="flex items-center gap-1.5 rounded-xl bg-pink-500 px-4 py-2 text-xs font-bold text-white transition hover:bg-pink-400 disabled:opacity-50"
            :disabled="creating"
            @click="createDraft"
          >
            <Loader2 v-if="creating" class="h-3.5 w-3.5 animate-spin" />
            <Wand2 v-else class="h-3.5 w-3.5" />
            Design system
          </button>
          <label class="flex cursor-pointer items-center gap-1.5 text-[11px] text-zinc-500">
            <input v-model="useLlm" type="checkbox" class="h-3 w-3 accent-pink-500" />
            AI enhancement via sandbox bridge (fail-soft)
          </label>
        </div>
      </div>

      <!-- drafts list -->
      <div v-if="!draft && drafts.length" class="mt-5">
        <h2 class="mb-2 text-sm font-bold">Your systems</h2>
        <div class="space-y-2">
          <button v-for="d in drafts" :key="d.id" class="flex w-full items-center gap-3 rounded-xl border border-zinc-800/80 bg-zinc-900/50 px-4 py-3 text-left transition hover:border-pink-500/40" @click="openDraft(d.id)">
            <Layers class="h-4 w-4 shrink-0" :class="d.status === 'built' ? 'text-emerald-400' : 'text-zinc-500'" />
            <div class="min-w-0 flex-1">
              <p class="truncate text-xs font-semibold">{{ d.name }}</p>
              <p class="truncate text-[10px] text-zinc-600">{{ d.description }}</p>
            </div>
            <span class="shrink-0 rounded-full px-2 py-0.5 text-[10px]" :class="d.status === 'built' ? 'bg-emerald-500/15 text-emerald-300' : 'bg-zinc-800 text-zinc-400'">
              {{ d.status }} · {{ d.selected }} components
            </span>
          </button>
        </div>
      </div>

      <!-- the draft -->
      <template v-if="draft">
        <!-- spec header -->
        <div class="mb-4 rounded-2xl border border-pink-500/30 bg-pink-500/5 p-5">
          <div class="flex flex-wrap items-center gap-2">
            <h2 class="text-base font-bold">{{ draft.spec.title }}</h2>
            <span class="rounded-full px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide" :class="personaMeta[draft.spec.persona]?.chip">
              <User v-if="draft.spec.persona === 'business'" class="mr-0.5 inline h-2.5 w-2.5" />
              <Cpu v-else class="mr-0.5 inline h-2.5 w-2.5" />
              {{ personaMeta[draft.spec.persona]?.label || draft.spec.persona }}
            </span>
            <span class="rounded-full px-2 py-0.5 text-[10px] font-bold uppercase" :class="draft.status === 'built' ? 'bg-emerald-500/15 text-emerald-300' : 'bg-zinc-800 text-zinc-400'">{{ draft.status }}</span>
            <span class="ml-auto text-[10px] text-zinc-500">{{ selectedCount }} components selected</span>
          </div>
          <p class="mt-1.5 text-[11px] italic leading-relaxed text-zinc-400">"{{ draft.description }}"</p>
          <div class="mt-2 flex flex-wrap gap-1.5 text-[10px]">
            <span class="rounded-full bg-zinc-800 px-2 py-0.5 text-zinc-300">source: {{ draft.spec.source?.label || 'upload' }}{{ draft.spec.source?.table ? ` · ${draft.spec.source.table}` : '' }}</span>
            <span class="rounded-full bg-zinc-800 px-2 py-0.5 text-zinc-300">schedule: {{ fmtSchedule(draft.spec.schedule) }}</span>
            <span v-if="draft.spec.fields?.length" class="rounded-full bg-zinc-800 px-2 py-0.5 text-zinc-300">{{ draft.spec.fields.length }} contract columns</span>
            <span v-if="draft.spec.lookback_hours" class="rounded-full bg-zinc-800 px-2 py-0.5 text-zinc-300">lookback {{ draft.spec.lookback_hours }}h</span>
          </div>
          <p v-for="(n, i) in draft.spec.notes" :key="i" class="mt-1.5 text-[10px] text-zinc-600">{{ n }}</p>
        </div>

        <!-- interview -->
        <div v-if="draft.status !== 'built'" class="mb-5 rounded-2xl border border-zinc-800/80 bg-zinc-900/50 p-5">
          <h2 class="flex items-center gap-2 text-sm font-bold"><Bot class="h-4 w-4 text-sky-400" /> Interview</h2>
          <p class="mt-0.5 text-[11px] text-zinc-500">The builder needs a few answers before it can build.</p>
          <div class="mt-3 space-y-2.5">
            <div v-for="q in draft.spec.questions" :key="q.id" class="flex flex-wrap items-center gap-2">
              <span class="w-64 shrink-0 text-[11px] text-zinc-300" :class="q.answered && 'text-zinc-500 line-through'">{{ q.question }}</span>
              <input
                v-model="answers[q.key]"
                class="min-w-0 flex-1 rounded-xl border border-zinc-800 bg-zinc-950 px-3 py-1.5 text-xs text-zinc-200 outline-none transition focus:border-sky-500/60"
                :disabled="q.answered"
                :placeholder="q.answered ? 'answered' : 'your answer'"
              />
              <CheckCircle2 v-if="q.answered" class="h-3.5 w-3.5 shrink-0 text-emerald-400" />
            </div>
          </div>
          <button
            class="mt-3 flex items-center gap-1.5 rounded-xl border border-sky-500/40 bg-sky-500/10 px-3 py-1.5 text-[11px] font-semibold text-sky-300 transition hover:bg-sky-500/20 disabled:opacity-50"
            :disabled="answering"
            @click="submitAnswers"
          >
            <Loader2 v-if="answering" class="h-3 w-3 animate-spin" />
            <Send v-else class="h-3 w-3" /> Submit answers
          </button>
        </div>

        <!-- components -->
        <div class="mb-5 rounded-2xl border border-zinc-800/80 bg-zinc-900/50 p-5">
          <h2 class="flex items-center gap-2 text-sm font-bold"><Layers class="h-4 w-4 text-orange-400" /> System design</h2>
          <p class="mt-0.5 text-[11px] text-zinc-500">Tick what you want - every component maps to a real py8n primitive.</p>
          <div v-for="g in groupedComponents" :key="g.tier" class="mt-3">
            <p class="mb-1.5 text-[10px] font-bold uppercase tracking-widest" :class="tierMeta[g.tier]?.text">{{ tierMeta[g.tier]?.label }}</p>
            <div class="space-y-1.5">
              <div
                v-for="c in g.items" :key="c.id"
                class="flex items-start gap-2.5 rounded-xl border px-3 py-2 transition"
                :class="c.selected ? 'border-emerald-500/30 bg-emerald-500/5' : 'border-zinc-800 bg-zinc-950/50'"
              >
                <button class="mt-0.5 shrink-0" :disabled="draft.status === 'built' || toggling === c.id" @click="toggle(c)">
                  <Loader2 v-if="toggling === c.id" class="h-4 w-4 animate-spin text-zinc-500" />
                  <CheckCircle2 v-else-if="c.selected" class="h-4 w-4 text-emerald-400" />
                  <Circle v-else class="h-4 w-4 text-zinc-600" />
                </button>
                <div class="min-w-0 flex-1">
                  <p class="text-xs font-semibold" :class="c.selected ? 'text-zinc-100' : 'text-zinc-400'">{{ c.label }}</p>
                  <p class="text-[10px] text-zinc-600">{{ c.detail }}</p>
                  <p v-if="c.note" class="text-[10px] text-amber-400/80">{{ c.note }}</p>
                </div>
              </div>
            </div>
          </div>
        </div>

        <!-- build -->
        <div v-if="draft.status !== 'built'" class="mb-5">
          <p v-if="buildError" class="mb-3 rounded-xl border border-rose-500/40 bg-rose-500/10 px-4 py-3 text-xs text-rose-300">{{ buildError }}</p>
          <button
            class="flex items-center gap-2 rounded-xl bg-gradient-to-r from-pink-500 to-violet-500 px-5 py-2.5 text-sm font-bold text-white shadow-lg shadow-pink-500/20 transition hover:from-pink-400 hover:to-violet-400 disabled:opacity-50"
            :disabled="building"
            @click="build"
          >
            <Loader2 v-if="building" class="h-4 w-4 animate-spin" />
            <Hammer v-else class="h-4 w-4" />
            Build the system
          </button>
          <p class="mt-2 text-[10px] text-zinc-600">Creates a dataset, the pipeline workflow (inactive until you fill source credentials), and everything else you ticked.</p>
        </div>

        <!-- review -->
        <div v-if="draft.status === 'built' && draft.built" class="rounded-2xl border border-emerald-500/30 bg-emerald-500/5 p-5">
          <h2 class="flex items-center gap-2 text-sm font-bold text-emerald-300"><CheckCircle2 class="h-4 w-4" /> System built</h2>
          <div class="mt-3 grid gap-2 sm:grid-cols-2">
            <NuxtLink v-if="draft.built.workflow_id" :to="`/workflows/${draft.built.workflow_id}`" class="flex items-center justify-between rounded-xl border border-zinc-800 bg-zinc-950/60 px-3 py-2 transition hover:border-orange-500/40">
              <span class="text-xs font-semibold">Pipeline workflow</span>
              <span class="flex items-center gap-1 text-[10px] text-zinc-500">{{ draft.built.workflow_name }} <ExternalLink class="h-3 w-3" /></span>
            </NuxtLink>
            <NuxtLink v-if="draft.built.dataset_id" :to="`/datasets/${draft.built.dataset_id}`" class="flex items-center justify-between rounded-xl border border-zinc-800 bg-zinc-950/60 px-3 py-2 transition hover:border-lime-500/40">
              <span class="text-xs font-semibold">Target dataset</span>
              <span class="flex items-center gap-1 text-[10px] text-zinc-500">{{ draft.built.dataset_name }} <ExternalLink class="h-3 w-3" /></span>
            </NuxtLink>
            <NuxtLink v-if="draft.built.dashboard_id" to="/dashboards" class="flex items-center justify-between rounded-xl border border-zinc-800 bg-zinc-950/60 px-3 py-2 transition hover:border-sky-500/40">
              <span class="text-xs font-semibold">Dashboard</span>
              <span class="flex items-center gap-1 text-[10px] text-zinc-500">auto-generated <ExternalLink class="h-3 w-3" /></span>
            </NuxtLink>
            <div v-if="draft.built.contract_version" class="flex items-center justify-between rounded-xl border border-zinc-800 bg-zinc-950/60 px-3 py-2">
              <span class="text-xs font-semibold">Schema contract</span>
              <span class="text-[10px]" :class="draft.built.on_violation === 'error' ? 'text-rose-300' : 'text-amber-300'">v{{ draft.built.contract_version }} · {{ draft.built.on_violation }} mode</span>
            </div>
            <div v-if="draft.built.policy" class="flex items-center justify-between rounded-xl border border-zinc-800 bg-zinc-950/60 px-3 py-2">
              <span class="text-xs font-semibold">Retry policy</span>
              <span class="font-mono text-[10px] text-zinc-400">{{ Object.entries(draft.built.policy).map(([k, v]) => `${k}=${v}`).join(' · ') }}</span>
            </div>
            <div v-if="draft.built.report_id" class="flex items-center justify-between rounded-xl border border-zinc-800 bg-zinc-950/60 px-3 py-2">
              <span class="text-xs font-semibold">Scheduled report</span>
              <span class="font-mono text-[10px] text-zinc-400">{{ draft.built.report_cron }}</span>
            </div>
            <div v-if="draft.built.notification_rule_id" class="flex items-center justify-between rounded-xl border border-zinc-800 bg-zinc-950/60 px-3 py-2">
              <span class="text-xs font-semibold">Failure alerts</span>
              <span class="text-[10px] text-zinc-400">webhook rule · scoped</span>
            </div>
          </div>
          <p v-for="(n, i) in draft.built.notes || []" :key="'bn' + i" class="mt-2 rounded-xl border border-amber-500/30 bg-amber-500/5 px-3 py-2 text-[10px] text-amber-300/90">{{ n }}</p>
          <p class="mt-3 text-[10px] leading-relaxed text-zinc-600">
            The pipeline starts INACTIVE on purpose - fill in the source credentials (table, connection or URL),
            activate the trigger, and run it once to see the checkpoints move.
          </p>
        </div>
      </template>
    </main>
  </div>
</template>

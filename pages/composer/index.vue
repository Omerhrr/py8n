<script setup lang="ts">
// v82: THE AI SYSTEM COMPOSER - the roadmap's "Build me a customer support
// system" door. Describe a BUSINESS system; the composer proposes a spec
// (LLM-first through a real credential, or the deterministic archetypes);
// you review the composed components; Build turns them into REAL py8n
// primitives - datasets, event-reactive workflows, knowledge-bound voice
// agents, meeting rooms, channel queues - bound into a RUNNING system.
// The builder composes primitives; it never generates a blob of code.
import { ref, computed, onMounted } from 'vue'
import {
  Loader2, Sparkles, Database, Workflow, Bot, Video, Hourglass, Boxes,
  CheckCircle2, AlertTriangle, ArrowRight, Wand2, ExternalLink, Info,
} from 'lucide-vue-next'
import { useApi } from '~/composables/useApi'

interface SpecComponent {
  kind: string; name: string; description?: string
  columns?: string[]; rows?: any[]
  trigger?: { type: string; params?: Record<string, any> }
  steps?: { type: string; name?: string; params?: Record<string, any> }[]
  greeting?: string; system_prompt?: string; brain?: string
  knowledge?: { dataset?: string; text_column?: string; answer_column?: string }
  modality?: string; agent?: string; meeting?: string
  max_size?: number; max_wait_seconds?: number; announce?: boolean
}
interface Spec {
  name: string; description?: string; mode?: string; archetype?: string
  components: SpecComponent[]; notes?: string[]
}
interface Built {
  datasets: any[]; workflows: any[]; voice_agents: any[]
  meeting_rooms: any[]; queues: any[]
  system: { id: string; name: string; lifecycle: string; components: number } | null
  notes?: string[]
}

const { api } = useApi()
const loading = ref(true)
const pageError = ref('')

const description = ref('')
const credentialId = ref('')
const model = ref('')
const proposing = ref(false)
const building = ref(false)
const generating = ref(false)
const spec = ref<Spec | null>(null)
const built = ref<Built | null>(null)
const actionError = ref('')

const catalog = ref<any>(null)
const credentials = ref<any[]>([])

const KIND_META: Record<string, { icon: any; label: string; color: string }> = {
  dataset: { icon: Database, label: 'Dataset', color: 'text-emerald-400' },
  workflow: { icon: Workflow, label: 'Workflow', color: 'text-sky-400' },
  voice_agent: { icon: Bot, label: 'Voice agent', color: 'text-violet-400' },
  meeting_room: { icon: Video, label: 'Meeting room', color: 'text-amber-400' },
  queue: { icon: Hourglass, label: 'Queue', color: 'text-rose-400' },
}

const llmCredentials = computed(() =>
  credentials.value.filter((c: any) =>
    ['openai_compatible', 'anthropic'].includes(c.type)))

onMounted(async () => {
  try {
    const [cat, creds] = await Promise.all([
      api('/ai-composer/catalog'),
      api('/credentials'),
    ])
    catalog.value = cat
    credentials.value = creds.credentials || creds || []
  } catch (e: any) {
    pageError.value = e?.message || String(e)
  } finally {
    loading.value = false
  }
})

async function propose() {
  actionError.value = ''
  built.value = null
  proposing.value = true
  try {
    const body: any = { description: description.value }
    if (credentialId.value) body.credential_id = credentialId.value
    if (model.value.trim()) body.model = model.value.trim()
    const res = await api('/ai-composer/propose', body)
    spec.value = res.spec
  } catch (e: any) {
    actionError.value = e?.message || String(e)
  } finally {
    proposing.value = false
  }
}

async function build() {
  if (!spec.value) return
  actionError.value = ''
  building.value = true
  try {
    built.value = await api('/ai-composer/build', { spec: spec.value })
  } catch (e: any) {
    actionError.value = e?.message || String(e)
  } finally {
    building.value = false
  }
}

async function generate() {
  actionError.value = ''
  spec.value = null
  generating.value = true
  try {
    const body: any = { description: description.value }
    if (credentialId.value) body.credential_id = credentialId.value
    if (model.value.trim()) body.model = model.value.trim()
    built.value = await api('/ai-composer/generate', body)
  } catch (e: any) {
    actionError.value = e?.message || String(e)
  } finally {
    generating.value = false
  }
}

function useArchetype(summary: string) {
  description.value = summary
}

function kindOf(c: SpecComponent) {
  return KIND_META[c.kind] || { icon: Boxes, label: c.kind, color: 'text-zinc-400' }
}

function stepLine(s: { type: string; name?: string }) {
  return s.name ? `${s.name} (${s.type})` : s.type
}
</script>

<template>
  <div class="max-w-5xl mx-auto px-4 py-8">
    <div class="flex items-start gap-3 mb-6">
      <div class="p-2 rounded-lg bg-indigo-500/10 border border-indigo-500/20">
        <Sparkles class="w-6 h-6 text-indigo-400" />
      </div>
      <div>
        <h1 class="text-2xl font-semibold text-zinc-100">AI System Composer</h1>
        <p class="text-sm text-zinc-400">
          Describe a business system - it is composed from py8n primitives
          (datasets, event-reactive workflows, voice agents, rooms, queues) and
          installed as a running system. Primitives, never generated code.
        </p>
      </div>
    </div>

    <div v-if="pageError" class="mb-4 p-3 rounded-lg bg-red-500/10 border border-red-500/20 text-red-300 text-sm flex items-center gap-2">
      <AlertTriangle class="w-4 h-4 shrink-0" /> {{ pageError }}
    </div>

    <div v-if="loading" class="flex items-center gap-2 text-zinc-400"><Loader2 class="w-4 h-4 animate-spin" /> Loading the composer catalog...</div>

    <template v-else>
      <!-- describe -->
      <div class="rounded-xl border border-zinc-800 bg-zinc-900/60 p-4 mb-5">
        <textarea
          v-model="description"
          rows="3"
          placeholder="Build me a customer support system: a phone line that answers from our FAQ, keeps callers in a queue with announcements, and logs every ended call."
          class="w-full bg-zinc-950 border border-zinc-800 rounded-lg px-3 py-2 text-sm text-zinc-200 placeholder-zinc-600 focus:outline-none focus:border-indigo-500"
        />
        <div class="flex flex-wrap items-center gap-2 mt-3">
          <select v-model="credentialId" class="bg-zinc-950 border border-zinc-800 rounded-lg px-2 py-1.5 text-xs text-zinc-300">
            <option value="">Deterministic archetypes (no LLM)</option>
            <option v-for="c in llmCredentials" :key="c.id" :value="c.id">
              LLM-first: {{ c.name }} ({{ c.type }})
            </option>
          </select>
          <input
            v-model="model" placeholder="model override (optional)"
            class="bg-zinc-950 border border-zinc-800 rounded-lg px-2 py-1.5 text-xs text-zinc-300 w-52"
          />
          <div class="flex-1" />
          <button
            :disabled="proposing || generating || description.trim().length < 8"
            class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 text-white text-xs font-medium"
            @click="propose"
          >
            <Loader2 v-if="proposing" class="w-3.5 h-3.5 animate-spin" />
            <Wand2 v-else class="w-3.5 h-3.5" /> Propose spec
          </button>
          <button
            :disabled="generating || proposing || description.trim().length < 8"
            class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-zinc-800 hover:bg-zinc-700 disabled:opacity-40 text-zinc-200 text-xs font-medium"
            @click="generate"
          >
            <Loader2 v-if="generating" class="w-3.5 h-3.5 animate-spin" />
            <template v-else>Describe <ArrowRight class="w-3.5 h-3.5" /> Deploy</template>
          </button>
        </div>
        <div v-if="catalog" class="flex flex-wrap gap-1.5 mt-3">
          <button
            v-for="a in catalog.archetypes" :key="a.id"
            class="px-2 py-0.5 rounded-full border border-zinc-800 hover:border-indigo-500/50 text-[11px] text-zinc-400 hover:text-zinc-200"
            :title="a.summary"
            @click="useArchetype(a.summary)"
          >
            {{ a.id.replace('_', ' ') }}
          </button>
        </div>
      </div>

      <div v-if="actionError" class="mb-5 p-3 rounded-lg bg-red-500/10 border border-red-500/20 text-red-300 text-sm flex items-center gap-2">
        <AlertTriangle class="w-4 h-4 shrink-0" /> {{ actionError }}
      </div>

      <!-- spec review -->
      <div v-if="spec && !built" class="rounded-xl border border-zinc-800 bg-zinc-900/60 p-4 mb-5">
        <div class="flex items-center justify-between mb-3">
          <div>
            <h2 class="text-lg font-medium text-zinc-100">{{ spec.name }}</h2>
            <p class="text-xs text-zinc-500">
              proposed by {{ spec.mode }}<template v-if="spec.archetype"> - archetype {{ spec.archetype }}</template>
              - validated against the catalog
            </p>
          </div>
          <button
            :disabled="building"
            class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-emerald-600 hover:bg-emerald-500 disabled:opacity-40 text-white text-xs font-medium"
            @click="build"
          >
            <Loader2 v-if="building" class="w-3.5 h-3.5 animate-spin" />
            <CheckCircle2 v-else class="w-3.5 h-3.5" /> Build system
          </button>
        </div>
        <div class="grid gap-2 md:grid-cols-2">
          <div
            v-for="c in spec.components" :key="c.name"
            class="rounded-lg border border-zinc-800 bg-zinc-950/60 p-3"
          >
            <div class="flex items-center gap-2 mb-1">
              <component :is="kindOf(c).icon" class="w-4 h-4" :class="kindOf(c).color" />
              <span class="text-sm font-medium text-zinc-200">{{ c.name }}</span>
              <span class="text-[11px] uppercase tracking-wide text-zinc-500">{{ kindOf(c).label }}</span>
            </div>
            <p v-if="c.description" class="text-xs text-zinc-500 mb-1">{{ c.description }}</p>
            <ul class="text-[11px] text-zinc-400 space-y-0.5">
              <template v-if="c.kind === 'dataset'">
                <li>columns: {{ (c.columns || []).join(', ') }}</li>
                <li v-if="(c.rows || []).length">{{ c.rows.length }} seed row(s)</li>
              </template>
              <template v-else-if="c.kind === 'workflow'">
                <li>trigger: {{ c.trigger?.type }}</li>
                <li v-for="(s, i) in c.steps || []" :key="i" class="pl-2">{{ i + 1 }}. {{ stepLine(s) }}</li>
              </template>
              <template v-else-if="c.kind === 'voice_agent'">
                <li v-if="c.greeting">greeting: "{{ c.greeting }}"</li>
                <li>brain: {{ c.brain || 'scaffold' }}</li>
                <li v-if="c.knowledge?.dataset">knowledge: {{ c.knowledge.dataset }} ({{ c.knowledge.text_column }} -> {{ c.knowledge.answer_column }})</li>
              </template>
              <template v-else-if="c.kind === 'meeting_room'">
                <li>modality: {{ c.modality || 'audio' }}</li>
                <li v-if="c.agent">agent: {{ c.agent }}</li>
              </template>
              <template v-else-if="c.kind === 'queue'">
                <li>seats into: {{ c.meeting }}</li>
                <li v-if="c.agent">agent: {{ c.agent }}</li>
                <li>announcements: {{ c.announce !== false ? 'on' : 'off' }} - SMS/callback backchannel pre-wired</li>
              </template>
            </ul>
          </div>
        </div>
        <div v-if="spec.notes?.length" class="mt-3 space-y-1">
          <p v-for="(n, i) in spec.notes" :key="i" class="text-[11px] text-zinc-500 flex gap-1.5">
            <Info class="w-3 h-3 shrink-0 mt-0.5" /> {{ n }}
          </p>
        </div>
      </div>

      <!-- built -->
      <div v-if="built" class="rounded-xl border border-emerald-500/20 bg-emerald-500/5 p-4">
        <div class="flex items-center gap-2 mb-3">
          <CheckCircle2 class="w-5 h-5 text-emerald-400" />
          <h2 class="text-lg font-medium text-zinc-100">{{ built.system?.name }} is running</h2>
          <span class="px-2 py-0.5 rounded-full bg-emerald-500/10 border border-emerald-500/30 text-emerald-300 text-[11px]">
            {{ built.system?.lifecycle }} - {{ built.system?.components }} components
          </span>
        </div>
        <div class="grid gap-3 md:grid-cols-2 text-sm">
          <div>
            <h3 class="text-xs uppercase tracking-wide text-zinc-500 mb-1">Datasets</h3>
            <p v-for="d in built.datasets" :key="d.id" class="text-zinc-300 text-xs">
              {{ d.name }} <span class="text-zinc-500">({{ d.columns.length }} cols, {{ d.rows }} rows)</span>
            </p>
            <h3 class="text-xs uppercase tracking-wide text-zinc-500 mt-3 mb-1">Workflows <span class="text-zinc-600">(inactive - boot via the system)</span></h3>
            <p v-for="w in built.workflows" :key="w.id" class="text-zinc-300 text-xs">
              {{ w.name }} <span class="text-zinc-500">({{ w.trigger }})</span>
            </p>
          </div>
          <div>
            <h3 class="text-xs uppercase tracking-wide text-zinc-500 mb-1">Agents</h3>
            <p v-for="a in built.voice_agents" :key="a.id" class="text-zinc-300 text-xs">
              {{ a.name }} <span v-if="a.knowledge?.dataset_id" class="text-zinc-500">(knowledge bound)</span>
            </p>
            <h3 class="text-xs uppercase tracking-wide text-zinc-500 mt-3 mb-1">Rooms + queues</h3>
            <p v-for="r in built.meeting_rooms" :key="r.id" class="text-zinc-300 text-xs">
              {{ r.title }} <span class="text-zinc-500">({{ r.modality }})</span>
            </p>
            <p v-for="q in built.queues" :key="q.id" class="text-zinc-300 text-xs">{{ q.name }}</p>
          </div>
        </div>
        <div v-if="built.notes?.length" class="mt-3 space-y-1">
          <p v-for="(n, i) in built.notes" :key="i" class="text-[11px] text-zinc-500 flex gap-1.5">
            <Info class="w-3 h-3 shrink-0 mt-0.5" /> {{ n }}
          </p>
        </div>
        <NuxtLink
          :to="`/systems/${built.system?.id}`"
          class="inline-flex items-center gap-1.5 mt-3 text-xs text-indigo-400 hover:text-indigo-300"
        >
          Open the system <ExternalLink class="w-3 h-3" />
        </NuxtLink>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import {
  Loader2, CheckCircle2, Download, AlertTriangle, ExternalLink, X, Layers,
  Building2, Video, TrendingUp, HeartPulse, Boxes, LayoutDashboard, GitBranch,
  ArrowLeft, BellRing, ListChecks, Database, Bot, Users, PhoneCall,
} from 'lucide-vue-next'
import { useApi } from '~/composables/useApi'

// v92: the operator DETAIL page - the install plan gets a room of its own
// and the cross-department chains get DRAWN. GET /operators/{slug} already
// resolved every leg against the real journeys; this page renders them as
// an SVG walk: operator nodes left to right, the operator whose detail you
// are reading lit up in its own color, each arrow naming the fire state,
// the process that opens itself and the leg's own SLA promise. The chains
// are visible and explorable BEFORE the install - the same honesty the
// v91 shelf cards started.

interface ChainLeg {
  from_process: string; on_state: string; opens: string
  due_in_seconds?: number | null
  from_operator: string; opens_operator: string
}
interface ChainView {
  slug: string; name: string; story: string; position: number
  operators: { slug: string; name: string; icon: string; color: string }[]
  legs: ChainLeg[]
}
interface OpDetail {
  slug: string; name: string; tagline: string; category: string
  icon: string; color: string; outcomes: string[]
  chains: ChainView[]
  installs: {
    datasets: { name: string; description: string; columns: string[]; rows: number }[]
    workflows: { name: string; description: string; trigger: string }[]
    processes: { name: string; states: string[]; seeded_from: string | null
      escalates: boolean; escalation: string
      journeys: { on_state: string; opens: string }[] }[]
    agent: { name: string; knowledge: string | null } | null
    rooms: { name: string; modality: string }[]
    queues: string[]
    campaign: string | null
    dashboard: string
  }
  notes: string[]
}
interface OperatorResult {
  datasets: any[]; workflows: any[]; agents: any[]; rooms: any[]
  queues: any[]; campaign: { id: string; name: string } | null
  dashboard: { id: string; name: string; slug: string } | null
  system: { id: string; name: string; lifecycle: string } | null
  notes: string[]
}

const { api } = useApi()
const route = useRoute()
const slug = computed(() => String(route.params.slug || ''))
const loading = ref(true)
const pageError = ref('')
const op = ref<OpDetail | null>(null)

const installing = ref(false)
const installResult = ref<OperatorResult | null>(null)
const installError = ref('')

const OP_ICONS: Record<string, any> = {
  video: Video, 'trending-up': TrendingUp, 'heart-pulse': HeartPulse,
}
function opIcon(icon: string) {
  return OP_ICONS[icon] || Building2
}

// ---- the drawn chain view - geometry for the SVG walk ---------------------
const NODE_W = 152
const NODE_H = 52
const GAP = 76
const PAD = 6

function chainW(n: number): number {
  return PAD * 2 + n * NODE_W + (n - 1) * GAP
}
function nodeX(i: number): number {
  return PAD + i * (NODE_W + GAP)
}
function slaLabel(d?: number | null): string {
  if (!d) return 'no leg SLA'
  if (d % 86400 === 0) return `leg SLA ${d / 86400}d`
  if (d % 3600 === 0) return `leg SLA ${d / 3600}h`
  return `leg SLA ${Math.round(d / 60)}m`
}
// the leg is THIS operator's to fire (out) or to receive (in) - the
// drawing paints those arrows fuchsia and dashes the pure inbound ones
function legMine(leg: ChainLeg, me: string): boolean {
  return leg.from_operator === me || leg.opens_operator === me
}
function stopLabel(chain: ChainView): string {
  if (chain.position === 0) return 'the chain starts here'
  if (chain.position === chain.operators.length - 1) {
    return `stop ${chain.position + 1} of ${chain.operators.length} - the terminus`
  }
  return `stop ${chain.position + 1} of ${chain.operators.length}`
}

async function installOperator() {
  if (!op.value) return
  installing.value = true
  installError.value = ''
  try {
    installResult.value = await api.post<OperatorResult>(
      `/operators/${op.value.slug}/install`, {})
  } catch (e: any) {
    installError.value = e?.data?.detail || e?.message || 'Install failed'
  } finally {
    installing.value = false
  }
}

function wfRef(w: any): string {
  return w?.id ? `/workflows/${w.id}` : '/workflows'
}

onMounted(async () => {
  try {
    op.value = await api.get<OpDetail>(`/operators/${slug.value}`)
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'Could not load the operator'
  } finally {
    loading.value = false
  }
})
</script>

<template>
  <div class="min-h-screen text-zinc-100">
    <header class="sticky top-0 z-20 border-b border-zinc-800/80 bg-zinc-950/90 backdrop-blur">
      <div class="mx-auto max-w-7xl px-4 py-3.5 sm:px-6">
        <div class="flex flex-wrap items-center gap-3">
          <NuxtLink to="/marketplace"
            class="flex items-center gap-1.5 rounded-xl border border-zinc-800 bg-zinc-900 px-3 py-2 text-xs text-zinc-400 transition hover:text-zinc-200">
            <ArrowLeft class="h-3.5 w-3.5" /> Marketplace
          </NuxtLink>
          <div v-if="op" class="flex h-9 w-9 items-center justify-center rounded-xl shadow-lg"
            :style="{ background: `linear-gradient(135deg, ${op.color}, ${op.color}55)` }">
            <component :is="opIcon(op.icon)" class="h-4 w-4 text-zinc-950" />
          </div>
          <div v-if="op" class="min-w-0 flex-1">
            <h1 class="text-lg font-bold tracking-tight">{{ op.name }}</h1>
            <p class="-mt-0.5 text-[11px] text-zinc-500">{{ op.category }} · business operator - {{ op.tagline }}</p>
          </div>
          <button v-if="op && !installResult"
            class="flex items-center gap-1.5 rounded-xl bg-emerald-500 px-4 py-2 text-xs font-bold text-zinc-950 transition hover:bg-emerald-400 disabled:opacity-50"
            :disabled="installing" @click="installOperator">
            <Loader2 v-if="installing" class="h-3.5 w-3.5 animate-spin" />
            <Download v-else class="h-3.5 w-3.5" /> Install operator
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

      <template v-else-if="op">
        <!-- the install receipt -->
        <div v-if="installResult" class="mb-5 rounded-2xl border border-emerald-500/40 bg-emerald-500/5 p-5">
          <p class="flex items-center gap-1.5 text-[10px] font-bold uppercase tracking-widest text-emerald-400">
            <CheckCircle2 class="h-3.5 w-3.5" /> Running - the whole estate
          </p>
          <div class="mt-3 space-y-1.5">
            <NuxtLink v-if="installResult.system" to="/systems" class="flex items-center justify-between rounded-xl border border-sky-500/30 bg-sky-500/5 px-3 py-2 transition hover:border-sky-500/50">
              <span class="flex items-center gap-1.5 text-xs font-semibold text-sky-200"><Boxes class="h-3.5 w-3.5" /> {{ installResult.system.name }} ({{ installResult.system.lifecycle }})</span>
              <span class="flex items-center gap-1 text-[10px] text-zinc-500">open systems <ExternalLink class="h-3 w-3" /></span>
            </NuxtLink>
            <NuxtLink v-if="installResult.dashboard" :to="`/dashboards/${installResult.dashboard.id}`" class="flex items-center justify-between rounded-xl border border-emerald-500/30 bg-emerald-500/5 px-3 py-2 transition hover:border-emerald-500/50">
              <span class="flex items-center gap-1.5 text-xs font-semibold text-emerald-200"><LayoutDashboard class="h-3.5 w-3.5" /> {{ installResult.dashboard.name }}</span>
              <span class="flex items-center gap-1 text-[10px] text-zinc-500">open board <ExternalLink class="h-3 w-3" /></span>
            </NuxtLink>
            <NuxtLink v-for="w in installResult.workflows" :key="w.id" :to="wfRef(w)" class="flex items-center justify-between rounded-xl border border-orange-500/30 bg-orange-500/5 px-3 py-2 transition hover:border-orange-500/50">
              <span class="text-xs font-semibold text-orange-200">{{ w.name }}</span>
              <span class="flex items-center gap-1 text-[10px] text-zinc-500">reacts to {{ w.trigger }} · inactive until boot <ExternalLink class="h-3 w-3" /></span>
            </NuxtLink>
          </div>
        </div>
        <p v-else-if="installError" class="mb-5 rounded-xl border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-[11px] text-rose-300">{{ installError }}</p>

        <!-- v92: the drawn chains - the cross-department journeys this operator sits in -->
        <section v-if="op.chains.length" class="mb-6">
          <div class="mb-2 flex flex-wrap items-center gap-2">
            <GitBranch class="h-4 w-4 text-fuchsia-400" />
            <h2 class="text-xs font-bold uppercase tracking-widest text-fuchsia-300">The chains</h2>
            <span class="text-[10px] text-zinc-600">the cross-department journeys this operator sits in - install both ends of a leg and the next department's case opens ITSELF</span>
          </div>
          <div class="space-y-4">
            <div v-for="chain in op.chains" :key="chain.slug" class="rounded-2xl border border-fuchsia-900/50 bg-zinc-900/40 p-4">
              <div class="flex flex-wrap items-center gap-2">
                <span class="rounded-full bg-fuchsia-500/15 px-2 py-0.5 text-[10px] font-bold text-fuchsia-300">{{ chain.name }} chain</span>
                <p class="text-[11px] text-zinc-500">{{ chain.story }}</p>
                <span class="ml-auto text-[10px] text-zinc-600">{{ stopLabel(chain) }}</span>
              </div>
              <svg :viewBox="`0 0 ${chainW(chain.operators.length)} 116`"
                class="mt-2 w-full" style="max-width: 640px"
                role="img" :aria-label="`the ${chain.name} chain`">
                <defs>
                  <marker :id="`arr-${chain.slug}`" viewBox="0 0 10 10" refX="9" refY="5"
                    markerWidth="7" markerHeight="7" orient="auto-start-reverse">
                    <path d="M 0 0 L 10 5 L 0 10 z" :fill="chain.legs.some(l => legMine(l, op!.slug)) ? '#e879f9' : '#52525b'" />
                  </marker>
                </defs>
                <!-- the walk: one node per operator, this one lit -->
                <g v-for="(opb, i) in chain.operators" :key="opb.slug">
                  <rect :x="nodeX(i)" y="8" :width="NODE_W" :height="NODE_H" rx="14"
                    :fill="i === chain.position ? opb.color + '2b' : '#131316'"
                    :stroke="i === chain.position ? opb.color : '#3f3f46'"
                    :stroke-width="i === chain.position ? 2 : 1"
                    :opacity="i === chain.position ? 1 : 0.55" />
                  <circle :cx="nodeX(i) + 16" cy="24" r="4" :fill="opb.color"
                    :opacity="i === chain.position ? 1 : 0.55" />
                  <text :x="nodeX(i) + 26" y="28" font-size="11" font-weight="700"
                    :fill="i === chain.position ? '#fafafa' : '#a1a1aa'"
                    :opacity="i === chain.position ? 1 : 0.7">{{ opb.name }}</text>
                  <text v-if="i === chain.position" :x="nodeX(i) + NODE_W / 2" y="48"
                    text-anchor="middle" font-size="8" font-weight="700" letter-spacing="1.5"
                    :fill="opb.color">YOU ARE HERE</text>
                </g>
                <!-- the legs: fire state -> the process that opens itself + the leg's own SLA -->
                <g v-for="(leg, i) in chain.legs" :key="`${leg.from_process}-${leg.on_state}`">
                  <line :x1="nodeX(i) + NODE_W" y1="34" :x2="nodeX(i + 1) - 6" y2="34"
                    :stroke="legMine(leg, op.slug) ? '#e879f9' : '#52525b'" stroke-width="1.6"
                    :stroke-dasharray="leg.opens_operator === op.slug && leg.from_operator !== op.slug ? '4 3' : undefined"
                    :marker-end="`url(#arr-${chain.slug})`" />
                  <text :x="nodeX(i) + NODE_W + GAP / 2" y="74" text-anchor="middle"
                    font-size="9.5" font-weight="700" fill="#67e8f9">{{ leg.on_state }}</text>
                  <text :x="nodeX(i) + NODE_W + GAP / 2" y="88" text-anchor="middle"
                    font-size="9" fill="#a1a1aa">opens {{ leg.opens }}</text>
                  <text :x="nodeX(i) + NODE_W + GAP / 2" y="102" text-anchor="middle"
                    font-size="8.5" font-weight="600"
                    :fill="legMine(leg, op.slug) ? '#e879f9' : '#71717a'">{{ slaLabel(leg.due_in_seconds) }}</text>
                </g>
              </svg>
              <div class="mt-1 space-y-0.5">
                <p v-for="leg in chain.legs" :key="`row-${leg.from_process}-${leg.on_state}`" class="text-[10px] text-zinc-600">
                  <template v-if="leg.from_operator === op.slug">
                    <span class="font-semibold text-fuchsia-300">your leg</span> - when {{ leg.from_process }} lands on
                    <span class="text-zinc-400">{{ leg.on_state }}</span>, {{ leg.opens }} opens itself<span v-if="leg.due_in_seconds"> carrying its own {{ slaLabel(leg.due_in_seconds).replace('leg SLA ', '') }} SLA</span>.
                  </template>
                  <template v-else-if="leg.opens_operator === op.slug">
                    <span class="font-semibold text-violet-300">fed by</span> {{ leg.from_operator.replace('-operator', '') }} - when their {{ leg.from_process }} lands on
                    <span class="text-zinc-400">{{ leg.on_state }}</span>, your {{ leg.opens }} opens itself.
                  </template>
                </p>
              </div>
            </div>
          </div>
        </section>
        <p v-else class="mb-6 rounded-2xl border border-dashed border-zinc-800 px-4 py-6 text-center text-xs text-zinc-600">
          This operator stands alone - its machines hand nothing off (yet).
        </p>

        <!-- what you get -->
        <section class="mb-6">
          <h2 class="text-xs font-bold uppercase tracking-widest text-zinc-500">What you get</h2>
          <div class="mt-2 grid gap-1.5 sm:grid-cols-2">
            <span v-for="o in op.outcomes" :key="o" class="flex items-center gap-1.5 text-[11px] text-zinc-300">
              <CheckCircle2 class="h-3 w-3 shrink-0" :style="{ color: op.color }" /> {{ o }}
            </span>
          </div>
        </section>

        <!-- the install plan -->
        <section class="space-y-4">
          <h2 class="text-xs font-bold uppercase tracking-widest text-zinc-500">The install plan</h2>

          <div v-if="op.installs.processes.length" class="rounded-2xl border border-indigo-900/60 bg-zinc-900/40 p-4">
            <p class="flex items-center gap-1.5 text-[10px] font-bold uppercase tracking-widest text-indigo-300"><ListChecks class="h-3 w-3" /> Pre-wired machines</p>
            <div class="mt-2 space-y-2">
              <div v-for="p in op.installs.processes" :key="p.name" class="rounded-xl border border-zinc-800 bg-zinc-950/60 px-3 py-2.5">
                <div class="flex flex-wrap items-center gap-2">
                  <span class="text-xs font-bold text-zinc-200">{{ p.name }}</span>
                  <span v-if="p.escalates" class="flex items-center gap-1 rounded-full bg-amber-500/10 px-2 py-0.5 text-[9px] font-semibold text-amber-300"><BellRing class="h-2.5 w-2.5" /> escalate loops</span>
                  <span class="rounded-full bg-indigo-500/10 px-2 py-0.5 text-[9px] font-semibold text-indigo-300">{{ p.escalation }}</span>
                  <span v-if="p.seeded_from" class="text-[9px] text-zinc-600">seeded from {{ p.seeded_from }}</span>
                </div>
                <div class="mt-1.5 flex flex-wrap items-center gap-1">
                  <template v-for="(s, i) in p.states" :key="s">
                    <span class="rounded-md bg-cyan-500/10 px-1.5 py-0.5 text-[9px] text-cyan-300">{{ s }}</span>
                    <span v-if="i < p.states.length - 1" class="text-[9px] text-zinc-700">→</span>
                  </template>
                </div>
                <div v-if="p.journeys.length" class="mt-1.5 flex flex-wrap gap-1">
                  <span v-for="j in p.journeys" :key="j.on_state" class="rounded-full bg-fuchsia-500/10 px-2 py-0.5 text-[9px] font-semibold text-fuchsia-300">
                    {{ j.on_state }} → {{ j.opens }}
                  </span>
                </div>
              </div>
            </div>
          </div>

          <div class="grid gap-4 lg:grid-cols-2">
            <div class="rounded-2xl border border-zinc-800/80 bg-zinc-900/40 p-4">
              <p class="flex items-center gap-1.5 text-[10px] font-bold uppercase tracking-widest text-lime-300"><Database class="h-3 w-3" /> Datasets</p>
              <div class="mt-2 space-y-1.5">
                <div v-for="d in op.installs.datasets" :key="d.name" class="rounded-xl border border-zinc-800 bg-zinc-950/60 px-3 py-2">
                  <div class="flex items-center justify-between gap-2">
                    <span class="text-xs font-semibold text-zinc-200">{{ d.name }}</span>
                    <span class="text-[9px] text-zinc-600">{{ d.rows }} sample row{{ d.rows === 1 ? '' : 's' }} · {{ d.columns.length }} columns</span>
                  </div>
                  <p class="mt-0.5 text-[10px] text-zinc-600">{{ d.description }}</p>
                </div>
              </div>
            </div>

            <div class="rounded-2xl border border-zinc-800/80 bg-zinc-900/40 p-4">
              <p class="flex items-center gap-1.5 text-[10px] font-bold uppercase tracking-widest text-orange-300"><Layers class="h-3 w-3" /> Reactive workflows</p>
              <div class="mt-2 space-y-1.5">
                <div v-for="w in op.installs.workflows" :key="w.name" class="rounded-xl border border-zinc-800 bg-zinc-950/60 px-3 py-2">
                  <div class="flex flex-wrap items-center justify-between gap-2">
                    <span class="text-xs font-semibold text-zinc-200">{{ w.name }}</span>
                    <span class="rounded-full bg-orange-500/10 px-2 py-0.5 text-[9px] font-semibold text-orange-300">{{ w.trigger || 'dataset trigger' }}</span>
                  </div>
                  <p class="mt-0.5 text-[10px] text-zinc-600">{{ w.description }}</p>
                </div>
              </div>
            </div>
          </div>

          <div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <div v-if="op.installs.agent" class="rounded-2xl border border-zinc-800/80 bg-zinc-900/40 p-4">
              <p class="flex items-center gap-1.5 text-[10px] font-bold uppercase tracking-widest text-amber-300"><Bot class="h-3 w-3" /> The agent</p>
              <p class="mt-2 text-xs font-semibold text-zinc-200">{{ op.installs.agent.name }}</p>
              <p v-if="op.installs.agent.knowledge" class="mt-0.5 text-[10px] text-zinc-600">grounded in {{ op.installs.agent.knowledge }}</p>
            </div>
            <div v-if="op.installs.rooms.length" class="rounded-2xl border border-zinc-800/80 bg-zinc-900/40 p-4">
              <p class="flex items-center gap-1.5 text-[10px] font-bold uppercase tracking-widest text-sky-300"><Users class="h-3 w-3" /> Rooms</p>
              <div class="mt-2 space-y-1">
                <p v-for="r in op.installs.rooms" :key="r.name" class="flex items-center justify-between gap-2 text-[11px] text-zinc-300">
                  {{ r.name }} <span class="text-[9px] text-zinc-600">{{ r.modality }}</span>
                </p>
              </div>
            </div>
            <div v-if="op.installs.queues.length" class="rounded-2xl border border-zinc-800/80 bg-zinc-900/40 p-4">
              <p class="flex items-center gap-1.5 text-[10px] font-bold uppercase tracking-widest text-cyan-300"><PhoneCall class="h-3 w-3" /> Queues</p>
              <div class="mt-2 space-y-1">
                <p v-for="q in op.installs.queues" :key="q" class="text-[11px] text-zinc-300">{{ q }}</p>
              </div>
            </div>
            <div class="rounded-2xl border border-zinc-800/80 bg-zinc-900/40 p-4">
              <p class="flex items-center gap-1.5 text-[10px] font-bold uppercase tracking-widest text-emerald-300"><LayoutDashboard class="h-3 w-3" /> The rest</p>
              <div class="mt-2 space-y-1 text-[11px] text-zinc-300">
                <p v-if="op.installs.campaign">campaign: {{ op.installs.campaign }}</p>
                <p>staff dashboard: {{ op.installs.dashboard }}</p>
              </div>
            </div>
          </div>

          <div v-if="op.notes.length" class="rounded-2xl border border-zinc-800/80 bg-zinc-900/40 p-4">
            <p class="text-[10px] font-bold uppercase tracking-widest text-zinc-500">Wiring notes</p>
            <ul class="mt-2 space-y-1">
              <li v-for="n in op.notes" :key="n" class="text-[11px] leading-relaxed text-zinc-500">- {{ n }}</li>
            </ul>
          </div>
        </section>
      </template>
    </main>
  </div>
</template>

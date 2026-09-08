<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useRoute } from 'vue-router'
import {
  Loader2, CheckCircle2, Download, AlertTriangle, ExternalLink, X, Layers,
  Building2, Video, TrendingUp, HeartPulse, Boxes, LayoutDashboard, GitBranch,
  ArrowLeft, BellRing, ListChecks, Database, Bot, Users, PhoneCall, History,
  Radio,
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

const { api, streamUrl, download } = useApi()
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

// v95: the HISTORY on the operator-detail chain. The shelf view above is
// pre-install (what the chains WILL do); /processes/chains is the
// owner-wide chain map - the same legs resolved against what is actually
// INSTALLED, each carrying its ride counts (opened / moving / stuck) and
// the recent traversals. Where the two agree (from_process + on_state +
// opens), the shelf chain wears its real history: not just what the
// install BUILDS but what it has been DOING.
interface LiveHistoryRow {
  instance_id: string; process_id: string; ref: string; title: string
  state: string; opened_at: string | null; is_stuck: boolean
  overdue_seconds: number; acked_by: string | null
  snooze_remaining_seconds?: number
}
interface LiveLeg {
  opened: number; open_now: number; stuck: number
  history: LiveHistoryRow[]
  history_truncated?: boolean
}
const liveLegs = ref<Map<string, LiveLeg>>(new Map())
const liveNodes = ref<Set<string>>(new Set())
const liveLoading = ref(false)

// v96: DEEPER chain history - the map's per-leg traversal window
// stretches beyond the recent 5 (the server clamps 1..50); the toggle
// re-fetches the map at the deeper depth
const histDepth = ref(5)

function legKey(from: string, onState: string, opens: string): string {
  return `${(from || '').toLowerCase()}|${(onState || '').toLowerCase()}|${(opens || '').toLowerCase()}`
}

async function loadChainMap() {
  liveLoading.value = true
  try {
    const map = await api.get<any>(`/processes/chains?history_limit=${histDepth.value}`)
    liveNodes.value = new Set()
    liveLegs.value = new Map()
    for (const [pid, n] of Object.entries<any>(map.nodes || {}))
      if ((n as any)?.name) liveNodes.value.add((n as any).name.toLowerCase())
    for (const c of map.chains || [])
      for (const l of c.legs || [])
        liveLegs.value.set(legKey(l.from_name, l.on_state, l.to_name),
          { opened: l.opened || 0, open_now: l.open_now || 0,
            stuck: l.stuck || 0, history: l.history || [],
            history_truncated: !!l.history_truncated })
  } catch { /* shelf-only view is honest too */ }
  finally { liveLoading.value = false }
}

async function toggleDepth() {
  histDepth.value = histDepth.value >= 50 ? 5 : 50
  await loadChainMap()
}

// v98: the per-leg chain history as a CSV download - the SAME map this page
// draws, one row per traversal with the chain and the leg named on every
// row; the file honors the depth toggle's window (the server clamps 1..50).
// v99: the PLOT's own filters ride the file - a chain name (the per-chain
// button) and/or a leg "from|state" (the per-leg chip) hit the SAME
// endpoint, so the file is exactly the slice you are looking at.
const csvBusy = ref(false)
const csvError = ref('')
function _csvSlug(s: string): string {
  return s.toLowerCase().split('').map(c => /[a-z0-9]/.test(c) ? c : '-').join('').replace(/-+/g, '-').replace(/^-|-$/g, '').slice(0, 40)
}
async function exportChainCsv(chain?: string, leg?: string) {
  csvBusy.value = true
  csvError.value = ''
  try {
    const params = new URLSearchParams({ history_limit: String(histDepth.value) })
    if (chain) params.set('chain', chain)
    if (leg) params.set('leg', leg)
    const tag = chain ? `-${_csvSlug(chain)}` : leg ? `-leg-${_csvSlug(leg.replace('|', '-'))}` : ''
    await download(`/processes/chains/history.csv?${params.toString()}`,
      `py8n-chain-history${tag}-depth${histDepth.value}.csv`)
  } catch (e: any) {
    csvError.value = e?.message || 'the export was refused'
  } finally { csvBusy.value = false }
}

// v97: the chain map is LIVE on business.journey_opened - when any
// machine lands on a fire-state and the handoff opens the next leg, the
// map re-reads itself (debounced) and the leg that just gained a ride
// flashes. Same owner-scoped live tail every reactive surface rides
// since v93 (WS /events/stream); reconnect is exponential like home.
const liveTail = ref<'connecting' | 'live' | 'reconnecting'>('connecting')
const flashKey = ref('')
const lastJourneyNote = ref('')
let tailWs: WebSocket | null = null
let tailDelay = 2000
let tailTimer: ReturnType<typeof setTimeout> | null = null
let flashTimer: ReturnType<typeof setTimeout> | null = null
let mapRefreshTimer: ReturnType<typeof setTimeout> | null = null

function flashLeg(key: string) {
  flashKey.value = key
  if (flashTimer) clearTimeout(flashTimer)
  flashTimer = setTimeout(() => { flashTimer = null; flashKey.value = '' }, 5000)
}

function connectTail() {
  if (tailWs) return
  try {
    tailWs = new WebSocket(streamUrl('/api/v1/events/stream'))
  } catch {
    liveTail.value = 'reconnecting'
    scheduleTailReconnect()
    return
  }
  liveTail.value = 'connecting'
  tailWs.onopen = () => { liveTail.value = 'live'; tailDelay = 2000 }
  tailWs.onmessage = (m) => {
    try {
      const msg = JSON.parse(m.data as string)
      if (msg?.event !== 'system_event' || msg?.type !== 'business.journey_opened') return
      const p = msg.payload || {}
      const target = p.target || {}
      lastJourneyNote.value =
        `refreshed by business.journey_opened - ${p.process_name || 'a machine'} landed on ${p.on_state || '?'}` +
        (target.process_name ? ` · ${target.process_name} opened itself` : '')
      if (!mapRefreshTimer) {
        mapRefreshTimer = setTimeout(async () => {
          mapRefreshTimer = null
          await loadChainMap()
        }, 600)
      }
      if (p.process_name && p.on_state && target.process_name)
        flashLeg(legKey(p.process_name, p.on_state, target.process_name))
    } catch { /* a malformed frame is not worth the page's attention */ }
  }
  tailWs.onclose = () => { tailWs = null; liveTail.value = 'reconnecting'; scheduleTailReconnect() }
  tailWs.onerror = () => { try { tailWs?.close() } catch { /* onclose follows */ } }
}

function scheduleTailReconnect() {
  if (tailTimer) return
  tailTimer = setTimeout(() => {
    tailTimer = null
    tailDelay = Math.min(tailDelay * 2, 15000)
    connectTail()
  }, tailDelay)
}

onUnmounted(() => {
  if (flashTimer) clearTimeout(flashTimer)
  if (mapRefreshTimer) clearTimeout(mapRefreshTimer)
  if (tailTimer) clearTimeout(tailTimer)
  if (tailWs) { try { tailWs.onclose = null; tailWs.close() } catch { /* gone */ } }
  tailWs = null
})
function legLive(leg: ChainLeg): LiveLeg | null {
  return liveLegs.value.get(legKey(leg.from_process, leg.on_state, leg.opens)) || null
}
function fmtOpenedAt(iso: string | null): string {
  if (!iso) return ''
  try {
    return new Date(iso).toLocaleString([], {
      month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
  } catch { return iso }
}
function fmtOverdueShort(sec: number): string {
  if (!sec || sec <= 0) return ''
  if (sec < 3600) return `${Math.max(1, Math.round(sec / 60))}m`
  if (sec < 86400) return `${(sec / 3600).toFixed(1)}h`
  return `${(sec / 86400).toFixed(1)}d`
}
const anyLive = computed(() => liveLegs.value.size > 0)

onMounted(async () => {
  try {
    op.value = await api.get<OpDetail>(`/operators/${slug.value}`)
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'Could not load the operator'
  } finally {
    loading.value = false
  }
  // the live overlay is a soft read - an absent/empty map just means the
  // machines are not installed yet (the shelf chain stays the story)
  await loadChainMap()
  connectTail()  // v97: and from here the map moves ITSELF on journey_opened
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
            <!-- v97: the chain map is LIVE on business.journey_opened -->
            <span class="flex items-center gap-1 rounded-full px-2 py-0.5 text-[9px] font-bold"
              :class="liveTail === 'live' ? 'bg-emerald-500/10 text-emerald-300' : 'bg-amber-500/10 text-amber-300'"
              :title="liveTail === 'live' ? 'the chain map re-reads itself when a handoff opens the next leg' : 'the live tail is down - the depth toggle still re-fetches'">
              <Radio class="h-2.5 w-2.5" :class="liveTail === 'live' ? 'animate-pulse' : ''" />
              {{ liveTail === 'live' ? 'live - moves on journey_opened' : liveTail === 'reconnecting' ? 'reconnecting...' : 'connecting...' }}
            </span>
            <span v-if="lastJourneyNote" class="w-full text-[10px] text-sky-300/80">{{ lastJourneyNote }}</span>
          </div>
          <div class="space-y-4">
            <div v-for="chain in op.chains" :key="chain.slug" class="rounded-2xl border border-fuchsia-900/50 bg-zinc-900/40 p-4">
              <div class="flex flex-wrap items-center gap-2">
                <span class="rounded-full bg-fuchsia-500/15 px-2 py-0.5 text-[10px] font-bold text-fuchsia-300">{{ chain.name }} chain</span>
                <p class="text-[11px] text-zinc-500">{{ chain.story }}</p>
                <!-- v99: the plot's own filter - this chain, and only this chain, in the file -->
                <button class="flex items-center gap-0.5 rounded-full border border-cyan-500/30 px-1.5 py-0.5 text-[9px] font-bold text-cyan-300/90 transition hover:bg-cyan-500/10 disabled:opacity-50"
                  :disabled="csvBusy"
                  :title="`export THIS chain's per-leg history as CSV (at depth ${histDepth})`"
                  @click="exportChainCsv(chain.name)">
                  <Download class="h-2.5 w-2.5" /> CSV
                </button>
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
              <!-- v95: the history ON the operator-detail chain - where the
                machines are installed, the shelf chain wears its real rides -->
              <div class="mt-3 border-t border-zinc-800/60 pt-3">
                <p class="flex items-center gap-1.5 text-[9px] font-bold uppercase tracking-widest text-zinc-500">
                  <History class="h-3 w-3" /> the history so far
                  <Loader2 v-if="liveLoading" class="h-3 w-3 animate-spin text-zinc-600" />
                  <!-- v96: deeper history - stretch the traversal window beyond the recent 5 -->
                  <button class="rounded-full border border-zinc-700 px-2 py-0.5 text-[9px] font-bold normal-case tracking-normal transition"
                    :class="histDepth >= 50 ? 'border-fuchsia-500/60 text-fuchsia-300' : 'text-zinc-400 hover:border-fuchsia-500/60 hover:text-fuchsia-300'"
                    :title="histDepth >= 50 ? 'back to the 5 most recent rides' : 'load rides beyond the recent 5 (up to 50 per leg)'"
                    @click="toggleDepth">
                    {{ histDepth >= 50 ? 'depth 50 · deeper' : 'depth 5 · go deeper' }}
                  </button>
                  <!-- v98: the same history as a CSV download, at the depth chosen -->
                  <button class="flex items-center gap-1 rounded-full border border-cyan-500/40 px-2 py-0.5 text-[9px] font-bold normal-case tracking-normal text-cyan-300 transition hover:bg-cyan-500/10 disabled:opacity-50"
                    :disabled="csvBusy"
                    :title="`export the per-leg chain history as CSV - one row per traversal, the chain and the leg named on every row (depth ${histDepth})`"
                    @click="exportChainCsv">
                    <Loader2 v-if="csvBusy" class="h-2.5 w-2.5 animate-spin" />
                    <Download v-else class="h-2.5 w-2.5" /> CSV
                  </button>
                </p>
                <div v-for="leg in chain.legs" :key="`hist-${leg.from_process}-${leg.on_state}`" class="mt-1.5">
                  <div v-if="legLive(leg)" class="text-[10px] rounded-xl transition"
                    :class="flashKey === legKey(leg.from_process, leg.on_state, leg.opens) ? 'ring-1 ring-fuchsia-400/70 bg-fuchsia-500/5 px-2 py-1.5' : ''">
                    <span v-if="flashKey === legKey(leg.from_process, leg.on_state, leg.opens)" class="float-right rounded-full bg-fuchsia-500/20 px-1.5 py-0.5 text-[9px] font-bold text-fuchsia-300">just opened</span>
                    <p class="text-zinc-500">
                      <span class="font-semibold text-zinc-400">{{ leg.from_process }} --{{ leg.on_state }}--> {{ leg.opens }}</span>:
                      {{ legLive(leg)!.opened }} opened · {{ legLive(leg)!.open_now }} still moving
                      <span v-if="legLive(leg)!.stuck" class="font-bold text-rose-400">· {{ legLive(leg)!.stuck }} past SLA</span>
                      <!-- v99: the plot's own filter - this leg, and only this leg, in the file -->
                      <button class="ml-1 inline-flex items-center gap-0.5 rounded-full border border-cyan-500/30 px-1.5 py-0.5 text-[9px] font-bold text-cyan-300/90 transition hover:bg-cyan-500/10 disabled:opacity-50"
                        :disabled="csvBusy"
                        :title="`export THIS leg's rides as CSV (at depth ${histDepth})`"
                        @click="exportChainCsv(undefined, `${leg.from_process}|${leg.on_state}`)">
                        <Download class="h-2.5 w-2.5" /> CSV
                      </button>
                    </p>
                    <p v-if="legLive(leg)!.history_truncated" class="text-[9px] text-amber-400/80">
                      showing the {{ legLive(leg)!.history.length }} most recent of {{ legLive(leg)!.opened }} rides - switch the depth to go deeper
                    </p>
                    <div v-if="legLive(leg)!.history.length" class="mt-1 space-y-0.5">
                      <div v-for="hrow in legLive(leg)!.history" :key="hrow.instance_id"
                        class="flex flex-wrap items-center gap-x-2 gap-y-0.5 rounded-lg border border-zinc-800/70 bg-zinc-950/50 px-2 py-1 text-[9.5px]">
                        <span class="font-mono font-bold text-zinc-300">{{ hrow.ref }}</span>
                        <span class="truncate text-zinc-500">{{ hrow.title }}</span>
                        <span class="rounded bg-zinc-800 px-1 py-0.5 font-semibold text-zinc-400">{{ hrow.state }}</span>
                        <span class="text-zinc-600">opened {{ fmtOpenedAt(hrow.opened_at) }}</span>
                        <span v-if="hrow.is_stuck" class="font-bold text-rose-400">{{ fmtOverdueShort(hrow.overdue_seconds) }} past SLA</span>
                        <span v-if="hrow.acked_by" class="text-emerald-400/80">acked by {{ hrow.acked_by }}</span>
                        <span v-if="hrow.snooze_remaining_seconds" class="text-sky-300/80">snoozing {{ fmtOverdueShort(hrow.snooze_remaining_seconds) }}</span>
                      </div>
                    </div>
                    <p v-else class="mt-0.5 text-[9.5px] text-zinc-600">
                      installed - nothing has ridden this leg yet; land {{ leg.from_process }} on {{ leg.on_state }} and the handoff writes itself here.
                    </p>
                  </div>
                  <p v-else class="text-[9.5px] text-zinc-600">
                    {{ leg.from_process }} --{{ leg.on_state }}--> {{ leg.opens }}: the machines are not installed yet - the history starts when they are.
                  </p>
                </div>
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

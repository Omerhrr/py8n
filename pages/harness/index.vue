<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import {
  Activity, CheckCircle2, CircleDashed, Clock, Gavel, Loader2,
  Pause, Play, Plus, RadioTower, Repeat, Send, ShieldAlert, Sparkles,
  Terminal, Trash2, Wrench, XCircle, Zap,
} from 'lucide-vue-next'
import { useApi } from '~/composables/useApi'

const { api } = useApi()

// v109 - the harness: the system's own agentic runtime. A session turns one
// message into a persistent loop that reads the estate, moves it ONLY through
// fail-closed approvals, and records every frame of the ride.
// v111 - the patrol: the harness stops waiting to be asked. A mission + a
// rhythm + a session = the system firing its own rounds, receipts on the board.

interface HarnessSession {
  id: string
  name: string
  description: string
  system_prompt: string
  provider: string
  model: string
  memory: string
  is_active: boolean
  turns?: TurnSummary[]
}
interface TurnSummary {
  id: string
  user_message: string
  status: string
  reply: string
  iterations: number
  guard_blocks: number
}
interface TraceFrame {
  event: string
  iteration?: number
  reply?: string
  tool?: string
  status?: string
  preview?: string
  reason?: string
  kind?: string
  arguments?: any
  moves?: string
  answer?: string
  decision?: string
  approval_id?: string
  patrol?: string
  patrol_id?: string
}
interface TurnFull {
  id: string
  user_message: string
  status: string
  reply: string
  iterations: number
  guard_blocks: number
  tool_calls: { tool: string; status: string; result: string }[]
  trace: TraceFrame[]
  error: string
  waiting: boolean
}
interface Approval {
  id: string
  turn_id: string
  tool: string
  arguments: any
  status: string
  created_at: string | null
}
interface ToolDefView {
  name: string
  description: string
  sensitive: boolean
  moves: string
}
interface Patrol {
  id: string
  session_id: string
  session_name: string | null
  name: string
  mission: string
  interval_seconds: number
  is_active: boolean
  run_count: number
  last_run_at: string | null
  last_status: string | null
  last_run_turn_id: string | null
  last_error: string
  runs?: TurnSummary[]
}
interface Health {
  ok: boolean
  sessions: number
  turns: number
  pending_approvals: number
  patrols: number
  active_patrols: number
  guard: Record<string, number>
}

const STATUS_CLASS: Record<string, string> = {
  running: 'bg-sky-500/10 text-sky-300 border-sky-500/30',
  waiting_approval: 'bg-violet-500/10 text-violet-300 border-violet-500/30',
  completed: 'bg-emerald-500/10 text-emerald-300 border-emerald-500/30',
  exhausted: 'bg-amber-500/10 text-amber-300 border-amber-500/30',
  failed: 'bg-red-500/10 text-red-300 border-red-500/30',
  refused: 'bg-red-500/10 text-red-300 border-red-500/30',
  held: 'bg-zinc-500/10 text-zinc-400 border-zinc-500/30',
}

const sessions = ref<HarnessSession[]>([])
const selected = ref<HarnessSession | null>(null)
const turns = ref<TurnFull[]>([])
const approvals = ref<Approval[]>([])
const tools = ref<ToolDefView[]>([])
const health = ref<Health | null>(null)
const loading = ref(true)
const sending = ref(false)
const deciding = ref('')
const draft = ref('')
const showCreate = ref(false)
const newName = ref('')
const newPrompt = ref('')
const newProvider = ref('sandbox_bridge')
const error = ref('')

// v111 - the patrol pane
const pane = ref<'sessions' | 'patrols'>('sessions')
const patrols = ref<Patrol[]>([])
const selectedPatrol = ref<Patrol | null>(null)
const showPatrolCreate = ref(false)
const newPatrolName = ref('')
const newPatrolMission = ref('')
const newPatrolInterval = ref(3600)
const newPatrolSession = ref('')
const patrolBusy = ref(false)

const pendingCount = computed(() =>
  approvals.value.filter((a) => a.status === 'pending').length)

async function loadSessions(keep = true) {
  sessions.value = await api.get<HarnessSession[]>('/harness/sessions')
  if (keep && selected.value) {
    const again = sessions.value.find((s) => s.id === selected.value?.id)
    selected.value = again ?? sessions.value[0] ?? null
  } else if (!selected.value) {
    selected.value = sessions.value[0] ?? null
  }
}

async function loadAll() {
  loading.value = true
  try {
    const [t, h] = await Promise.all([
      api.get<ToolDefView[]>('/harness/tools'),
      api.get<Health>('/harness/health'),
    ])
    tools.value = t
    health.value = h
    await loadSessions()
    patrols.value = await api.get<Patrol[]>('/harness/patrols')
    if (selectedPatrol.value) {
      const again = patrols.value.find((p) => p.id === selectedPatrol.value?.id)
      selectedPatrol.value = again ?? null
    }
    if (selected.value) await select(selected.value.id, false)
    approvals.value = await api.get<Approval[]>('/harness/approvals')
  } catch (e: any) {
    error.value = e?.data?.detail || String(e)
  } finally {
    loading.value = false
  }
}

async function select(id: string, reloadSessions = true) {
  selectedPatrol.value = null
  if (reloadSessions) await loadSessions(true)
  const found = sessions.value.find((s) => s.id === id)
  selected.value = found ?? null
  if (!found) { turns.value = []; return }
  turns.value = await api.get<TurnFull[]>(`/harness/sessions/${id}/turns`)
}

// ---------------------------------------------------------------------------
// v111 - the patrols: the system firing its own rounds
// ---------------------------------------------------------------------------

function humanize(seconds: number): string {
  const s = Math.max(5, Number(seconds) || 3600)
  if (s >= 3600 && s % 3600 === 0) return `every ${s / 3600}h`
  if (s >= 60 && s % 60 === 0) return `every ${s / 60}m`
  return `every ${s}s`
}

async function selectPatrol(id: string) {
  const p = patrols.value.find((x) => x.id === id)
  if (!p) return
  await select(p.session_id, false)
  selectedPatrol.value = p
  pane.value = 'patrols'
}

async function refreshPatrols() {
  patrols.value = await api.get<Patrol[]>('/harness/patrols')
  if (selectedPatrol.value) {
    selectedPatrol.value = patrols.value.find(
      (p) => p.id === selectedPatrol.value?.id) ?? null
  }
}

async function createPatrol() {
  if (!newPatrolName.value.trim() || !newPatrolMission.value.trim() ||
      !newPatrolSession.value) return
  patrolBusy.value = true
  error.value = ''
  try {
    const made = await api.post<Patrol>('/harness/patrols', {
      session_id: newPatrolSession.value,
      name: newPatrolName.value.trim(),
      mission: newPatrolMission.value.trim(),
      interval_seconds: Math.max(5, Number(newPatrolInterval.value) || 3600),
    })
    showPatrolCreate.value = false
    newPatrolName.value = ''
    newPatrolMission.value = ''
    newPatrolInterval.value = 3600
    await loadAll()
    await selectPatrol(made.id)
  } catch (e: any) {
    error.value = e?.data?.detail || String(e)
  } finally {
    patrolBusy.value = false
  }
}

async function runPatrolNow() {
  if (!selectedPatrol.value || patrolBusy.value) return
  patrolBusy.value = true
  error.value = ''
  try {
    const receipt = await api.post<{ status: string; error: string }>(
      `/harness/patrols/${selectedPatrol.value.id}/run`)
    if (receipt.status === 'failed' || receipt.status === 'held') {
      error.value = receipt.error || `round ${receipt.status}`
    }
    await refreshPatrols()
    health.value = await api.get<Health>('/harness/health')
    approvals.value = await api.get<Approval[]>('/harness/approvals')
    if (selected.value) {
      turns.value = await api.get<TurnFull[]>(
        `/harness/sessions/${selected.value.id}/turns`)
    }
  } catch (e: any) {
    error.value = e?.data?.detail || String(e)
  } finally {
    patrolBusy.value = false
  }
}

async function togglePatrol() {
  if (!selectedPatrol.value || patrolBusy.value) return
  patrolBusy.value = true
  error.value = ''
  try {
    await api.patch(`/harness/patrols/${selectedPatrol.value.id}`, {
      is_active: !selectedPatrol.value.is_active,
    })
    await refreshPatrols()
    health.value = await api.get<Health>('/harness/health')
  } catch (e: any) {
    error.value = e?.data?.detail || String(e)
  } finally {
    patrolBusy.value = false
  }
}

async function deletePatrol() {
  if (!selectedPatrol.value || patrolBusy.value) return
  patrolBusy.value = true
  error.value = ''
  try {
    await api.delete(`/harness/patrols/${selectedPatrol.value.id}`)
    selectedPatrol.value = null
    await refreshPatrols()
    health.value = await api.get<Health>('/harness/health')
  } catch (e: any) {
    error.value = e?.data?.detail || String(e)
  } finally {
    patrolBusy.value = false
  }
}

async function createSession() {
  if (!newName.value.trim()) return
  try {
    const made = await api.post<HarnessSession>('/harness/sessions', {
      name: newName.value.trim(),
      system_prompt: newPrompt.value.trim() || undefined,
      provider: newProvider.value,
    })
    showCreate.value = false
    newName.value = ''
    newPrompt.value = ''
    selected.value = made
    await loadAll()
    await select(made.id, false)
  } catch (e: any) {
    error.value = e?.data?.detail || String(e)
  }
}

async function send() {
  if (!draft.value.trim() || !selected.value || sending.value) return
  sending.value = true
  error.value = ''
  const message = draft.value.trim()
  draft.value = ''
  try {
    const turn = await api.post<TurnFull>(
      `/harness/sessions/${selected.value.id}/turns`, { message })
    turns.value = await api.get<TurnFull[]>(
      `/harness/sessions/${selected.value.id}/turns`)
    approvals.value = await api.get<Approval[]>('/harness/approvals')
    health.value = await api.get<Health>('/harness/health')
    if (turn.status === 'failed') error.value = turn.error || 'turn failed'
  } catch (e: any) {
    error.value = e?.data?.detail || String(e)
  } finally {
    sending.value = false
  }
}
async function decide(approvalId: string, approve: boolean) {
  deciding.value = approvalId
  error.value = ''
  try {
    await api.post(`/harness/approvals/${approvalId}/${approve ? 'approve' : 'reject'}`)
    if (selected.value) {
      turns.value = await api.get<TurnFull[]>(
        `/harness/sessions/${selected.value.id}/turns`)
    }
    approvals.value = await api.get<Approval[]>('/harness/approvals')
    health.value = await api.get<Health>('/harness/health')
    await refreshPatrols()
  } catch (e: any) {
    error.value = e?.data?.detail || String(e)
  } finally {
    deciding.value = ''
  }
}

function framesOf(turn: TurnFull): TraceFrame[] {
  return (turn.trace || []).filter((f) => f.event !== 'iteration' && f.event !== 'reply')
}

function pendingFor(turn: TurnFull): Approval | undefined {
  return approvals.value.find(
    (a) => a.turn_id === turn.id && a.status === 'pending')
}

onMounted(loadAll)
</script>

<template>
  <div class="flex h-screen flex-col text-zinc-100">
    <!-- page header -->
    <header class="sticky top-0 z-20 border-b border-zinc-800/80 bg-zinc-950/90 backdrop-blur">
      <div class="mx-auto flex max-w-[110rem] flex-wrap items-center justify-between gap-3 px-4 py-3.5 sm:px-6">
        <div class="flex items-center gap-3">
          <div class="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-amber-500 to-orange-600 shadow-lg shadow-amber-500/20">
            <Gavel class="h-4 w-4 text-white" />
          </div>
          <div>
            <h1 class="text-lg font-bold tracking-tight">The Harness</h1>
            <p class="-mt-0.5 text-[11px] text-zinc-500">
              the system's own agentic runtime · reads the estate, moves or builds on it only with a human's word - and patrols it on its own rhythm
            </p>
          </div>
        </div>
        <div class="flex items-center gap-2 text-[11px] text-zinc-500">
          <span
            class="flex items-center gap-1.5 rounded-lg border px-2.5 py-1"
            :class="pendingCount ? 'border-violet-500/40 bg-violet-500/10 text-violet-300' : 'border-zinc-800 bg-zinc-900'"
          >
            <ShieldAlert class="h-3.5 w-3.5" />
            {{ pendingCount }} pending decision{{ pendingCount === 1 ? '' : 's' }}
          </span>
          <span v-if="health" class="rounded-lg border border-zinc-800 bg-zinc-900 px-2.5 py-1">
            {{ health.sessions }} sessions · {{ health.turns }} turns · {{ health.patrols }} patrol{{ health.patrols === 1 ? '' : 's' }} · guard ≤{{ health.guard.max_iterations }} rounds
          </span>
        </div>
      </div>
    </header>

    <div v-if="error" class="mx-auto mt-2 w-full max-w-[110rem] px-4 sm:px-6">
      <div class="flex items-center gap-2 rounded-lg border border-red-500/40 bg-red-500/10 px-3 py-2 text-xs text-red-300">
        <XCircle class="h-4 w-4 shrink-0" /> {{ error }}
        <button class="ml-auto text-zinc-400 hover:text-zinc-200" @click="error = ''">✕</button>
      </div>
    </div>

    <!-- three-pane work surface -->
    <div class="mx-auto flex w-full max-w-[110rem] flex-1 gap-4 overflow-hidden px-4 py-4 sm:px-6">
      <!-- sessions + patrols -->
      <aside class="hidden w-64 shrink-0 flex-col overflow-y-auto lg:flex">
        <div class="mb-3 grid grid-cols-2 gap-1 rounded-xl border border-zinc-800 bg-zinc-900/60 p-1">
          <button
            class="rounded-lg px-2 py-1.5 text-[11px] font-semibold transition"
            :class="pane === 'sessions' ? 'bg-amber-500/15 text-amber-300' : 'text-zinc-500 hover:text-zinc-300'"
            @click="pane = 'sessions'"
          >Sessions</button>
          <button
            class="rounded-lg px-2 py-1.5 text-[11px] font-semibold transition"
            :class="pane === 'patrols' ? 'bg-amber-500/15 text-amber-300' : 'text-zinc-500 hover:text-zinc-300'"
            @click="pane = 'patrols'"
          >Patrols</button>
        </div>

        <template v-if="pane === 'sessions'">
        <button
          class="mb-3 flex items-center justify-center gap-2 rounded-xl border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs font-semibold text-amber-300 transition hover:bg-amber-500/20"
          @click="showCreate = !showCreate"
        >
          <Plus class="h-3.5 w-3.5" /> New session
        </button>
        <div v-if="showCreate" class="mb-3 space-y-2 rounded-xl border border-zinc-800 bg-zinc-900/60 p-3">
          <input
            v-model="newName"
            placeholder="Session name"
            class="w-full rounded-lg border border-zinc-800 bg-zinc-950 px-2.5 py-1.5 text-xs outline-none focus:border-amber-500/50"
          />
          <select
            v-model="newProvider"
            class="w-full rounded-lg border border-zinc-800 bg-zinc-950 px-2.5 py-1.5 text-xs outline-none focus:border-amber-500/50"
          >
            <option value="sandbox_bridge">sandbox bridge</option>
            <option value="openai_compatible">openai-compatible credential</option>
          </select>
          <textarea
            v-model="newPrompt"
            rows="3"
            placeholder="System prompt (optional)"
            class="w-full resize-none rounded-lg border border-zinc-800 bg-zinc-950 px-2.5 py-1.5 text-xs outline-none focus:border-amber-500/50"
          />
          <button
            class="w-full rounded-lg bg-amber-500/20 px-2.5 py-1.5 text-xs font-semibold text-amber-300 hover:bg-amber-500/30"
            @click="createSession"
          >Open the session</button>
        </div>
        <button
          v-for="s in sessions"
          :key="s.id"
          class="mb-1.5 rounded-xl border px-3 py-2.5 text-left transition"
          :class="selected?.id === s.id
            ? 'border-amber-500/40 bg-amber-500/10'
            : 'border-zinc-800/80 bg-zinc-900/40 hover:border-zinc-700'"
          @click="select(s.id)"
        >
          <div class="flex items-center justify-between gap-2">
            <span class="truncate text-xs font-semibold">{{ s.name }}</span>
            <span
              class="shrink-0 rounded px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wide"
              :class="s.is_active ? 'bg-emerald-500/10 text-emerald-300' : 'bg-zinc-500/10 text-zinc-400'"
            >{{ s.is_active ? 'live' : 'off' }}</span>
          </div>
          <div class="mt-0.5 flex items-center gap-1.5 text-[10px] text-zinc-500">
            <Sparkles class="h-3 w-3" />
            {{ s.provider }}<span v-if="s.memory === 'buffer'"> · remembers</span>
          </div>
        </button>
        <p v-if="!loading && !sessions.length" class="px-1 text-[11px] leading-relaxed text-zinc-600">
          No sessions yet. Open one, then ask it for the estate - it reads with its tools and asks before it moves.
        </p>
        </template>

        <template v-else>
        <button
          class="mb-3 flex items-center justify-center gap-2 rounded-xl border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs font-semibold text-amber-300 transition hover:bg-amber-500/20"
          @click="showPatrolCreate = !showPatrolCreate"
        >
          <RadioTower class="h-3.5 w-3.5" /> Enlist patrol
        </button>
        <div v-if="showPatrolCreate" class="mb-3 space-y-2 rounded-xl border border-zinc-800 bg-zinc-900/60 p-3">
          <input
            v-model="newPatrolName"
            placeholder="Patrol name"
            class="w-full rounded-lg border border-zinc-800 bg-zinc-950 px-2.5 py-1.5 text-xs outline-none focus:border-amber-500/50"
          />
          <select
            v-model="newPatrolSession"
            class="w-full rounded-lg border border-zinc-800 bg-zinc-950 px-2.5 py-1.5 text-xs outline-none focus:border-amber-500/50"
          >
            <option value="" disabled>rides session…</option>
            <option v-for="s in sessions.filter((x) => x.is_active)" :key="s.id" :value="s.id">
              {{ s.name }}
            </option>
          </select>
          <textarea
            v-model="newPatrolMission"
            rows="3"
            placeholder="The mission - what to check every round (e.g. 'read the attention feed and summarize anything overdue')"
            class="w-full resize-none rounded-lg border border-zinc-800 bg-zinc-950 px-2.5 py-1.5 text-xs outline-none focus:border-amber-500/50"
          />
          <label class="flex items-center justify-between gap-2 text-[10px] text-zinc-500">
            rhythm (seconds)
            <input
              v-model.number="newPatrolInterval"
              type="number"
              min="5"
              class="w-24 rounded-lg border border-zinc-800 bg-zinc-950 px-2 py-1 text-right text-xs outline-none focus:border-amber-500/50"
            />
          </label>
          <button
            class="w-full rounded-lg bg-amber-500/20 px-2.5 py-1.5 text-xs font-semibold text-amber-300 hover:bg-amber-500/30 disabled:opacity-40"
            :disabled="patrolBusy || !newPatrolName.trim() || !newPatrolMission.trim() || !newPatrolSession"
            @click="createPatrol"
          >Enlist - the system takes the rhythm</button>
        </div>
        <button
          v-for="p in patrols"
          :key="p.id"
          class="mb-1.5 rounded-xl border px-3 py-2.5 text-left transition"
          :class="selectedPatrol?.id === p.id
            ? 'border-amber-500/40 bg-amber-500/10'
            : 'border-zinc-800/80 bg-zinc-900/40 hover:border-zinc-700'"
          @click="selectPatrol(p.id)"
        >
          <div class="flex items-center justify-between gap-2">
            <span class="truncate text-xs font-semibold">{{ p.name }}</span>
            <span
              class="shrink-0 rounded border px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wide"
              :class="STATUS_CLASS[p.last_status || ''] || 'border-zinc-800 text-zinc-500'"
            >{{ p.last_status || (p.is_active ? 'armed' : 'off') }}</span>
          </div>
          <div class="mt-0.5 flex items-center gap-1.5 text-[10px] text-zinc-500">
            <RadioTower class="h-3 w-3" />
            {{ humanize(p.interval_seconds) }} · {{ p.run_count }} round{{ p.run_count === 1 ? '' : 's' }}
            <span v-if="!p.is_active" class="text-zinc-600">· paused</span>
          </div>
        </button>
        <p v-if="!loading && !patrols.length" class="px-1 text-[11px] leading-relaxed text-zinc-600">
          No patrols yet. Enlist one on a session with a mission and a rhythm - the system fires the rounds itself, receipts on the board.
        </p>
        </template>
      </aside>

      <!-- transcript -->
      <main class="flex min-w-0 flex-1 flex-col overflow-hidden rounded-2xl border border-zinc-800/80 bg-zinc-900/30">
        <div v-if="!selected" class="flex flex-1 flex-col items-center justify-center gap-2 text-zinc-600">
          <Terminal class="h-8 w-8" />
          <p class="text-xs">Pick a session (or open one) to run the harness.</p>
        </div>
        <template v-else>
          <!-- v111: the patrol card - the receipt board of the selected patrol -->
          <div v-if="selectedPatrol" class="border-b border-zinc-800/80 bg-zinc-900/60 px-4 py-3">
            <div class="flex flex-wrap items-start justify-between gap-3">
              <div class="min-w-0">
                <div class="flex items-center gap-2">
                  <RadioTower class="h-4 w-4 shrink-0 text-amber-300" />
                  <span class="text-sm font-bold">{{ selectedPatrol.name }}</span>
                  <span
                    class="rounded border px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wide"
                    :class="STATUS_CLASS[selectedPatrol.last_status || ''] || 'border-zinc-800 text-zinc-500'"
                  >{{ selectedPatrol.last_status || (selectedPatrol.is_active ? 'armed' : 'off') }}</span>
                </div>
                <p class="mt-1 whitespace-pre-wrap text-[11px] leading-relaxed text-zinc-400">{{ selectedPatrol.mission }}</p>
              </div>
              <div class="flex shrink-0 items-center gap-2">
                <button
                  class="rounded-lg bg-amber-500/20 px-3 py-1.5 text-[11px] font-semibold text-amber-300 hover:bg-amber-500/30 disabled:opacity-40"
                  :disabled="patrolBusy"
                  @click="runPatrolNow"
                >
                  <Loader2 v-if="patrolBusy" class="mr-1 inline h-3 w-3 animate-spin" />
                  <Repeat v-else class="mr-1 inline h-3 w-3" /> Run a round now
                </button>
                <button
                  class="rounded-lg px-2.5 py-1.5 text-[11px] font-semibold disabled:opacity-40"
                  :class="selectedPatrol.is_active ? 'bg-zinc-800 text-zinc-300 hover:bg-zinc-700' : 'bg-emerald-500/20 text-emerald-300 hover:bg-emerald-500/30'"
                  :disabled="patrolBusy"
                  @click="togglePatrol"
                >
                  <Pause v-if="selectedPatrol.is_active" class="mr-1 inline h-3 w-3" />
                  <Play v-else class="mr-1 inline h-3 w-3" />
                  {{ selectedPatrol.is_active ? 'Pause' : 'Resume' }}
                </button>
                <button
                  class="rounded-lg bg-red-500/15 px-2.5 py-1.5 text-[11px] font-semibold text-red-300 hover:bg-red-500/25 disabled:opacity-40"
                  :disabled="patrolBusy"
                  @click="deletePatrol"
                ><Trash2 class="h-3 w-3" /></button>
              </div>
            </div>
            <div class="mt-2.5 flex flex-wrap items-center gap-x-4 gap-y-1 text-[10px] text-zinc-500">
              <span>rides <span class="text-zinc-300">{{ selectedPatrol.session_name || 'session' }}</span></span>
              <span>{{ humanize(selectedPatrol.interval_seconds) }}</span>
              <span>{{ selectedPatrol.run_count }} round{{ selectedPatrol.run_count === 1 ? '' : 's' }} fired</span>
              <span v-if="selectedPatrol.last_run_at">last at {{ new Date(selectedPatrol.last_run_at).toLocaleString() }}</span>
              <span v-if="selectedPatrol.last_error" class="text-red-300">{{ selectedPatrol.last_error }}</span>
            </div>
          </div>
          <div class="flex-1 space-y-4 overflow-y-auto p-4">
            <div
              v-for="turn in turns"
              :key="turn.id"
              class="rounded-xl border border-zinc-800 bg-zinc-900/60 p-3.5"
            >
              <div class="flex items-start justify-between gap-3">
                <p class="whitespace-pre-wrap text-sm text-zinc-200">{{ turn.user_message }}</p>
                <span
                  class="shrink-0 rounded border px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wide"
                  :class="STATUS_CLASS[turn.status] || 'border-zinc-800 text-zinc-400'"
                >{{ turn.status.replace('_', ' ') }}</span>
              </div>

              <!-- the loop's frames -->
              <div class="mt-3 space-y-1.5 border-l-2 border-zinc-800 pl-3">
                <div
                  v-for="(f, i) in framesOf(turn)"
                  :key="i"
                  class="text-[11px]"
                >
                  <div v-if="f.event === 'tool_call'" class="flex items-center gap-1.5">
                    <Wrench class="h-3 w-3 text-zinc-500" />
                    <span class="font-mono text-zinc-300">{{ f.tool }}</span>
                    <span
                      class="rounded px-1 py-0.5 text-[9px] font-bold"
                      :class="f.status === 'ok' ? 'bg-emerald-500/10 text-emerald-300' : 'bg-red-500/10 text-red-300'"
                    >{{ f.status }}</span>
                  </div>
                  <div v-else-if="f.event === 'tool_result'" class="truncate pl-[18px] font-mono text-[10px] text-zinc-500" :title="f.preview">
                    {{ f.preview }}
                  </div>
                  <div v-else-if="f.event === 'patrol_start'" class="flex items-center gap-1.5 text-amber-300">
                    <RadioTower class="h-3 w-3" /> patrol round - fired by the system<span v-if="f.patrol" class="text-zinc-500"> ({{ f.patrol }})</span>
                  </div>
                  <div v-else-if="f.event === 'approval_requested'" class="rounded-lg border border-violet-500/40 bg-violet-500/10 px-2.5 py-2">
                    <div class="flex items-center gap-1.5 font-semibold text-violet-300">
                      <ShieldAlert class="h-3.5 w-3.5" /> the gate: {{ f.tool }} waits for a human
                    </div>
                    <p class="mt-1 text-zinc-400">{{ f.moves }} · args <span class="font-mono text-[10px]">{{ JSON.stringify(f.arguments) }}</span></p>
                  </div>
                  <div v-else-if="f.event === 'approval_decided'" class="flex items-center gap-1.5 pl-[18px]"
                    :class="f.decision === 'approved' ? 'text-emerald-300' : 'text-red-300'">
                    <CheckCircle2 v-if="f.decision === 'approved'" class="h-3 w-3" />
                    <XCircle v-else class="h-3 w-3" />
                    human {{ f.decision }} <span v-if="f.tool" class="font-mono">{{ f.tool }}</span>
                    <span v-if="f.status" class="text-zinc-500">({{ f.status }})</span>
                  </div>
                  <div v-else-if="f.event === 'guard'" class="flex items-start gap-1.5 text-amber-300">
                    <Zap class="h-3 w-3 shrink-0" /> <span>{{ f.reason }}</span>
                  </div>
                  <div v-else-if="f.event === 'guard_stop'" class="flex items-start gap-1.5 text-red-300">
                    <Clock class="h-3 w-3 shrink-0" /> <span>{{ f.reason }}</span>
                  </div>
                  <div v-else-if="f.event === 'approval_expired'" class="flex items-center gap-1.5 text-red-300">
                    <Clock class="h-3 w-3" /> approval expired - refused on silence
                  </div>
                </div>
              </div>

              <!-- the decision panel -->
              <div
                v-if="turn.waiting && pendingFor(turn)"
                class="mt-3 flex flex-wrap items-center gap-2 rounded-lg border border-violet-500/40 bg-violet-500/5 px-3 py-2.5"
              >
                <span class="text-xs text-violet-300">
                  Decide <span class="font-mono">{{ pendingFor(turn)!.tool }}</span>?
                </span>
                <div class="ml-auto flex gap-2">
                  <button
                    class="rounded-lg bg-emerald-500/20 px-3 py-1.5 text-[11px] font-bold text-emerald-300 hover:bg-emerald-500/30 disabled:opacity-40"
                    :disabled="deciding === pendingFor(turn)!.id"
                    @click="decide(pendingFor(turn)!.id, true)"
                  >
                    <Loader2 v-if="deciding === pendingFor(turn)!.id" class="mr-1 inline h-3 w-3 animate-spin" />Approve &amp; run
                  </button>
                  <button
                    class="rounded-lg bg-red-500/20 px-3 py-1.5 text-[11px] font-bold text-red-300 hover:bg-red-500/30 disabled:opacity-40"
                    :disabled="deciding === pendingFor(turn)!.id"
                    @click="decide(pendingFor(turn)!.id, false)"
                  >Reject</button>
                </div>
              </div>

              <!-- the answer -->
              <div v-if="turn.reply" class="mt-3 rounded-lg bg-zinc-800/50 px-3 py-2.5 text-sm leading-relaxed">
                {{ turn.reply }}
              </div>
              <div v-if="turn.status === 'exhausted' || turn.status === 'failed' || turn.status === 'refused'" class="mt-2 text-[11px] text-red-300">
                {{ turn.error }}
              </div>
              <div class="mt-2 flex items-center gap-3 text-[10px] text-zinc-600">
                <span class="flex items-center gap-1"><Activity class="h-3 w-3" /> {{ turn.iterations }} rounds</span>
                <span v-if="turn.guard_blocks" class="flex items-center gap-1 text-amber-400/70">
                  <Zap class="h-3 w-3" /> {{ turn.guard_blocks }} guard block{{ turn.guard_blocks === 1 ? '' : 's' }}
                </span>
              </div>
            </div>
            <p v-if="!turns.length" class="py-8 text-center text-xs text-zinc-600">
              No turns yet - ask the harness something.
            </p>
          </div>

          <!-- composer -->
          <div class="border-t border-zinc-800/80 p-3">
            <div class="flex items-center gap-2 rounded-xl border border-zinc-800 bg-zinc-950 px-3 py-2 focus-within:border-amber-500/50">
              <input
                v-model="draft"
                placeholder="Ask the harness… (it answers from the estate, drafts machines, and asks before it moves or builds)"
                class="min-w-0 flex-1 bg-transparent text-sm outline-none placeholder:text-zinc-600"
                :disabled="sending"
                @keydown.enter="send"
              />
              <button
                class="flex items-center gap-1.5 rounded-lg bg-amber-500/20 px-3 py-1.5 text-xs font-semibold text-amber-300 transition hover:bg-amber-500/30 disabled:opacity-40"
                :disabled="sending || !draft.trim()"
                @click="send"
              >
                <Loader2 v-if="sending" class="h-3.5 w-3.5 animate-spin" />
                <Send v-else class="h-3.5 w-3.5" />
                {{ sending ? 'running the loop…' : 'Run' }}
              </button>
            </div>
          </div>
        </template>
      </main>

      <!-- the toolchest -->
      <aside class="hidden w-72 shrink-0 overflow-y-auto xl:block">
        <h2 class="mb-2 flex items-center gap-1.5 text-[11px] font-bold uppercase tracking-wider text-zinc-500">
          <Wrench class="h-3.5 w-3.5" /> The toolchest ({{ tools.length }})
        </h2>
        <div
          v-for="t in tools"
          :key="t.name"
          class="mb-2 rounded-xl border px-3 py-2.5"
          :class="t.sensitive ? 'border-violet-500/30 bg-violet-500/5' : 'border-zinc-800/80 bg-zinc-900/40'"
        >
          <div class="flex items-center gap-2">
            <component
              :is="t.sensitive ? ShieldAlert : CircleDashed"
              class="h-3.5 w-3.5 shrink-0"
              :class="t.sensitive ? 'text-violet-300' : 'text-zinc-500'"
            />
            <span class="font-mono text-xs font-semibold text-zinc-200">{{ t.name }}</span>
            <span
              v-if="t.sensitive"
              class="ml-auto rounded bg-violet-500/15 px-1.5 py-0.5 text-[9px] font-bold uppercase text-violet-300"
            >gated</span>
          </div>
          <p class="mt-1 text-[10px] leading-relaxed text-zinc-500">{{ t.description }}</p>
        </div>
        <p class="mt-3 px-1 text-[10px] leading-relaxed text-zinc-600">
          Sensitive tools never run on the model's word alone: the turn pauses, a slip
          enters the queue, and the move happens exactly when a human approves - at
          decision time, not ask time.
        </p>
      </aside>
    </div>
  </div>
</template>

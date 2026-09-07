<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import {
  GitBranch, Loader2, AlertTriangle, Plus, Clock, CheckCircle2, XCircle,
  Flag, RefreshCw, ChevronRight, ListChecks, BellRing, PenLine, CheckCheck,
} from 'lucide-vue-next'
import { useApi } from '~/composables/useApi'

// v84: Business Processes - long-running autonomy. Workflows are moments
// (trigger in, run, done); a business process is the shape of something
// that stays open for days or months (a lead pipeline, a support case, a
// claim). The machine = states + named transitions; instances = the
// tracked entities that REMEMBER state + context; every advance is on
// the record and emits business.state_changed (workflows react).
// v88: the memory door (annotate - agents write facts without moving the
// machine) and the receipt (acknowledge the escalation, the door quiets).

interface ProcessDef {
  id: string; name: string; description: string
  states: string[]; initial: string
  transitions: { name: string; from: string; to: string; description?: string }[]
  terminal_states: string[]
  escalation_policy: { channel: string; to: string; repeat_every_seconds: number
    max_repeats: number; message_template: string } | null
  escalation_summary: string
  instance_counts: Record<string, number>
  created_at: string
}
interface Instance {
  id: string; process_id: string; ref: string; title: string; state: string
  context: Record<string, any>
  entered_state_at: string | null; due_at: string | null; ended_at: string | null
  age_in_state_seconds: number; is_terminal: boolean; is_stuck: boolean
  journey?: { from_state: string | null; to_state: string; transition: string; actor: string; note: string; at: string }[]
}
interface Analytics {
  instances: number; open: number; by_state: Record<string, number>
  stuck: string[]; stuck_count: number; escalations: number
  mean_time_in_state_seconds: Record<string, number>
  advance_counts: Record<string, number>
  terminal_states: string[]
}

const { api } = useApi()
const loading = ref(true)
const pageError = ref('')
const processes = ref<ProcessDef[]>([])
const selected = ref<ProcessDef | null>(null)
const instances = ref<Instance[]>([])
const analytics = ref<Analytics | null>(null)
const detail = ref<Instance | null>(null)
const loadingDetail = ref(false)

const newStateRef = ref('')
const newStateTitle = ref('')
const newStateDue = ref('')
const starting = ref(false)
const startError = ref('')
const advanceTransition = ref('')
const advanceNote = ref('')
const advancing = ref(false)
const advanceError = ref('')
const advanceForId = ref('')
const sweeping = ref(false)
const sweepNote = ref('')

// v88: the annotate form + the ack form (one open row at a time)
const annotateForId = ref('')
const annKey = ref('')
const annValue = ref('')
const annNote = ref('')
const annotating = ref(false)
const annotateError = ref('')
const ackForId = ref('')
const ackBy = ref('')
const ackNote = ref('')
const acking = ref(false)
const ackError = ref('')

// v85: the scheduler door, run by hand - the same sweep the APScheduler
// loop ticks on its interval, on demand, for the operator who just fixed
// a process and wants to see the door move now.
async function runEscalationSweep() {
  sweeping.value = true
  sweepNote.value = ''
  pageError.value = ''
  try {
    const out = await api.post<{ stuck: number; escalated: any[]; recorded: any[]; held: any[]; already: number }>(
      '/scheduler/escalations/tick', {})
    sweepNote.value = `swept: ${out.stuck} stuck - ${out.escalated.length} moved through the machine, ` +
      `${out.recorded.length} recorded, ${out.held.length} held, ${out.already} already escalated this stint`
    await refreshAll()
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'The escalation sweep failed'
  } finally {
    sweeping.value = false
  }
}

function fmtAge(sec: number): string {
  if (sec < 60) return `${sec}s`
  if (sec < 3600) return `${Math.floor(sec / 60)}m`
  if (sec < 86400) return `${Math.floor(sec / 3600)}h`
  return `${Math.floor(sec / 86400)}d`
}

// v87: the episode bookkeeping the door keeps on the instance's memory
// v88: the ack rides the same book (the door holds acknowledged episodes)
function escBook(inst: Instance): { count: number; last_delivery: string; acked: { by: string; at: string; note?: string } | null } | null {
  const b = inst.context?.escalations
  if (!b || typeof b !== 'object') return null
  return {
    count: b.count || 0,
    last_delivery: b.last_delivery || '',
    acked: b.acked && typeof b.acked === 'object' ? b.acked : null,
  }
}

// v88: journey rows carry paperwork too - color it by what it means
function journeyChipClass(transition: string): string {
  if (transition === 'annotate') return 'bg-violet-500/15 text-violet-300'
  if (transition === 'escalation_acknowledged') return 'bg-emerald-500/15 text-emerald-300'
  if (transition === 'escalated' || transition === 'escalate') return 'bg-amber-500/15 text-amber-300'
  return 'bg-cyan-500/10 text-cyan-300'
}

function allowedFrom(state: string) {
  if (!selected.value) return []
  return selected.value.transitions.filter(t => t.from === state)
}

async function refreshAll() {
  if (!selected.value) return
  const [p, inst, a] = await Promise.all([
    api.get<ProcessDef>(`/processes/${selected.value.id}`),
    api.get<{ instances: Instance[] }>(
      `/processes/${selected.value.id}/instances${stateFilter.value ? `?state=${stateFilter.value}` : ''}`),
    api.get<Analytics>(`/processes/${selected.value.id}/analytics`),
  ])
  selected.value = p
  instances.value = inst.instances
  analytics.value = a
}

const stateFilter = ref('')

async function openProcess(p: ProcessDef) {
  loadingDetail.value = true
  pageError.value = ''
  detail.value = null
  advanceForId.value = ''
  stateFilter.value = ''
  try {
    selected.value = await api.get<ProcessDef>(`/processes/${p.id}`)
    const [inst, a] = await Promise.all([
      api.get<{ instances: Instance[] }>(`/processes/${p.id}/instances`),
      api.get<Analytics>(`/processes/${p.id}/analytics`),
    ])
    instances.value = inst.instances
    analytics.value = a
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'Could not load the process'
  } finally {
    loadingDetail.value = false
  }
}

async function startInstance() {
  if (!selected.value || !newStateRef.value.trim()) return
  starting.value = true
  startError.value = ''
  try {
    await api.post(`/processes/${selected.value.id}/instances`, {
      ref: newStateRef.value.trim(),
      title: newStateTitle.value.trim(),
      due_in_seconds: newStateDue.value ? Number(newStateDue.value) : null,
    })
    newStateRef.value = ''
    newStateTitle.value = ''
    newStateDue.value = ''
    await refreshAll()
  } catch (e: any) {
    startError.value = e?.data?.detail || e?.message || 'Could not start the instance'
  } finally {
    starting.value = false
  }
}

function openAdvance(inst: Instance) {
  advanceForId.value = inst.id
  advanceTransition.value = ''
  advanceNote.value = ''
  advanceError.value = ''
}

// v88: the annotate door - facts land on the entity's memory
function openAnnotate(inst: Instance) {
  annotateForId.value = inst.id
  annKey.value = ''
  annValue.value = ''
  annNote.value = ''
  annotateError.value = ''
}

async function annotate(inst: Instance) {
  if (!selected.value || !annKey.value.trim()) return
  annotating.value = true
  annotateError.value = ''
  let value: any = annValue.value
  try { value = JSON.parse(annValue.value) } catch { /* keep the raw string */ }
  try {
    await api.post(
      `/processes/${selected.value.id}/instances/${inst.id}/annotate`,
      { context_patch: { [annKey.value.trim()]: value }, note: annNote.value.trim(), actor: 'staff' })
    annotateForId.value = ''
    annKey.value = ''
    annValue.value = ''
    annNote.value = ''
    await refreshAll()
  } catch (e: any) {
    annotateError.value = e?.data?.detail || e?.message || 'The annotation was refused'
  } finally {
    annotating.value = false
  }
}

// v88: the receipt - acknowledge the escalation, the door quiets
function openAck(inst: Instance) {
  ackForId.value = inst.id
  ackBy.value = ''
  ackNote.value = ''
  ackError.value = ''
}

async function ackEscalation(inst: Instance) {
  if (!selected.value || !ackBy.value.trim()) return
  acking.value = true
  ackError.value = ''
  try {
    await api.post(
      `/processes/${selected.value.id}/instances/${inst.id}/escalations/ack`,
      { by: ackBy.value.trim(), note: ackNote.value.trim() })
    ackForId.value = ''
    ackBy.value = ''
    ackNote.value = ''
    await refreshAll()
  } catch (e: any) {
    ackError.value = e?.data?.detail || e?.message || 'The acknowledgement was refused'
  } finally {
    acking.value = false
  }
}

async function advance(inst: Instance) {
  if (!selected.value || !advanceTransition.value) return
  advancing.value = true
  advanceError.value = ''
  try {
    await api.post(
      `/processes/${selected.value.id}/instances/${inst.id}/advance`,
      { transition: advanceTransition.value, note: advanceNote.value.trim() })
    advanceForId.value = ''
    advanceTransition.value = ''
    advanceNote.value = ''
    await refreshAll()
  } catch (e: any) {
    advanceError.value = e?.data?.detail || e?.message || 'The machine refused the move'
  } finally {
    advancing.value = false
  }
}

async function openJourney(inst: Instance) {
  try {
    detail.value = await api.get<Instance>(
      `/processes/${selected.value?.id}/instances/${inst.id}`)
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'Could not load the journey'
  }
}

onMounted(async () => {
  try {
    const res = await api.get<{ processes: ProcessDef[] }>('/processes')
    processes.value = res.processes
    if (processes.value.length) await openProcess(processes.value[0])
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'Could not load the processes'
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
          <div class="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-emerald-500 to-cyan-500 shadow-lg shadow-emerald-500/20">
            <GitBranch class="h-4 w-4 text-white" />
          </div>
          <div class="min-w-0 flex-1">
            <h1 class="text-lg font-bold tracking-tight">Business Processes</h1>
            <p class="-mt-0.5 text-[11px] text-zinc-500">Long-running autonomy - state machines that remember across days and months; every move on the record, workflows reacting to business.state_changed.</p>
          </div>
          <button class="flex items-center gap-1.5 rounded-xl border border-zinc-800 bg-zinc-900 px-3 py-2 text-xs text-zinc-400 transition hover:text-zinc-200" @click="selected && refreshAll()">
            <RefreshCw class="h-3.5 w-3.5" /> Refresh
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

      <div v-else class="grid gap-5 lg:grid-cols-[300px_1fr]">
        <!-- the machine shelf -->
        <aside class="space-y-2">
          <button
            v-for="p in processes" :key="p.id"
            class="w-full rounded-2xl border p-4 text-left transition"
            :class="selected?.id === p.id
              ? 'border-emerald-500/50 bg-emerald-500/5'
              : 'border-zinc-800/80 bg-zinc-900/50 hover:border-zinc-700'"
            @click="openProcess(p)"
          >
            <div class="flex items-center justify-between gap-2">
              <p class="text-sm font-bold">{{ p.name }}</p>
              <span class="rounded-full bg-zinc-800 px-2 py-0.5 text-[9px] font-semibold text-zinc-300">{{ Object.values(p.instance_counts || {}).reduce((a, b) => a + b, 0) }} tracked</span>
            </div>
            <div class="mt-2 flex flex-wrap gap-1">
              <span
                v-for="s in p.states.slice(0, 6)" :key="s"
                class="rounded-full px-1.5 py-0.5 text-[9px]"
                :class="s === p.initial ? 'bg-emerald-500/15 text-emerald-300' : (p.terminal_states.includes(s) ? 'bg-zinc-700/60 text-zinc-400' : 'bg-cyan-500/10 text-cyan-300')"
              >{{ s }}</span>
              <span v-if="p.states.length > 6" class="text-[9px] text-zinc-600">+{{ p.states.length - 6 }}</span>
            </div>
          </button>
          <p v-if="!processes.length" class="rounded-2xl border border-dashed border-zinc-800 py-10 text-center text-xs text-zinc-600">
            No machines yet - POST /api/v1/processes with {states, initial, transitions}.
          </p>
        </aside>

        <!-- the selected machine -->
        <section v-if="selected" class="space-y-5">
          <div class="rounded-2xl border border-zinc-800/80 bg-zinc-900/50 p-5">
            <div class="flex flex-wrap items-center gap-2">
              <h2 class="text-base font-bold">{{ selected.name }}</h2>
              <span v-for="t in selected.terminal_states" :key="t" class="rounded-full bg-zinc-700/60 px-2 py-0.5 text-[9px] font-semibold text-zinc-300">terminal: {{ t }}</span>
            </div>
            <p class="mt-1 text-[11px] text-zinc-500">{{ selected.description || 'the business state machine' }}</p>
            <!-- v87: the escalation policy - who gets told, how often -->
            <div class="mt-2 flex flex-wrap items-center gap-2">
              <span v-if="selected.escalation_summary && selected.escalation_summary !== 'no escalation policy'" class="flex items-center gap-1 rounded-full bg-indigo-500/10 px-2 py-0.5 text-[9px] font-semibold text-indigo-300">
                <BellRing class="h-2.5 w-2.5" /> {{ selected.escalation_summary }}
              </span>
              <span v-if="selected.escalation_policy && !selected.escalation_policy.to" class="text-[9px] text-zinc-600">bind escalation_policy.to + a channel endpoint to deliver</span>
            </div>
            <!-- the pipeline -->
            <div class="mt-4 flex flex-wrap items-center gap-1.5">
              <template v-for="(s, i) in selected.states" :key="s">
                <div class="flex items-center gap-1.5">
                  <div
                    class="rounded-xl border px-2.5 py-1.5 text-center"
                    :class="analytics?.by_state?.[s]
                      ? 'border-cyan-500/50 bg-cyan-500/10'
                      : 'border-zinc-800 bg-zinc-950/60'"
                  >
                    <p class="text-[10px] font-semibold" :class="analytics?.by_state?.[s] ? 'text-cyan-300' : 'text-zinc-500'">{{ s }}</p>
                    <p class="text-sm font-bold" :class="analytics?.by_state?.[s] ? 'text-cyan-200' : 'text-zinc-700'">{{ analytics?.by_state?.[s] || 0 }}</p>
                  </div>
                  <ChevronRight v-if="i < selected.states.length - 1" class="h-3 w-3 text-zinc-700" />
                </div>
              </template>
            </div>
            <div class="mt-4 grid grid-cols-2 gap-2 sm:grid-cols-5">
              <div class="rounded-xl border border-zinc-800 bg-zinc-950/60 px-3 py-2">
                <p class="text-[9px] uppercase tracking-widest text-zinc-600">instances</p>
                <p class="text-sm font-bold text-zinc-200">{{ analytics?.instances ?? '-' }}</p>
              </div>
              <div class="rounded-xl border border-zinc-800 bg-zinc-950/60 px-3 py-2">
                <p class="text-[9px] uppercase tracking-widest text-zinc-600">open</p>
                <p class="text-sm font-bold text-cyan-300">{{ analytics?.open ?? '-' }}</p>
              </div>
              <div class="rounded-xl border border-zinc-800 bg-zinc-950/60 px-3 py-2">
                <p class="text-[9px] uppercase tracking-widest text-zinc-600">stuck (SLA)</p>
                <p class="text-sm font-bold" :class="(analytics?.stuck_count || 0) > 0 ? 'text-rose-300' : 'text-zinc-200'">{{ analytics?.stuck_count ?? '-' }}</p>
              </div>
              <div class="rounded-xl border border-zinc-800 bg-zinc-950/60 px-3 py-2">
                <p class="text-[9px] uppercase tracking-widest text-zinc-600">escalations</p>
                <p class="text-sm font-bold" :class="(analytics?.escalations || 0) > 0 ? 'text-amber-300' : 'text-zinc-200'">{{ analytics?.escalations ?? '-' }}</p>
              </div>
              <div class="rounded-xl border border-zinc-800 bg-zinc-950/60 px-3 py-2">
                <p class="text-[9px] uppercase tracking-widest text-zinc-600">moves on record</p>
                <p class="text-sm font-bold text-zinc-200">{{ Object.values(analytics?.advance_counts || {}).reduce((a, b) => a + b, 0) }}</p>
              </div>
            </div>
            <div class="mt-3 flex flex-wrap items-center gap-2">
              <button
                class="flex items-center gap-1.5 rounded-xl border border-amber-500/40 bg-amber-500/10 px-3 py-1.5 text-[10px] font-bold text-amber-300 transition hover:bg-amber-500/20 disabled:opacity-50"
                :disabled="sweeping"
                @click="runEscalationSweep">
                <Loader2 v-if="sweeping" class="h-3 w-3 animate-spin" />
                <BellRing v-else class="h-3 w-3" /> Escalation sweep (the scheduler door, now)
              </button>
              <p v-if="sweepNote" class="text-[10px] text-amber-300/80">{{ sweepNote }}</p>
            </div>
          </div>

          <!-- start an instance -->
          <div class="rounded-2xl border border-zinc-800/80 bg-zinc-900/50 p-5">
            <p class="flex items-center gap-1.5 text-[10px] font-bold uppercase tracking-widest text-zinc-400"><Plus class="h-3 w-3" /> Track something new</p>
            <div class="mt-3 grid gap-2 sm:grid-cols-[1fr_1fr_140px_auto]">
              <input v-model="newStateRef" placeholder="ref (LEAD-1042, +1555...)" class="rounded-xl border border-zinc-800 bg-zinc-950 px-3 py-2 text-xs text-zinc-200 outline-none focus:border-emerald-500/60" />
              <input v-model="newStateTitle" placeholder="title (Globex - Dana)" class="rounded-xl border border-zinc-800 bg-zinc-950 px-3 py-2 text-xs text-zinc-200 outline-none focus:border-emerald-500/60" />
              <input v-model="newStateDue" type="number" min="1" placeholder="SLA seconds" class="rounded-xl border border-zinc-800 bg-zinc-950 px-3 py-2 text-xs text-zinc-200 outline-none focus:border-emerald-500/60" />
              <button class="flex items-center justify-center gap-1.5 rounded-xl bg-emerald-500 px-4 py-2 text-xs font-bold text-zinc-950 transition hover:bg-emerald-400 disabled:opacity-50" :disabled="starting || !newStateRef.trim()" @click="startInstance">
                <Loader2 v-if="starting" class="h-3.5 w-3.5 animate-spin" />
                <Flag v-else class="h-3.5 w-3.5" /> Start in {{ selected.initial }}
              </button>
            </div>
            <p v-if="startError" class="mt-2 text-[11px] text-rose-300">{{ startError }}</p>
          </div>

          <!-- the tracked entities -->
          <div class="rounded-2xl border border-zinc-800/80 bg-zinc-900/50 p-5">
            <div class="flex flex-wrap items-center justify-between gap-2">
              <p class="flex items-center gap-1.5 text-[10px] font-bold uppercase tracking-widest text-zinc-400"><ListChecks class="h-3 w-3" /> Tracked - the ones that remember</p>
              <select v-model="stateFilter" class="rounded-lg border border-zinc-800 bg-zinc-950 px-2 py-1 text-[10px] text-zinc-300" @change="refreshAll()">
                <option value="">all states</option>
                <option v-for="s in selected.states" :key="s" :value="s">{{ s }}</option>
              </select>
            </div>
            <div class="mt-3 space-y-2">
              <div v-for="inst in instances" :key="inst.id" class="rounded-xl border border-zinc-800 bg-zinc-950/60 px-3 py-2.5">
                <div class="flex flex-wrap items-center gap-2">
                  <span class="rounded-lg px-2 py-0.5 text-[10px] font-bold"
                    :class="inst.is_terminal ? 'bg-zinc-700/60 text-zinc-300' : 'bg-emerald-500/15 text-emerald-300'">{{ inst.state }}</span>
                  <span class="text-xs font-semibold text-zinc-200">{{ inst.ref }}</span>
                  <span class="truncate text-[10px] text-zinc-500">{{ inst.title }}</span>
                  <span class="ml-auto flex items-center gap-1 text-[10px] text-zinc-600"><Clock class="h-3 w-3" /> {{ fmtAge(inst.age_in_state_seconds) }} in state</span>
                  <span v-if="inst.is_stuck" class="rounded-full bg-rose-500/15 px-1.5 py-0.5 text-[9px] font-bold text-rose-300">stuck</span>
                  <span v-if="escBook(inst)?.acked" class="rounded-full bg-emerald-500/15 px-1.5 py-0.5 text-[9px] font-bold text-emerald-300" :title="`acknowledged by ${escBook(inst)!.acked!.by}${escBook(inst)!.acked!.note ? ` - ${escBook(inst)!.acked!.note}` : ''}`"><CheckCheck class="mr-0.5 inline h-2.5 w-2.5" /> ack by {{ escBook(inst)!.acked!.by }}</span>
                  <span v-else-if="escBook(inst)" class="rounded-full bg-indigo-500/15 px-1.5 py-0.5 text-[9px] font-bold text-indigo-300" :title="`last delivery: ${escBook(inst)!.last_delivery || 'n/a'}`">escalated ×{{ escBook(inst)!.count }}</span>
                  <span v-if="inst.is_terminal" class="flex items-center gap-1 text-[10px] text-zinc-500"><CheckCircle2 class="h-3 w-3" /> closed</span>
                </div>
                <div class="mt-2 flex flex-wrap items-center gap-2">
                  <template v-if="allowedFrom(inst.state).length">
                    <template v-if="advanceForId === inst.id">
                      <select v-model="advanceTransition" class="rounded-lg border border-zinc-800 bg-zinc-950 px-2 py-1 text-[10px] text-zinc-300">
                        <option value="">move...</option>
                        <option v-for="t in allowedFrom(inst.state)" :key="t.name" :value="t.name">{{ t.name }} -> {{ t.to }}</option>
                      </select>
                      <input v-model="advanceNote" placeholder="note" class="w-40 rounded-lg border border-zinc-800 bg-zinc-950 px-2 py-1 text-[10px] text-zinc-300 outline-none focus:border-emerald-500/60" />
                      <button class="rounded-lg bg-emerald-500/90 px-2.5 py-1 text-[10px] font-bold text-zinc-950 transition hover:bg-emerald-400 disabled:opacity-50" :disabled="advancing || !advanceTransition" @click="advance(inst)">
                        <Loader2 v-if="advancing" class="h-3 w-3 animate-spin" /> Advance
                      </button>
                    </template>
                    <button v-else class="rounded-lg border border-emerald-500/40 bg-emerald-500/10 px-2.5 py-1 text-[10px] font-bold text-emerald-300 transition hover:bg-emerald-500/20" @click="openAdvance(inst)">
                      Advance
                    </button>
                  </template>
                  <span v-else class="text-[10px] text-zinc-600">terminal - the machine has no outgoing moves</span>
                  <button class="rounded-lg border border-violet-500/40 bg-violet-500/10 px-2.5 py-1 text-[10px] font-bold text-violet-300 transition hover:bg-violet-500/20" @click="annotateForId === inst.id ? (annotateForId = '') : openAnnotate(inst)">
                    <PenLine class="mr-0.5 inline h-2.5 w-2.5" /> Annotate
                  </button>
                  <button v-if="escBook(inst) && !escBook(inst)!.acked" class="rounded-lg border border-emerald-500/40 bg-emerald-500/10 px-2.5 py-1 text-[10px] font-bold text-emerald-300 transition hover:bg-emerald-500/20" @click="ackForId === inst.id ? (ackForId = '') : openAck(inst)">
                    <CheckCheck class="mr-0.5 inline h-2.5 w-2.5" /> Ack
                  </button>
                  <button class="ml-auto text-[10px] text-cyan-400 transition hover:text-cyan-300" @click="openJourney(inst)">journey</button>
                </div>
                <!-- v88: the annotate form - a fact lands on the memory -->
                <div v-if="annotateForId === inst.id" class="mt-2 flex flex-wrap items-center gap-2 rounded-xl border border-violet-500/30 bg-violet-500/5 px-2.5 py-2">
                  <input v-model="annKey" placeholder="key (budget, address...)" class="w-36 rounded-lg border border-zinc-800 bg-zinc-950 px-2 py-1 text-[10px] text-zinc-300 outline-none focus:border-violet-500/60" />
                  <input v-model="annValue" placeholder="value (json or text)" class="w-44 rounded-lg border border-zinc-800 bg-zinc-950 px-2 py-1 text-[10px] text-zinc-300 outline-none focus:border-violet-500/60" />
                  <input v-model="annNote" placeholder="note" class="w-36 rounded-lg border border-zinc-800 bg-zinc-950 px-2 py-1 text-[10px] text-zinc-300 outline-none focus:border-violet-500/60" />
                  <button class="rounded-lg bg-violet-500/90 px-2.5 py-1 text-[10px] font-bold text-zinc-950 transition hover:bg-violet-400 disabled:opacity-50" :disabled="annotating || !annKey.trim()" @click="annotate(inst)">
                    <Loader2 v-if="annotating" class="h-3 w-3 animate-spin" /> Remember
                  </button>
                  <p v-if="annotateError" class="w-full text-[10px] text-rose-300">{{ annotateError }}</p>
                </div>
                <!-- v88: the ack form - the handler takes it -->
                <div v-if="ackForId === inst.id" class="mt-2 flex flex-wrap items-center gap-2 rounded-xl border border-emerald-500/30 bg-emerald-500/5 px-2.5 py-2">
                  <input v-model="ackBy" placeholder="acknowledged by" class="w-36 rounded-lg border border-zinc-800 bg-zinc-950 px-2 py-1 text-[10px] text-zinc-300 outline-none focus:border-emerald-500/60" />
                  <input v-model="ackNote" placeholder="note (on it, calling now...)" class="w-48 rounded-lg border border-zinc-800 bg-zinc-950 px-2 py-1 text-[10px] text-zinc-300 outline-none focus:border-emerald-500/60" />
                  <button class="rounded-lg bg-emerald-500/90 px-2.5 py-1 text-[10px] font-bold text-zinc-950 transition hover:bg-emerald-400 disabled:opacity-50" :disabled="acking || !ackBy.trim()" @click="ackEscalation(inst)">
                    <Loader2 v-if="acking" class="h-3 w-3 animate-spin" /> Acknowledge
                  </button>
                  <p v-if="ackError" class="w-full text-[10px] text-rose-300">{{ ackError }}</p>
                </div>
                <p v-if="advanceForId === inst.id && advanceError" class="mt-1.5 text-[10px] text-rose-300">{{ advanceError }}</p>
              </div>
              <p v-if="!instances.length" class="rounded-xl border border-dashed border-zinc-800 py-8 text-center text-xs text-zinc-600">
                Nothing tracked{{ stateFilter ? ` in ${stateFilter}` : '' }} yet.
              </p>
            </div>
          </div>

          <!-- the journey -->
          <div v-if="detail" class="rounded-2xl border border-cyan-900/60 bg-zinc-900/50 p-5">
            <div class="flex items-center justify-between">
              <p class="flex items-center gap-1.5 text-[10px] font-bold uppercase tracking-widest text-cyan-300"><GitBranch class="h-3 w-3" /> The journey of {{ detail.ref }}</p>
              <button class="text-[10px] text-zinc-500 hover:text-zinc-300" @click="detail = null"><XCircle class="h-3.5 w-3.5" /></button>
            </div>
            <div class="mt-3 space-y-1.5">
              <div v-for="(j, i) in detail.journey || []" :key="i" class="flex items-start gap-2 rounded-xl border border-zinc-800 bg-zinc-950/60 px-3 py-2">
                <span class="mt-0.5 rounded-full px-1.5 py-0.5 text-[9px] font-bold" :class="journeyChipClass(j.transition)">{{ j.transition }}</span>
                <div class="min-w-0 flex-1">
                  <p class="text-[11px] text-zinc-300">
                    <span class="text-zinc-600">{{ j.from_state || 'start' }}</span> → <span class="font-semibold">{{ j.to_state }}</span>
                    <span class="ml-2 text-[9px] text-zinc-600">by {{ j.actor }}</span>
                  </p>
                  <p v-if="j.note" class="truncate text-[10px] text-zinc-500">{{ j.note }}</p>
                </div>
                <span class="text-[9px] text-zinc-600">{{ j.at?.slice(0, 19).replace('T', ' ') }}</span>
              </div>
            </div>
          </div>
        </section>
      </div>
    </main>
  </div>
</template>

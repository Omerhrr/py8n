<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import {
  Flame,
  GitBranch, Loader2, AlertTriangle, Plus, Clock, CheckCircle2, XCircle,
  Flag, RefreshCw, ChevronRight, ListChecks, BellRing, PenLine, CheckCheck,
  Siren, SlidersHorizontal, Radio, Eye, Mail, Send, Trash2, Pencil,
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
// v91: the attention view (everything past SLA across ALL machines) and
// the policy editor (the door's rhythm is editable from the board).

interface ProcessDef {
  id: string; name: string; description: string
  states: string[]; initial: string
  transitions: { name: string; from: string; to: string; description?: string }[]
  terminal_states: string[]
  escalation_policy: { channel: string; to: string; handlers?: string[]; mode?: string
    digest_every_seconds?: number; repeat_every_seconds: number
    max_repeats: number; message_template: string } | null
  escalation_summary: string
  journeys: { on_state: string; open: { process: string; state?: string | null; title_template?: string } }[]
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
  escalation_history?: EscalationHistory
}

// v94: the escalation history the sparkline draws - 14 day buckets the
// analytics derive from the transition log's own timestamps (the door's
// knocks/moves, the team's receipts, the summaries)
interface EscalationHistory {
  window_days: number
  days: { date: string; escalations: number; acknowledgements: number
    digests: number; total: number }[]
}

// v91: one row of the overdue-attention view - an open instance past its
// SLA on ANY machine, with the door's escalation book attached
interface AttentionRow {
  process_id: string; process_name: string
  instance_id: string; ref: string; title: string; state: string
  due_at: string | null; overdue_seconds: number
  age_in_state_seconds: number
  escalation: { count: number; last_delivery: string; last_detail: string
    acked_by: string; snooze_until: string
    reschedule_at?: string; reschedule_remaining_seconds?: number } | null
  escalation_summary: string
  journey_leg: boolean
}

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
const ackSnooze = ref('')  // v89: hours - the hold is a loan, then the door re-knocks
const ackReschedule = ref('')  // v98: minutes - the machine board's receipt carries the SAME clock the attention rows gained in v96 (parity)
const acking = ref(false)
const ackError = ref('')

// v91: the attention view - everything past SLA across ALL machines,
// most overdue first; loaded on mount and after every mutation
const attention = ref<AttentionRow[]>([])
const attentionMachines = ref(0)
const attentionLoading = ref(false)

// v93: the attention feed AUTO-REFRESHES on business.stuck - the board
// rides the same owner-scoped live tail every reactive surface rides
// (WS /events/stream); a breach anywhere re-reads the feed without
// anybody pressing refresh
const { api, streamUrl } = useApi()
const liveState = ref<'connecting' | 'live' | 'reconnecting'>('connecting')
const lastStuckNote = ref('')
let liveWs: WebSocket | null = null
let liveReconnectDelay = 2000
let liveReconnectTimer: ReturnType<typeof setTimeout> | null = null
let attentionRefreshTimer: ReturnType<typeof setTimeout> | null = null

function scheduleAttentionRefresh() {
  if (attentionRefreshTimer) clearTimeout(attentionRefreshTimer)
  attentionRefreshTimer = setTimeout(async () => {
    attentionRefreshTimer = null
    await loadAttention()
    // the open machine's own board moves too - quietly, forms untouched
    if (selected.value) {
      try {
        const [p, inst, a] = await Promise.all([
          api.get<ProcessDef>(`/processes/${selected.value.id}`),
          api.get<{ instances: Instance[] }>(
            `/processes/${selected.value.id}/instances${stateFilter.value ? `?state=${stateFilter.value}` : ''}`),
          api.get<Analytics>(`/processes/${selected.value.id}/analytics`),
        ])
        selected.value = p
        instances.value = inst.instances
        analytics.value = a
      } catch { /* the next event or a manual refresh catches it */ }
    }
  }, 600)
}

function connectLiveTail() {
  if (liveWs) return
  try {
    liveWs = new WebSocket(streamUrl('/api/v1/events/stream'))
  } catch {
    liveState.value = 'reconnecting'
    scheduleLiveReconnect()
    return
  }
  liveState.value = 'connecting'
  liveWs.onopen = () => { liveState.value = 'live'; liveReconnectDelay = 2000 }
  liveWs.onmessage = (m) => {
    try {
      const msg = JSON.parse(m.data as string)
      if (msg?.event !== 'system_event' || msg?.type !== 'business.stuck') return
      const p = msg.payload || {}
      lastStuckNote.value =
        `refreshed by business.stuck - ${p.ref || 'an entity'} on ${p.process_name || 'a machine'}` +
        ` (+${fmtAge(Number(p.overdue_seconds) || 0)} past SLA)`
      scheduleAttentionRefresh()
    } catch { /* a malformed frame is not worth the board's attention */ }
  }
  liveWs.onclose = () => { liveWs = null; liveState.value = 'reconnecting'; scheduleLiveReconnect() }
  liveWs.onerror = () => { try { liveWs?.close() } catch { /* onclose follows */ } }
}

function scheduleLiveReconnect() {
  if (liveReconnectTimer) return
  liveReconnectTimer = setTimeout(() => {
    liveReconnectTimer = null
    liveReconnectDelay = Math.min(liveReconnectDelay * 2, 15000)
    connectLiveTail()
  }, liveReconnectDelay)
}

onUnmounted(() => {
  if (attentionRefreshTimer) clearTimeout(attentionRefreshTimer)
  if (liveReconnectTimer) clearTimeout(liveReconnectTimer)
  if (liveWs) { try { liveWs.onclose = null; liveWs.close() } catch { /* gone */ } }
  liveWs = null
})

// v92: ack/snooze STRAIGHT from the attention row - the row already
// carries process_id + instance_id, so the receipt needs no detour
// through the machine view
const attAckForId = ref('')
const attAckBy = ref('')
const attAckNote = ref('')
const attAckSnooze = ref('')
const attAckReschedule = ref('')  // v96: minutes - the door's next knock at an explicit moment
const attAcking = ref(false)
const attAckError = ref('')

function openAttAck(row: AttentionRow) {
  attAckForId.value = attAckForId.value === row.instance_id ? '' : row.instance_id
  attAckBy.value = ''
  attAckNote.value = ''
  attAckSnooze.value = ''
  attAckReschedule.value = ''
  attAckError.value = ''
}

async function ackFromAttention(row: AttentionRow) {
  if (!attAckBy.value.trim()) return
  attAcking.value = true
  attAckError.value = ''
  try {
    await api.post(
      `/processes/${row.process_id}/instances/${row.instance_id}/escalations/ack`,
      { by: attAckBy.value.trim(), note: attAckNote.value.trim(),
        snooze_hours: attAckSnooze.value.trim() ? Number(attAckSnooze.value) : null,
        reschedule_in_minutes: attAckReschedule.value.trim() ? Number(attAckReschedule.value) : null })
    attAckForId.value = ''
    await loadAttention()  // the row stays on the feed wearing the ack chip
    if (selected.value?.id === row.process_id) await refreshAll()
  } catch (e: any) {
    attAckError.value = e?.data?.detail || e?.message || 'The acknowledgement was refused'
  } finally {
    attAcking.value = false
  }
}

async function loadAttention() {
  attentionLoading.value = true
  try {
    const res = await api.get<{ attention: AttentionRow[]; count: number; machines: number }>(
      '/processes/attention')
    attention.value = res.attention
    attentionMachines.value = res.machines
  } catch {
    // the panel stays honest - an unreachable feed is an empty one here,
    // the page error line is reserved for the machines themselves
  } finally {
    attentionLoading.value = false
  }
}

// jump from an attention row to its owning machine (and its tracked list)
async function openAttentionMachine(row: AttentionRow) {
  const p = processes.value.find(x => x.id === row.process_id)
  if (p) await openProcess(p)
}

// v91: the policy editor - the door reads the policy fresh at every
// sweep, so what is saved here rules the NEXT tick
const policyOpen = ref(false)
const polChannel = ref('')
const polTo = ref('')
const polMode = ref<'knock' | 'digest'>('knock')
const polCadence = ref('')   // seconds - repeat_every_seconds or digest_every_seconds by mode
const polMaxRepeats = ref('')
const polTemplate = ref('')
const policySaving = ref(false)
const policyError = ref('')
const policySavedNote = ref('')

// v92: the before/after diff - every save is reviewed BEFORE it lands,
// and the server's receipt (policy_diff.changed) confirms what moved
interface PolicyDiffRow { key: string; kind: 'added' | 'removed' | 'changed'
  before: string; after: string }
const policyReview = ref<{ rows: PolicyDiffRow[]; after: Record<string, any> | null } | null>(null)

const POLICY_LABELS: Record<string, string> = {
  channel: 'channel', to: 'deliver to', handlers: 'handler rotation',
  mode: 'rhythm', repeat_every_seconds: 'knock cadence',
  digest_every_seconds: 'digest window', max_repeats: 'cap',
  message_template: 'template',
}

// the client lens on the CANONICAL policy shape the server validates to
// (escalations.validate_escalation_policy): defaults filled, the cadence
// key the mode does not use pinned to its default - so a diff row only
// appears when the value truly moves
function normPolicy(p: any): Record<string, any> {
  if (!p) return {}
  const mode = p.mode || 'knock'
  const out: Record<string, any> = {
    channel: p.channel || '',
    to: p.to || '',
    handlers: Array.isArray(p.handlers) ? p.handlers : [],
    mode,
    max_repeats: p.max_repeats ?? 3,
    message_template: p.message_template || '',
  }
  if (mode === 'digest') {
    out.digest_every_seconds = p.digest_every_seconds ?? 86400
    out.repeat_every_seconds = 3600
  } else {
    out.repeat_every_seconds = p.repeat_every_seconds ?? 3600
    out.digest_every_seconds = 86400
  }
  return out
}

function fmtPolicyVal(key: string, v: any): string {
  if (key === 'handlers') return Array.isArray(v) && v.length ? v.join(' -> ') : '(none)'
  if (key === 'message_template') return v ? String(v).slice(0, 60) : '(default)'
  if (key === 'to' || key === 'channel') return v === '' ? '(event-only)' : String(v)
  if (typeof v === 'number' && key.endsWith('_seconds')) return fmtAge(v)
  return String(v)
}

function diffPolicyRows(before: any, after: Record<string, any> | null): PolicyDiffRow[] {
  const b = normPolicy(before)
  const a = after || {}
  const rows: PolicyDiffRow[] = []
  for (const key of Object.keys({ ...b, ...a })) {
    const had = key in b
    const has = !!after && key in a
    if (had && !has) rows.push({ key, kind: 'removed', before: fmtPolicyVal(key, b[key]), after: '' })
    else if (!had && has) rows.push({ key, kind: 'added', before: '', after: fmtPolicyVal(key, a[key]) })
    else if (JSON.stringify(b[key]) !== JSON.stringify(a[key]))
      rows.push({ key, kind: 'changed', before: fmtPolicyVal(key, b[key]), after: fmtPolicyVal(key, a[key]) })
  }
  return rows
}

// the policy the form builds - the exact object a confirmed save PATCHes
function builtPolicy(): Record<string, any> {
  const cadence = polCadence.value.trim() ? Number(polCadence.value) : undefined
  const policy: Record<string, any> = {
    channel: polChannel.value,
    to: polTo.value.trim(),
    mode: polMode.value,
    max_repeats: polMaxRepeats.value.trim() ? Number(polMaxRepeats.value) : undefined,
  }
  if (polMode.value === 'digest') policy.digest_every_seconds = cadence
  else policy.repeat_every_seconds = cadence
  if (polTemplate.value.trim()) policy.message_template = polTemplate.value.trim()
  return policy
}

async function openPolicyEditor() {
  const pol = selected.value?.escalation_policy
  polChannel.value = pol?.channel || ''
  polTo.value = pol?.to || ''
  polMode.value = (pol?.mode as 'knock' | 'digest') || 'knock'
  polCadence.value = pol
    ? String(pol.mode === 'digest'
        ? (pol.digest_every_seconds ?? 86400)
        : (pol.repeat_every_seconds ?? 3600))
    : ''
  polMaxRepeats.value = pol ? String(pol.max_repeats ?? 3) : ''
  polTemplate.value = pol?.message_template || ''
  policyError.value = ''
  policyReview.value = null
  preview.value = null
  previewError.value = ''
  policyOpen.value = true
  await loadPreview()  // v93: the preview typesets itself on open
}

// v92: Save first SHOWS the diff - the confirm button is the one that PATCHes
function requestPolicySave() {
  if (!selected.value) return
  policyError.value = ''
  const rows = diffPolicyRows(selected.value.escalation_policy, builtPolicy())
  if (!rows.length) {
    policyReview.value = null
    policyError.value = 'nothing changed - the form matches the policy on the machine'
    return
  }
  policyReview.value = { rows, after: builtPolicy() }
}

// removing is a save too - the diff shows every key the machine gives back
function requestPolicyRemove() {
  if (!selected.value) return
  policyError.value = ''
  const rows = diffPolicyRows(selected.value.escalation_policy, null)
  policyReview.value = { rows, after: null }
}

// v93: the SLA digest preview - the door's next move rendered over the
// machine's LIVE overdue items under the DRAFT the form is holding. It
// rides the same validator a save runs, so a broken draft refuses HERE,
// before it can be saved; every form change re-typesets it (debounced).
const preview = ref<any | null>(null)
const previewLoading = ref(false)
const previewError = ref('')
let previewTimer: ReturnType<typeof setTimeout> | null = null

const HELD_LABELS: Record<string, string> = {
  acknowledged: 'acknowledged - the receipt owns this stint',
  too_soon: 'too soon - the cadence holds the next knock',
  episode_complete: 'episode complete - the cap was reached',
  already_escalated_this_stint: 'already escalated this stint',
}

function schedulePreview() {
  if (previewTimer) clearTimeout(previewTimer)
  previewTimer = setTimeout(() => { previewTimer = null; loadPreview() }, 400)
}

async function loadPreview() {
  if (!selected.value || !policyOpen.value) return
  previewLoading.value = true
  try {
    preview.value = await api.post<any>(
      `/processes/${selected.value.id}/escalation-preview`,
      { policy: builtPolicy() })
    previewError.value = ''
  } catch (e: any) {
    preview.value = null
    previewError.value = e?.data?.detail || e?.message || 'the preview was refused'
  } finally {
    previewLoading.value = false
  }
}

watch([polChannel, polTo, polMode, polCadence, polMaxRepeats, polTemplate],
  () => { if (policyOpen.value) schedulePreview() })

async function confirmPolicyReview() {
  if (!selected.value || !policyReview.value) return
  policySaving.value = true
  policyError.value = ''
  try {
    const res = await api.patch<any>(
      `/processes/${selected.value.id}/escalation-policy`,
      { policy: policyReview.value.after, actor: 'staff' })
    const moved = res?.policy_diff?.changed || []
    policySavedNote.value = policyReview.value.after
      ? `saved - ${moved.length} key${moved.length === 1 ? '' : 's'} changed: ${moved.join(', ')}`
      : 'policy removed - the machine falls back to one knock per stint, event-only'
    policyReview.value = null
    policyOpen.value = false
    await refreshAll()
    await loadAttention()
  } catch (e: any) {
    policyError.value = e?.data?.detail || e?.message || 'The policy was refused'
  } finally {
    policySaving.value = false
  }
}

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
// v89: digest sends stamp last_delivery='digest' - the summary receipt
function escBook(inst: Instance): { count: number; last_delivery: string; acked: { by: string; at: string; note?: string; snooze_until?: string; reschedule_at?: string } | null } | null {
  const b = inst.context?.escalations
  if (!b || typeof b !== 'object') return null
  return {
    count: b.count || 0,
    last_delivery: b.last_delivery || '',
    acked: b.acked && typeof b.acked === 'object' ? b.acked : null,
  }
}

// v98: seconds until an ISO loan stamp elapses (0 = spent) - the raw book
// carries the stamps, the attention feed's remaining-seconds come from the
// server projection; the machine board reads the same book the board owns
function loanRemaining(iso?: string): number {
  if (!iso) return 0
  const t = new Date(iso).getTime()
  return Number.isFinite(t) ? Math.max(0, Math.round((t - Date.now()) / 1000)) : 0
}

// v88: journey rows carry paperwork too - color it by what it means
function journeyChipClass(transition: string): string {
  if (transition === 'annotate') return 'bg-violet-500/15 text-violet-300'
  if (transition === 'escalation_acknowledged') return 'bg-emerald-500/15 text-emerald-300'
  if (transition === 'escalated' || transition === 'escalate') return 'bg-amber-500/15 text-amber-300'
  return 'bg-cyan-500/10 text-cyan-300'
}

// ---- v94: the escalation-history sparkline per machine -------------------
// one row of stacked bars per day: the door's knocks (amber), the team's
// receipts (emerald), the summaries (violet) - all derived from the log,
// the window named so a quiet stretch reads as data, not absence
const histDays = computed(() => analytics.value?.escalation_history?.days || [])
const histMaxV = computed(() => Math.max(1, ...histDays.value.map(d => d.total)))
const histQuiet = computed(() => histDays.value.every(d => d.total === 0))

function histX(i: number): number {
  return 24 + i * 19.6
}

interface HistSeg { y: number; h: number; fill: string }

function histSegs(d: { escalations: number; acknowledgements: number; digests: number }): HistSeg[] {
  const segs: HistSeg[] = []
  let base = 46
  for (const [v, fill] of [
    [d.escalations, '#fbbf24'],
    [d.acknowledgements, '#34d399'],
    [d.digests, '#a78bfa'],
  ] as const) {
    if (!v) continue
    const h = Math.max(2, Math.round((v / histMaxV.value) * 38))
    base -= h
    segs.push({ y: base, h, fill })
  }
  return segs
}

function histTitle(d: { date: string; escalations: number; acknowledgements: number; digests: number }): string {
  return `${d.date} - ${d.escalations} escalation(s) · ${d.acknowledgements} ack(s) · ${d.digests} digest(s)`
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
  await loadAttention()  // v91: any mutation re-reads the overdue view
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
// v89: the ack may carry a snooze - the hold becomes a loan (N hours,
// then the door re-knocks)
function openAck(inst: Instance) {
  ackForId.value = inst.id
  ackBy.value = ''
  ackNote.value = ''
  ackSnooze.value = ''
  ackReschedule.value = ''  // v98: the machine board's form opens with the same blank clocks the attention row's does
  ackError.value = ''
}

async function ackEscalation(inst: Instance) {
  if (!selected.value || !ackBy.value.trim()) return
  acking.value = true
  ackError.value = ''
  try {
    await api.post(
      `/processes/${selected.value.id}/instances/${inst.id}/escalations/ack`,
      { by: ackBy.value.trim(), note: ackNote.value.trim(),
        snooze_hours: ackSnooze.value.trim() ? Number(ackSnooze.value) : null,
        reschedule_in_minutes: ackReschedule.value.trim() ? Number(ackReschedule.value) : null })  // v98: the same one-clock-per-receipt the attention rows post - the door refuses both clocks together
    ackForId.value = ''
    ackBy.value = ''
    ackNote.value = ''
    ackSnooze.value = ''
    ackReschedule.value = ''
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

// v95: the CROSS-MACHINE escalation heatmap - per machine, per day, the
// door's pressure across the whole estate (v94's sparkline is per-machine
// inside the selected board; this is the machines-x-days grid beside it).
interface HeatCell { escalations: number; acks: number; digests: number }
interface HeatMachine {
  process_id: string; name: string
  cells: Record<string, HeatCell>
  totals: { escalations: number; acks: number; digests: number }
}
const heatData = ref<{ days: string[]; machines: HeatMachine[] } | null>(null)
const heatLoading = ref(false)
const heatError = ref('')

async function loadHeat() {
  heatLoading.value = true
  heatError.value = ''
  try {
    heatData.value = await api.get<any>('/processes/escalation-history?days=14')
  } catch (e: any) {
    heatError.value = e?.data?.detail || e?.message || 'Could not load the escalation history'
  } finally {
    heatLoading.value = false
  }
}

function heatDay(iso: string): string {
  try { return new Date(iso + 'T00:00:00').toLocaleDateString([], { weekday: 'narrow' }) }
  catch { return '' }
}
function heatDayNum(iso: string): string {
  try { return String(new Date(iso + 'T00:00:00').getDate()) }
  catch { return iso.slice(8) }
}
function heatCellClass(cell: HeatCell): string {
  const e = cell.escalations
  if (!e && !cell.acks && !cell.digests) return 'bg-zinc-900/60'
  if (e >= 5) return 'bg-rose-500/60 text-rose-100'
  if (e >= 3) return 'bg-orange-500/55 text-orange-100'
  if (e >= 2) return 'bg-amber-500/45 text-amber-100'
  if (e >= 1) return 'bg-amber-500/25 text-amber-200'
  return 'bg-emerald-500/20 text-emerald-200' // acks/digests only
}
function heatCellTitle(name: string, iso: string, cell: HeatCell): string {
  return `${name} · ${iso} - ${cell.escalations} escalation(s) · ${cell.acks} ack(s) · ${cell.digests} digest item(s)`
}

// v96: the drill-down - ONE heatmap cell opened: the machine's DAY,
// row by row off the transition log (knocks, acks, digest receipts)
interface DayRow {
  at: string | null; transition: string; kind: string
  instance_id: string; ref: string; title: string; state: string
  actor: string; note: string; attempt: number | null
  overdue_seconds: number | null; snooze_until: string | null
  reschedule_at: string | null
}
interface DayDetail {
  process_id: string; name: string; day: string; rows: DayRow[]
  counts: { escalations: number; acks: number; digests: number }; total: number
}
const drillKey = ref('')  // `${process_id}|${day}` of the open cell ('' = closed)
const drill = ref<DayDetail | null>(null)
const drillLoading = ref(false)
const drillError = ref('')

async function openDrill(m: HeatMachine, day: string) {
  const key = `${m.process_id}|${day}`
  if (drillKey.value === key) { drillKey.value = ''; drill.value = null; return }
  drillKey.value = key
  drillLoading.value = true
  drillError.value = ''
  drill.value = null
  try {
    drill.value = await api.get<DayDetail>(
      `/processes/escalation-history/${m.process_id}/${day}`)
  } catch (e: any) {
    drillError.value = e?.data?.detail || e?.message || 'Could not open the machine\'s day'
  } finally {
    drillLoading.value = false
  }
}

function drillKindClass(kind: string): string {
  if (kind === 'ack') return 'bg-emerald-500/15 text-emerald-300'
  if (kind === 'digest') return 'bg-violet-500/15 text-violet-300'
  return 'bg-amber-500/15 text-amber-300'
}

function drillAt(iso: string | null): string {
  if (!iso) return ''
  try { return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) }
  catch { return iso }
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
  await loadAttention()  // v91: the overdue view opens with the page
  loadHeat()             // v95: the cross-machine heatmap opens with it
  loadChainReport()      // v99: the file-on-a-cadence board reads its schedule
  connectLiveTail()      // v93: and it refreshes ITSELF from here on
})

// ---- v99: the chain report - the digest pattern applied to the FILE -------
// one schedule per owner: every window the SAME per-leg chain history the
// plots export rides the owner's bound email endpoint as a real MIME
// attachment - the subject carries the scan line, the outcome (delivered /
// skipped / failed) stamps the schedule row honestly, and an empty window
// consumes itself without emailing an empty spreadsheet.
const chainReport = ref<any>(null)
// v100: the report carries a RECIPIENT LIST (comma/semicolon-separated - one
// envelope, every name) and an optional SYSTEM scope (the file covers only
// the machines that system binds, the same filter the systems page's
// per-system export sends)
// v101: the scope is a CHAIN TAG LIST (comma-separated - the file covers
// every chain the list names) and the rhythm speaks its named beats
// (hourly / daily / the weekly digest - the same envelope, the report's own
// list, the file on its weekly beat - or custom minutes)
const CR_PRESETS = [
  { key: 'hourly', label: 'hourly', seconds: 3600 },
  { key: 'daily', label: 'daily', seconds: 86400 },
  { key: 'weekly', label: 'weekly digest', seconds: 604800 },
  { key: 'custom', label: 'custom', seconds: 0 },
]
const crForm = ref({ enabled: true, cadence: 'daily', cadence_minutes: 1440, to: '', history_limit: 50, chains: '', system: '' })
const crEditing = ref(false)
const crBusy = ref(false)
const crSending = ref(false)
const crError = ref('')

function crCadence(sec: number): string {
  if (sec >= 86400) return `${Math.round(sec / 86400)}d`
  if (sec >= 3600) return `${Math.round(sec / 3600)}h`
  return `${Math.round(sec / 60)}m`
}

function crPresetFor(sec: number): string {
  return CR_PRESETS.find(p => p.seconds && p.seconds === sec)?.key || 'custom'
}

async function loadChainReport() {
  try {
    const out = await api.get<any>('/processes/chain-report')
    chainReport.value = out.schedule || null
    if (chainReport.value) {
      crForm.value = {
        enabled: !!chainReport.value.enabled,
        cadence: chainReport.value.cadence || crPresetFor(chainReport.value.cadence_seconds || 86400),
        cadence_minutes: Math.max(5, Math.round((chainReport.value.cadence_seconds || 86400) / 60)),
        to: chainReport.value.to || '',
        history_limit: chainReport.value.history_limit || 50,
        chains: (chainReport.value.chains || []).join(', ') || chainReport.value.chain || '',
        system: chainReport.value.system || '',
      }
    }
  } catch { /* the card reads its absence honestly */ }
}

async function saveChainReport() {
  crBusy.value = true
  crError.value = ''
  try {
    const preset = CR_PRESETS.find(p => p.key === crForm.value.cadence)
    const named = preset && preset.seconds ? { cadence: preset.key, cadence_seconds: preset.seconds } : { cadence: '', cadence_seconds: Math.max(300, Math.round(crForm.value.cadence_minutes * 60)) }
    const out = await api.put<any>('/processes/chain-report', {
      enabled: crForm.value.enabled,
      ...named,
      to: crForm.value.to.trim(),
      history_limit: crForm.value.history_limit,
      chains: crForm.value.chains.trim(),
      system: crForm.value.system.trim(),
    })
    chainReport.value = out.schedule
    crEditing.value = false
  } catch (e: any) {
    crError.value = e?.data?.detail || e?.message || 'the schedule was refused'
  } finally { crBusy.value = false }
}

async function sendChainReportNow() {
  crSending.value = true
  crError.value = ''
  try {
    const out = await api.post<any>('/processes/chain-report/send-now')
    chainReport.value = out.schedule
  } catch (e: any) {
    crError.value = e?.data?.detail || e?.message || 'the dispatch was refused'
  } finally { crSending.value = false }
}

async function removeChainReport() {
  crBusy.value = true
  crError.value = ''
  try {
    await api.del('/processes/chain-report')
    chainReport.value = null
    crEditing.value = false
  } catch (e: any) {
    crError.value = e?.data?.detail || e?.message || 'the removal was refused'
  } finally { crBusy.value = false }
}
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

      <!-- v91: the overdue-attention view - everything past SLA across ALL machines -->
      <div class="mb-5 rounded-2xl border px-5 py-4"
        :class="attention.length ? 'border-rose-500/40 bg-rose-500/5' : 'border-emerald-500/30 bg-emerald-500/5'">
        <div class="flex flex-wrap items-center gap-2">
          <Siren class="h-4 w-4" :class="attention.length ? 'text-rose-300' : 'text-emerald-300'" />
          <p class="text-xs font-bold uppercase tracking-widest" :class="attention.length ? 'text-rose-300' : 'text-emerald-300'">
            Needs attention
          </p>
          <span v-if="attention.length" class="rounded-full bg-rose-500/15 px-2 py-0.5 text-[10px] font-bold text-rose-300">
            {{ attention.length }} past SLA across {{ attentionMachines }} machine{{ attentionMachines === 1 ? '' : 's' }}
          </span>
          <span v-else class="text-[11px] text-emerald-300/80">all clear - nothing is past its SLA</span>
          <!-- v93: the feed refreshes ITSELF - business.stuck rides the live tail -->
          <span class="flex items-center gap-1 rounded-full px-2 py-0.5 text-[9px] font-bold"
            :class="liveState === 'live' ? 'bg-emerald-500/10 text-emerald-300' : 'bg-amber-500/10 text-amber-300'"
            :title="liveState === 'live' ? 'the feed re-reads itself when the door emits business.stuck' : 'the live tail is down - the Refresh button still works'">
            <Radio class="h-2.5 w-2.5" :class="liveState === 'live' ? 'animate-pulse' : ''" />
            {{ liveState === 'live' ? 'live - refreshes on business.stuck' : liveState === 'reconnecting' ? 'reconnecting...' : 'connecting...' }}
          </span>
          <span v-if="lastStuckNote" class="w-full text-[10px] text-sky-300/80">{{ lastStuckNote }}</span>
          <Loader2 v-if="attentionLoading" class="h-3 w-3 animate-spin text-zinc-600" />
        </div>
        <div v-if="attention.length" class="mt-3 space-y-1.5">
          <div v-for="row in attention.slice(0, 8)" :key="row.instance_id"
            class="rounded-xl border border-rose-500/20 bg-zinc-950/60 px-3 py-2">
            <div class="flex flex-wrap items-center gap-2">
              <span class="rounded-lg bg-rose-500/15 px-2 py-0.5 text-[10px] font-bold text-rose-300">{{ row.state }}</span>
              <span class="text-xs font-semibold text-zinc-200">{{ row.ref }}</span>
              <span class="truncate text-[10px] text-zinc-500">{{ row.title }}</span>
              <span class="text-[10px] text-zinc-500">on <span class="text-zinc-400">{{ row.process_name }}</span></span>
              <span class="rounded-full bg-rose-500/10 px-1.5 py-0.5 text-[9px] font-bold text-rose-300" title="time past the SLA promise">
                {{ fmtAge(row.overdue_seconds) }} overdue
              </span>
              <span v-if="row.escalation?.acked_by" class="rounded-full bg-emerald-500/15 px-1.5 py-0.5 text-[9px] font-bold text-emerald-300" :title="`acknowledged by ${row.escalation.acked_by}`"><CheckCheck class="mr-0.5 inline h-2.5 w-2.5" /> ack</span>
              <span v-if="row.escalation?.snooze_until" class="rounded-full bg-sky-500/15 px-1.5 py-0.5 text-[9px] font-bold text-sky-300" :title="`the door re-knocks after ${row.escalation.snooze_until}`">snoozed</span>
              <span v-if="row.escalation?.reschedule_at && (row.escalation.reschedule_remaining_seconds || 0) > 0" class="rounded-full bg-fuchsia-500/15 px-1.5 py-0.5 text-[9px] font-bold text-fuchsia-300" :title="`the door re-knocks at ${row.escalation.reschedule_at}`"><Clock class="mr-0.5 inline h-2.5 w-2.5" /> rescheduled · {{ fmtAge(row.escalation.reschedule_remaining_seconds || 0) }}</span>
              <span v-if="row.escalation && !row.escalation.acked_by && row.escalation.last_delivery === 'digest'" class="rounded-full bg-fuchsia-500/15 px-1.5 py-0.5 text-[9px] font-bold text-fuchsia-300" title="listed in the escalation digest">digest ×{{ row.escalation.count }}</span>
              <span v-else-if="row.escalation && !row.escalation.acked_by" class="rounded-full bg-indigo-500/15 px-1.5 py-0.5 text-[9px] font-bold text-indigo-300" :title="row.escalation.last_detail || row.escalation.last_delivery">escalated ×{{ row.escalation.count }}</span>
              <span v-if="row.journey_leg" class="rounded-full bg-fuchsia-500/10 px-1.5 py-0.5 text-[9px] font-semibold text-fuchsia-300" title="this entity opened itself from another department's hand-off">journey leg</span>
              <!-- v92: the receipt straight from the row - no detour through the machine -->
              <button v-if="!row.escalation?.acked_by"
                class="flex items-center gap-1 rounded-lg border border-emerald-500/40 bg-emerald-500/10 px-2 py-0.5 text-[10px] font-bold text-emerald-300 transition hover:bg-emerald-500/20"
                @click="openAttAck(row)">
                <CheckCheck class="h-2.5 w-2.5" /> Ack
              </button>
              <button class="ml-auto flex items-center gap-1 text-[10px] font-bold text-cyan-400 transition hover:text-cyan-300" @click="openAttentionMachine(row)">
                open machine <ChevronRight class="h-3 w-3" />
              </button>
            </div>
            <!-- v92: the row's own ack form (by + note + snooze, same receipt as the machine view) -->
            <div v-if="attAckForId === row.instance_id" class="mt-2 flex flex-wrap items-center gap-2 rounded-xl border border-emerald-500/30 bg-emerald-500/5 px-2.5 py-2">
              <input v-model="attAckBy" placeholder="acknowledged by" class="w-36 rounded-lg border border-zinc-800 bg-zinc-950 px-2 py-1 text-[10px] text-zinc-300 outline-none focus:border-emerald-500/60" />
              <input v-model="attAckNote" placeholder="note (on it, calling now...)" class="w-44 rounded-lg border border-zinc-800 bg-zinc-950 px-2 py-1 text-[10px] text-zinc-300 outline-none focus:border-emerald-500/60" />
              <input v-model="attAckSnooze" type="number" min="0" step="0.5" placeholder="snooze hrs" class="w-24 rounded-lg border border-zinc-800 bg-zinc-950 px-2 py-1 text-[10px] text-zinc-300 outline-none focus:border-emerald-500/60" title="hold the door quiet for N hours, then it re-knocks (empty = owns the rest of the stint)" />
              <input v-model="attAckReschedule" type="number" min="0" step="5" placeholder="reschedule in min" class="w-32 rounded-lg border border-fuchsia-500/30 bg-zinc-950 px-2 py-1 text-[10px] text-zinc-300 outline-none focus:border-fuchsia-500/60" title="v96: re-knock the door at an EXPLICIT moment - now + N minutes (the human picks the time; leave empty if you set snooze hrs - one clock per receipt)" />
              <button class="rounded-lg bg-emerald-500/90 px-2.5 py-1 text-[10px] font-bold text-zinc-950 transition hover:bg-emerald-400 disabled:opacity-50" :disabled="attAcking || !attAckBy.trim()" @click="ackFromAttention(row)">
                <Loader2 v-if="attAcking" class="h-3 w-3 animate-spin" /> Acknowledge
              </button>
              <p v-if="attAckError" class="w-full text-[10px] text-rose-300">{{ attAckError }}</p>
            </div>
          </div>
          <p v-if="attention.length > 8" class="text-[10px] text-zinc-600">+ {{ attention.length - 8 }} more past SLA (the feed caps at 200, most overdue first)</p>
        </div>
      </div>

      <!-- v95: the cross-machine escalation heatmap - the door's pressure across the estate -->
      <div v-if="!loading" class="mb-5 rounded-2xl border border-zinc-800/80 bg-zinc-900/50 p-4">
        <div class="mb-3 flex flex-wrap items-center justify-between gap-2">
          <div class="flex items-center gap-2">
            <Flame class="h-3.5 w-3.5 text-amber-400" />
            <h2 class="text-xs font-bold uppercase tracking-wide text-zinc-300">Escalation heatmap</h2>
            <span class="text-[10px] text-zinc-500">the door's pressure across machines, last 14 days - hover a cell for the counts, click one to drill into the machine's day</span>
          </div>
          <div class="flex items-center gap-2">
            <span class="flex items-center gap-1 text-[9px] text-zinc-600">
              quiet
              <span class="inline-block h-2.5 w-2.5 rounded-sm bg-zinc-900 ring-1 ring-zinc-800" />
              <span class="inline-block h-2.5 w-2.5 rounded-sm bg-amber-500/25" />
              <span class="inline-block h-2.5 w-2.5 rounded-sm bg-amber-500/45" />
              <span class="inline-block h-2.5 w-2.5 rounded-sm bg-orange-500/55" />
              <span class="inline-block h-2.5 w-2.5 rounded-sm bg-rose-500/60" />
              hot
            </span>
            <button class="flex items-center gap-1 rounded-lg border border-zinc-700 px-2 py-1 text-[10px] font-bold text-zinc-300 transition hover:border-zinc-500 disabled:opacity-50"
              :disabled="heatLoading" @click="loadHeat">
              <Loader2 v-if="heatLoading" class="h-3 w-3 animate-spin" />
              <RefreshCw v-else class="h-3 w-3" /> refresh
            </button>
          </div>
        </div>
        <div v-if="heatError" class="rounded-xl border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-[10px] text-rose-300">{{ heatError }}</div>
        <div v-else-if="heatData && !heatData.machines.length"
          class="rounded-xl border border-dashed border-zinc-800 px-3 py-5 text-center text-[11px] text-zinc-600">
          No escalation history yet - the door's knocks, digests and the team's acks will draw the grid here.
        </div>
        <div v-else-if="heatData" class="overflow-x-auto">
          <table class="min-w-[720px] border-separate border-spacing-y-1">
            <thead>
              <tr>
                <th class="w-48 px-2 pb-1 text-left text-[9px] font-bold uppercase tracking-widest text-zinc-600">machine</th>
                <th v-for="d in heatData.days" :key="d" class="px-0.5 pb-1 text-center text-[8px] font-bold text-zinc-600">
                  <div>{{ heatDay(d) }}</div>
                  <div class="text-zinc-500">{{ heatDayNum(d) }}</div>
                </th>
                <th class="w-28 px-2 pb-1 text-right text-[9px] font-bold uppercase tracking-widest text-zinc-600">14d total</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="m in heatData.machines" :key="m.process_id">
                <td class="max-w-48 truncate px-2 py-1 text-[11px] font-bold text-zinc-300" :title="m.name">{{ m.name }}</td>
                <td v-for="d in heatData.days" :key="d" class="px-0.5 py-0.5 text-center">
                  <div class="mx-auto flex h-6 w-7 cursor-pointer items-center justify-center rounded text-[9px] font-bold transition hover:scale-110"
                    :class="[heatCellClass(m.cells[d]), drillKey === `${m.process_id}|${d}` ? 'ring-2 ring-cyan-400/80' : '']"
                    :title="heatCellTitle(m.name, d, m.cells[d])"
                    @click="openDrill(m, d)">
                    <span v-if="m.cells[d].escalations || m.cells[d].acks || m.cells[d].digests">
                      {{ m.cells[d].escalations + m.cells[d].acks + m.cells[d].digests }}
                    </span>
                  </div>
                </td>
                <td class="whitespace-nowrap px-2 py-1 text-right text-[9.5px] font-bold">
                  <span class="text-amber-300">{{ m.totals.escalations }} esc</span>
                  <span class="text-zinc-600"> · </span>
                  <span class="text-emerald-300">{{ m.totals.acks }} ack</span>
                  <span class="text-zinc-600"> · </span>
                  <span class="text-sky-300">{{ m.totals.digests }} dig</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <!-- v96: the drill-down - the open cell's machine-day, row by row -->
        <div v-if="drillKey" class="mt-3 rounded-xl border border-cyan-500/30 bg-cyan-500/5 px-3 py-2.5">
          <div class="flex flex-wrap items-center gap-2">
            <p class="text-[10px] font-bold uppercase tracking-widest text-cyan-300">
              {{ drill?.name || 'machine' }} · {{ drillKey.split('|')[1] }} - the day, row by row
            </p>
            <span v-if="drill" class="text-[9.5px] text-zinc-500">{{ drill.total }} row{{ drill.total === 1 ? '' : 's' }}: {{ drill.counts.escalations }} esc · {{ drill.counts.acks }} ack · {{ drill.counts.digests }} dig</span>
            <button class="ml-auto rounded-lg border border-zinc-700 px-2 py-0.5 text-[10px] font-bold text-zinc-400 transition hover:text-zinc-200" @click="drillKey = ''; drill = null">close</button>
          </div>
          <Loader2 v-if="drillLoading" class="mt-2 h-3 w-3 animate-spin text-zinc-600" />
          <p v-else-if="drillError" class="mt-1 text-[10px] text-rose-300">{{ drillError }}</p>
          <template v-else-if="drill">
            <p v-if="!drill.rows.length" class="mt-1.5 text-[10px] text-zinc-600">The door was quiet on this machine that day - nothing to drill into.</p>
            <div v-else class="mt-1.5 space-y-1">
              <div v-for="(r, i) in drill.rows" :key="`${r.instance_id}-${r.transition}-${i}`"
                class="flex flex-wrap items-center gap-x-2 gap-y-0.5 rounded-lg border border-zinc-800/70 bg-zinc-950/50 px-2 py-1 text-[9.5px]">
                <span class="rounded px-1.5 py-0.5 font-bold capitalize" :class="drillKindClass(r.kind)">{{ r.kind }}</span>
                <span class="font-mono text-zinc-300">{{ drillAt(r.at) }}</span>
                <span class="font-semibold text-zinc-200">{{ r.ref }}</span>
                <span class="truncate text-zinc-500">{{ r.title }}</span>
                <span class="rounded bg-zinc-800 px-1 py-0.5 text-zinc-400">{{ r.state }}</span>
                <span v-if="r.attempt" class="text-zinc-500">attempt {{ r.attempt }}</span>
                <span v-if="r.overdue_seconds" class="text-rose-400/80">+{{ fmtAge(Number(r.overdue_seconds)) }} past SLA</span>
                <span v-if="r.snooze_until" class="text-sky-300/80" :title="r.snooze_until">snooze → {{ r.snooze_until.slice(0, 16).replace('T', ' ') }}</span>
                <span v-if="r.reschedule_at" class="text-fuchsia-300/90" :title="r.reschedule_at">rescheduled → {{ r.reschedule_at.slice(0, 16).replace('T', ' ') }}</span>
                <span v-if="r.actor" class="ml-auto text-zinc-600">by {{ r.actor }}</span>
              </div>
            </div>
          </template>
        </div>
      </div>

      <!-- v99: the chain report - the chain-history FILE on a cadence (the digest pattern applied to the file) -->
      <div v-if="!loading" class="mb-5 rounded-2xl border border-cyan-900/60 bg-zinc-900/50 p-4">
        <div class="flex flex-wrap items-center gap-2">
          <Mail class="h-3.5 w-3.5 text-cyan-400" />
          <h2 class="text-xs font-bold uppercase tracking-wide text-zinc-300">Chain report</h2>
          <span class="text-[10px] text-zinc-500">the chain-history file on a cadence - every window the same per-leg CSV the plots export rides your bound email endpoint as an attachment; an empty window consumes itself quietly</span>
          <template v-if="chainReport">
            <span class="rounded-full px-2 py-0.5 text-[9px] font-bold"
              :class="chainReport.enabled ? 'bg-emerald-500/15 text-emerald-300' : 'bg-zinc-700/60 text-zinc-400'"
              :title="chainReport.enabled ? 'the door dispatches the file when the window elapses' : 'paused - the schedule stays but nothing dispatches'">
              {{ chainReport.enabled ? 'scheduled' : 'paused' }}
            </span>
            <span class="rounded-full bg-cyan-500/10 px-2 py-0.5 text-[9px] font-bold text-cyan-300" :title="chainReport.cadence === 'weekly' ? 'the weekly digest - the same envelope path, the report\'s own list, the file on its weekly beat' : `every ${chainReport.cadence_seconds}s the file rides the wire`">
              {{ chainReport.cadence === 'weekly' ? 'weekly digest' : `every ${crCadence(chainReport.cadence_seconds)}` }}
            </span>
            <span class="text-[10px] text-zinc-500">to <span class="text-zinc-300">{{ chainReport.to }}</span></span>
            <span v-if="chainReport.recipient_count > 1" class="rounded-full bg-sky-500/10 px-2 py-0.5 text-[9px] font-bold text-sky-300" title="one envelope carries every name - one SMTP conversation, the whole list on it">{{ chainReport.recipient_count }} recipients</span>
            <span v-if="chainReport.chain_count > 1" class="rounded-full bg-fuchsia-500/10 px-2 py-0.5 text-[9px] font-bold text-fuchsia-300" title="the report's tag list - the file covers every chain it names">chains: {{ chainReport.chains.join(' + ') }}</span>
            <span v-else-if="chainReport.chain" class="rounded-full bg-fuchsia-500/10 px-2 py-0.5 text-[9px] font-bold text-fuchsia-300" title="the report watches one chain only">chain: {{ chainReport.chain }}</span>
            <span v-if="chainReport.system" class="rounded-full bg-violet-500/10 px-2 py-0.5 text-[9px] font-bold text-violet-300" title="the report covers only the machines this system binds">system: {{ chainReport.system }}</span>
            <span class="ml-auto flex items-center gap-1.5">
              <button class="flex items-center gap-1 rounded-lg border border-cyan-500/40 bg-cyan-500/10 px-2 py-1 text-[10px] font-bold text-cyan-300 transition hover:bg-cyan-500/20 disabled:opacity-50"
                :disabled="crSending" title="one dispatch NOW, whatever the window says - the same renderer the schedule uses"
                @click="sendChainReportNow">
                <Loader2 v-if="crSending" class="h-3 w-3 animate-spin" />
                <Send v-else class="h-3 w-3" /> Send now
              </button>
              <button class="flex items-center gap-1 rounded-lg border border-zinc-700 px-2 py-1 text-[10px] font-bold text-zinc-300 transition hover:border-zinc-500" @click="crEditing = !crEditing">
                <Pencil class="h-3 w-3" /> {{ crEditing ? 'Close' : 'Edit' }}
              </button>
              <button class="flex items-center gap-1 rounded-lg border border-rose-500/40 px-2 py-1 text-[10px] font-bold text-rose-300 transition hover:bg-rose-500/10 disabled:opacity-50"
                :disabled="crBusy" title="remove the schedule - removing is not pausing, the file stops riding the door"
                @click="removeChainReport">
                <Trash2 class="h-3 w-3" />
              </button>
            </span>
          </template>
          <button v-else class="ml-auto flex items-center gap-1 rounded-lg border border-cyan-500/40 bg-cyan-500/10 px-2.5 py-1 text-[10px] font-bold text-cyan-300 transition hover:bg-cyan-500/20"
            @click="crEditing = true">
            <Plus class="h-3 w-3" /> Schedule the file
          </button>
        </div>
        <!-- the schedule's own bookkeeping - "did the file actually go out?" without grepping logs -->
        <div v-if="chainReport && !crEditing" class="mt-2 flex flex-wrap items-center gap-2">
          <span class="text-[10px] text-zinc-500">depth {{ chainReport.history_limit }} rides per leg</span>
          <span class="text-[10px] text-zinc-500">· next due {{ chainReport.next_due ? chainReport.next_due.slice(0, 16).replace('T', ' ') : 'on the next sweep' }}</span>
          <span v-if="chainReport.last_result" class="rounded-full px-2 py-0.5 text-[9px] font-bold"
            :class="chainReport.last_result.delivery === 'delivered' ? 'bg-emerald-500/15 text-emerald-300'
              : chainReport.last_result.delivery === 'failed' ? 'bg-rose-500/15 text-rose-300'
              : 'bg-amber-500/15 text-amber-300'"
            :title="`${chainReport.last_result.detail || ''} (${chainReport.last_result.legs} leg(s), ${chainReport.last_result.rides} ride(s))`">
            last: {{ chainReport.last_result.delivery }} · {{ chainReport.last_result.rides }} ride(s) · {{ chainReport.last_result.filename }}
          </span>
        </div>
        <p v-if="crError" class="mt-2 text-[10px] text-rose-300">{{ crError }}</p>
        <!-- the form - the same fields the backend validates loud -->
        <div v-if="crEditing" class="mt-3 rounded-xl border border-cyan-500/25 bg-cyan-500/5 p-3">
          <div class="grid gap-2 sm:grid-cols-[1fr_140px_1fr_110px_1fr_140px_auto]">
            <label class="flex items-center gap-1.5 text-[10px] text-zinc-400">
              <input v-model="crForm.enabled" type="checkbox" class="accent-cyan-500" /> scheduled
            </label>
            <input v-model="crForm.to" placeholder="to (a@co.com, b@co.com)"
              class="rounded-lg border border-zinc-800 bg-zinc-950 px-2 py-1.5 text-[11px] text-zinc-200 outline-none focus:border-cyan-500/60"
              title="the recipient LIST - commas or semicolons separate the names; ONE envelope carries every name (ceiling 8); delivered over your bound email endpoint (bind one on /channels)" />
            <input v-model="crForm.chains" placeholder="chains (Revenue, Supply)"
              class="rounded-lg border border-zinc-800 bg-zinc-950 px-2 py-1.5 text-[11px] text-zinc-200 outline-none focus:border-cyan-500/60"
              title="the chain TAG LIST - commas separate the names; the file covers every chain it names (ceiling 8); empty = the whole estate map" />
            <input v-model="crForm.history_limit" type="number" min="1" max="50" placeholder="depth"
              class="rounded-lg border border-zinc-800 bg-zinc-950 px-2 py-1.5 text-[11px] text-zinc-200 outline-none focus:border-cyan-500/60"
              title="the per-leg traversal window (1..50, the map's own clamp)" />
            <input v-model="crForm.system" placeholder="system (optional)"
              class="rounded-lg border border-zinc-800 bg-zinc-950 px-2 py-1.5 text-[11px] text-zinc-200 outline-none focus:border-cyan-500/60"
              title="break the report down by ONE system - the file covers only the machines that system binds (empty = every system); the email names the rides per system either way" />
            <input v-if="crForm.cadence === 'custom'" v-model="crForm.cadence_minutes" type="number" min="5" step="5" placeholder="cadence (min)"
              class="rounded-lg border border-zinc-800 bg-zinc-950 px-2 py-1.5 text-[11px] text-zinc-200 outline-none focus:border-cyan-500/60"
              title="how often the file rides the wire, in minutes (floor 5 - a file dispatch is a minutes concern)" />
            <button v-else class="rounded-lg bg-cyan-500 px-3 py-1.5 text-[11px] font-bold text-zinc-950 transition hover:bg-cyan-400 disabled:opacity-50"
              :disabled="crBusy || !crForm.to.trim()" @click="saveChainReport">
              <Loader2 v-if="crBusy" class="h-3 w-3 animate-spin" /> Save
            </button>
          </div>
          <!-- v101: the named rhythms - the weekly digest rides the same envelope path to the report's own list -->
          <div class="mt-2 flex flex-wrap items-center gap-1.5">
            <span class="text-[10px] text-zinc-500">rhythm</span>
            <button v-for="p in CR_PRESETS" :key="p.key" type="button"
              class="rounded-full px-2.5 py-1 text-[10px] font-bold transition"
              :class="crForm.cadence === p.key ? 'bg-cyan-500/20 text-cyan-200 ring-1 ring-cyan-500/60' : 'bg-zinc-800/70 text-zinc-400 hover:text-zinc-200'"
              :title="p.key === 'weekly' ? 'the weekly digest - ONE envelope path, the report\'s own recipient list, the file attached, every 7 days' : p.key === 'custom' ? 'speak the cadence in minutes (floor 5)' : `every ${p.label === 'hourly' ? 'hour' : p.key} the file rides the wire`"
              @click="crForm.cadence = p.key">
              {{ p.label }}
            </button>
          </div>
          <div v-if="crForm.cadence === 'custom'" class="mt-2">
            <button class="rounded-lg bg-cyan-500 px-3 py-1.5 text-[11px] font-bold text-zinc-950 transition hover:bg-cyan-400 disabled:opacity-50"
              :disabled="crBusy || !crForm.to.trim()" @click="saveChainReport">
              <Loader2 v-if="crBusy" class="h-3 w-3 animate-spin" /> Save
            </button>
          </div>
        </div>
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
              <span v-if="selected.escalation_policy && !selected.escalation_policy.to && !(selected.escalation_policy as any).handlers?.length" class="text-[9px] text-zinc-600">bind escalation_policy.to + a channel endpoint to deliver</span>
              <!-- v91: the door's rhythm is editable from the board -->
              <button class="flex items-center gap-1 rounded-lg border border-indigo-500/40 bg-indigo-500/10 px-2 py-0.5 text-[9px] font-bold text-indigo-300 transition hover:bg-indigo-500/20" @click="policyOpen ? (policyOpen = false) : openPolicyEditor()">
                <SlidersHorizontal class="h-2.5 w-2.5" /> {{ selected.escalation_policy ? 'Edit policy' : 'Add policy' }}
              </button>
              <!-- v89: cross-operator journeys - the legs this machine opens -->
              <span v-for="j in selected.journeys || []" :key="j.on_state" class="flex items-center gap-1 rounded-full bg-fuchsia-500/10 px-2 py-0.5 text-[9px] font-semibold text-fuchsia-300" :title="`when this machine lands on ${j.on_state}, a case opens itself on ${j.open.process}`">
                {{ j.on_state }} → {{ j.open.process }}
              </span>
              <!-- v92: the server's receipt for the last save - what actually moved -->
              <span v-if="policySavedNote" class="flex items-center gap-1 rounded-full bg-emerald-500/10 px-2 py-0.5 text-[9px] font-semibold text-emerald-300">
                <CheckCheck class="h-2.5 w-2.5" /> {{ policySavedNote }}
                <button class="ml-0.5 text-zinc-600 hover:text-zinc-300" title="dismiss" @click="policySavedNote = ''">✕</button>
              </span>
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
            <!-- v94: the escalation-history sparkline - the door's rhythm on
              THIS machine over the last 14 days, drawn from the log -->
            <div v-if="histDays.length" class="mt-3 rounded-xl border border-zinc-800 bg-zinc-950/60 px-3 py-2.5">
              <div class="flex flex-wrap items-center gap-2">
                <p class="text-[9px] font-bold uppercase tracking-widest text-zinc-500">Escalation history - last {{ analytics?.escalation_history?.window_days ?? 14 }} days</p>
                <span class="flex items-center gap-1 text-[8px] text-amber-300/90"><span class="h-1.5 w-1.5 rounded-sm bg-amber-400"></span>escalations</span>
                <span class="flex items-center gap-1 text-[8px] text-emerald-300/90"><span class="h-1.5 w-1.5 rounded-sm bg-emerald-400"></span>acks</span>
                <span class="flex items-center gap-1 text-[8px] text-violet-300/90"><span class="h-1.5 w-1.5 rounded-sm bg-violet-400"></span>digests</span>
                <span v-if="histQuiet" class="ml-auto text-[9px] text-zinc-600">the door has been quiet</span>
              </div>
              <svg viewBox="0 0 300 62" class="mt-1.5 w-full" style="max-width: 460px"
                role="img" aria-label="escalation history - the last 14 days, one bar per day">
                <line x1="20" y1="46.5" x2="296" y2="46.5" stroke="#3f3f46" stroke-width="1" />
                <g v-for="(d, i) in histDays" :key="d.date">
                  <rect v-for="(s, si) in histSegs(d)" :key="`${d.date}-${si}`"
                    :x="histX(i)" :y="s.y" width="14" :height="s.h"
                    :fill="s.fill" rx="1.5" />
                  <rect v-if="d.total" :x="histX(i)" y="8" width="14" height="38" fill="transparent">
                    <title>{{ histTitle(d) }}</title>
                  </rect>
                  <text v-if="i === 0 || i === 7 || i === histDays.length - 1"
                    :x="histX(i) + 7" y="57" text-anchor="middle" font-size="6.5" fill="#71717a">
                    {{ d.date.slice(5) }}
                  </text>
                </g>
              </svg>
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

            <!-- v91: the policy editor - what is saved here rules the NEXT sweep -->
            <div v-if="policyOpen" class="mt-3 rounded-xl border border-indigo-500/30 bg-indigo-500/5 p-3">
              <p class="text-[10px] font-bold uppercase tracking-widest text-indigo-300">Escalation policy - the door's rhythm</p>
              <div class="mt-2 flex flex-wrap items-center gap-2">
                <select v-model="polChannel" class="rounded-lg border border-zinc-800 bg-zinc-950 px-2 py-1 text-[10px] text-zinc-300" title="empty = event-only escalation (business.stuck fires, nobody is knocked)">
                  <option value="">event-only</option>
                  <option value="email">email</option>
                  <option value="sms">sms</option>
                  <option value="whatsapp">whatsapp</option>
                  <option value="telegram">telegram</option>
                  <option value="discord">discord</option>
                </select>
                <input v-model="polTo" placeholder="deliver to (ops@co.com, +1555...)" class="w-48 rounded-lg border border-zinc-800 bg-zinc-950 px-2 py-1 text-[10px] text-zinc-300 outline-none focus:border-indigo-500/60" />
                <select v-model="polMode" class="rounded-lg border border-zinc-800 bg-zinc-950 px-2 py-1 text-[10px] text-zinc-300">
                  <option value="knock">knock - a message per attempt</option>
                  <option value="digest">digest - one summary per window</option>
                </select>
                <input v-model="polCadence" type="number" min="60" class="w-28 rounded-lg border border-zinc-800 bg-zinc-950 px-2 py-1 text-[10px] text-zinc-300 outline-none focus:border-indigo-500/60"
                  :placeholder="polMode === 'digest' ? 'window s' : 'cadence s'"
                  :title="polMode === 'digest' ? 'one summary per this many seconds (min 60)' : 'one knock per this many seconds (min 60)'" />
                <input v-model="polMaxRepeats" type="number" min="0" placeholder="max repeats" class="w-24 rounded-lg border border-zinc-800 bg-zinc-950 px-2 py-1 text-[10px] text-zinc-300 outline-none focus:border-indigo-500/60" />
              </div>
              <div class="mt-2 flex flex-wrap items-center gap-2">
                <input v-model="polTemplate" placeholder="custom message template ({process}, {ref}, {state}, {overdue_minutes}, {attempt})" class="min-w-64 flex-1 rounded-lg border border-zinc-800 bg-zinc-950 px-2 py-1 text-[10px] text-zinc-300 outline-none focus:border-indigo-500/60" />
                <button class="flex items-center gap-1 rounded-lg bg-indigo-500/90 px-3 py-1 text-[10px] font-bold text-zinc-950 transition hover:bg-indigo-400 disabled:opacity-50" :disabled="policySaving" @click="requestPolicySave">
                  <Loader2 v-if="policySaving" class="h-3 w-3 animate-spin" /> Save policy
                </button>
                <button v-if="selected.escalation_policy" class="rounded-lg border border-rose-500/40 bg-rose-500/10 px-2.5 py-1 text-[10px] font-bold text-rose-300 transition hover:bg-rose-500/20 disabled:opacity-50" :disabled="policySaving" title="remove the policy - the machine falls back to one knock per stint, event-only" @click="requestPolicyRemove">
                  Remove
                </button>
              </div>
              <!-- v92: the before/after diff - save shows what moves BEFORE it lands -->
              <div v-if="policyReview" class="mt-2 rounded-xl border border-indigo-500/40 bg-zinc-950/70 px-3 py-2.5">
                <div class="flex flex-wrap items-center gap-2">
                  <p class="text-[10px] font-bold uppercase tracking-widest text-indigo-300">
                    {{ policyReview.after ? 'This save - before / after' : 'Removing the policy' }}
                  </p>
                  <span class="rounded-full bg-indigo-500/15 px-2 py-0.5 text-[9px] font-bold text-indigo-300">{{ policyReview.rows.length }} key{{ policyReview.rows.length === 1 ? '' : 's' }} move{{ policyReview.rows.length === 1 ? 's' : '' }}</span>
                </div>
                <div class="mt-1.5 space-y-1">
                  <div v-for="r in policyReview.rows" :key="r.key" class="flex flex-wrap items-center gap-2 text-[10px]">
                    <span class="w-28 shrink-0 text-zinc-500">{{ POLICY_LABELS[r.key] || r.key }}</span>
                    <span class="rounded bg-rose-500/10 px-1.5 py-0.5 font-mono text-rose-300/90 line-through decoration-rose-400/50">{{ r.before || '(unset)' }}</span>
                    <span class="text-zinc-600">→</span>
                    <span class="rounded bg-emerald-500/10 px-1.5 py-0.5 font-mono text-emerald-300">{{ r.after || '(unset)' }}</span>
                    <span v-if="r.kind === 'added'" class="rounded-full bg-emerald-500/15 px-1.5 py-0.5 text-[8px] font-bold uppercase text-emerald-300">new</span>
                  </div>
                </div>
                <div class="mt-2 flex items-center gap-2">
                  <button class="flex items-center gap-1 rounded-lg bg-indigo-500/90 px-3 py-1 text-[10px] font-bold text-zinc-950 transition hover:bg-indigo-400 disabled:opacity-50" :disabled="policySaving" @click="confirmPolicyReview">
                    <Loader2 v-if="policySaving" class="h-3 w-3 animate-spin" />
                    {{ policyReview.after ? `Confirm save (${policyReview.rows.length})` : 'Confirm remove' }}
                  </button>
                  <button class="rounded-lg border border-zinc-700 px-2.5 py-1 text-[10px] font-bold text-zinc-400 transition hover:text-zinc-200" :disabled="policySaving" @click="policyReview = null">
                    Keep editing
                  </button>
                </div>
              </div>
              <p class="mt-1.5 text-[9px] text-zinc-600">the door reads the policy fresh at every sweep - the new rhythm rules the NEXT tick; running instances and their episodes are untouched</p>
              <p v-if="policyError" class="mt-1 text-[10px] text-rose-300">{{ policyError }}</p>

              <!-- v93: the SLA digest preview - the door's next move, typeset -->
              <div class="mt-2 rounded-xl border border-cyan-500/30 bg-cyan-500/5 px-3 py-2.5">
                <div class="flex flex-wrap items-center gap-2">
                  <p class="flex items-center gap-1.5 text-[10px] font-bold uppercase tracking-widest text-cyan-300">
                    <Eye class="h-3 w-3" /> The door's next move - preview
                  </p>
                  <span v-if="preview" class="rounded-full bg-cyan-500/15 px-2 py-0.5 text-[9px] font-bold uppercase text-cyan-300">{{ preview.mode }}</span>
                  <span v-if="preview" class="text-[9px] text-zinc-500">{{ preview.policy_line }}</span>
                  <Loader2 v-if="previewLoading" class="h-3 w-3 animate-spin text-zinc-600" />
                </div>
                <p v-if="previewError" class="mt-1.5 text-[10px] text-rose-300">the draft was refused: {{ previewError }}</p>
                <template v-if="preview">
                  <div class="mt-1.5 flex flex-wrap items-center gap-2 text-[10px]">
                    <span class="rounded-full px-2 py-0.5 font-bold"
                      :class="preview.overdue_count ? 'bg-rose-500/15 text-rose-300' : 'bg-emerald-500/15 text-emerald-300'">
                      {{ preview.overdue_count }} past SLA right now
                    </span>
                    <span class="text-[10px]" :class="preview.would_deliver ? 'text-emerald-300/80' : 'text-amber-300/80'">{{ preview.delivery_note }}</span>
                  </div>
                  <!-- digest mode: the ONE summary -->
                  <div v-if="preview.digest" class="mt-2 rounded-lg border border-zinc-800 bg-zinc-950/70 px-2.5 py-2">
                    <p class="text-[9px] font-bold uppercase tracking-widest text-zinc-500">the next digest email</p>
                    <p class="mt-1 font-mono text-[10px] font-bold text-zinc-200">{{ preview.digest.subject }}</p>
                    <pre class="mt-1 max-h-40 overflow-y-auto whitespace-pre-wrap font-mono text-[10px] leading-relaxed text-zinc-400">{{ preview.digest.body }}</pre>
                    <p class="mt-1 text-[9px]" :class="preview.digest.due ? 'text-amber-300' : 'text-zinc-500'">
                      {{ preview.digest.due
                        ? 'the window has elapsed - the next sweep sends this summary'
                        : preview.digest.candidates
                          ? `the window elapses in ${fmtAge(preview.digest.next_in_seconds)} (one summary covers every listed item)`
                          : 'nothing past SLA - the next window would say so' }}
                    </p>
                  </div>
                  <!-- knock / event-only: the per-item messages or the record -->
                  <div v-if="preview.messages?.length" class="mt-2 space-y-1">
                    <p class="text-[9px] font-bold uppercase tracking-widest text-zinc-500">
                      {{ preview.mode === 'knock' ? 'the next knocks' : 'the escalation record (event-only)' }}
                    </p>
                    <div v-for="m in preview.messages" :key="m.instance_id" class="rounded-lg border border-zinc-800 bg-zinc-950/70 px-2.5 py-1.5">
                      <div class="flex flex-wrap items-center gap-1.5 text-[9px] text-zinc-500">
                        <span class="font-bold text-zinc-300">{{ m.ref }}</span>
                        <span class="truncate">{{ m.title }}</span>
                        <span class="rounded bg-zinc-800 px-1 py-0.5 text-zinc-400">{{ m.state }}</span>
                        <span>attempt {{ m.attempt }}</span>
                        <span v-if="m.to" class="text-cyan-300/80">→ {{ m.to }}</span>
                      </div>
                      <p v-if="m.subject" class="mt-0.5 font-mono text-[10px] font-bold text-zinc-300">{{ m.subject }}</p>
                      <pre class="mt-0.5 whitespace-pre-wrap font-mono text-[10px] leading-relaxed text-zinc-500">{{ m.message }}</pre>
                    </div>
                  </div>
                  <!-- the holds: why the quiet -->
                  <div v-if="preview.held?.length" class="mt-2 space-y-1">
                    <p class="text-[9px] font-bold uppercase tracking-widest text-zinc-500">held - why the quiet</p>
                    <div v-for="x in preview.held" :key="x.instance_id" class="flex flex-wrap items-center gap-1.5 rounded-lg border border-zinc-800 bg-zinc-950/70 px-2.5 py-1 text-[9px]">
                      <span class="font-bold text-zinc-300">{{ x.ref }}</span>
                      <span class="truncate text-zinc-500">{{ x.title }}</span>
                      <span class="rounded px-1.5 py-0.5 font-semibold"
                        :class="x.reason === 'acknowledged' ? 'bg-emerald-500/15 text-emerald-300' : x.reason === 'too_soon' ? 'bg-amber-500/15 text-amber-300' : 'bg-zinc-800 text-zinc-400'">
                        {{ HELD_LABELS[x.reason] || x.reason }}
                      </span>
                      <span v-if="x.acked_by" class="text-emerald-300/80">by {{ x.acked_by }}</span>
                      <span v-if="x.snooze_remaining_seconds" class="text-sky-300/80">snooze {{ fmtAge(x.snooze_remaining_seconds) }} left</span>
                      <span v-if="x.next_in_seconds" class="text-amber-300/80">next in {{ fmtAge(x.next_in_seconds) }}</span>
                    </div>
                  </div>
                </template>
              </div>
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
                  <span v-else-if="escBook(inst)?.last_delivery === 'digest'" class="rounded-full bg-fuchsia-500/15 px-1.5 py-0.5 text-[9px] font-bold text-fuchsia-300" title="listed in the escalation digest - one summary per window instead of N knocks">digest ×{{ escBook(inst)!.count }}</span>
                  <span v-else-if="escBook(inst)" class="rounded-full bg-indigo-500/15 px-1.5 py-0.5 text-[9px] font-bold text-indigo-300" :title="`last delivery: ${escBook(inst)!.last_delivery || 'n/a'}`">escalated ×{{ escBook(inst)!.count }}</span>
                  <!-- v98: the loan chips - the machine board wears what the attention rows wear (parity); independent of the primary chip above -->
                  <span v-if="escBook(inst)?.acked?.snooze_until" class="rounded-full bg-sky-500/15 px-1.5 py-0.5 text-[9px] font-bold" :class="loanRemaining(escBook(inst)!.acked!.snooze_until) > 0 ? 'text-sky-300' : 'text-zinc-500 line-through'" :title="`the door re-knocks after ${escBook(inst)!.acked!.snooze_until}`">snoozed{{ loanRemaining(escBook(inst)!.acked!.snooze_until) > 0 ? '' : ' (ran out)' }}</span>
                  <span v-if="escBook(inst)?.acked?.reschedule_at && loanRemaining(escBook(inst)!.acked!.reschedule_at) > 0" class="rounded-full bg-fuchsia-500/15 px-1.5 py-0.5 text-[9px] font-bold text-fuchsia-300" :title="`the door re-knocks at ${escBook(inst)!.acked!.reschedule_at}`"><Clock class="mr-0.5 inline h-2.5 w-2.5" /> rescheduled · {{ fmtAge(loanRemaining(escBook(inst)!.acked!.reschedule_at)) }}</span>
                  <span v-else-if="escBook(inst)?.acked?.reschedule_at" class="rounded-full bg-fuchsia-500/10 px-1.5 py-0.5 text-[9px] font-bold text-fuchsia-300/70" :title="`the reschedule ran out at ${escBook(inst)!.acked!.reschedule_at} - the door re-knocked`">rescheduled (ran out)</span>
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
                <!-- v88: the ack form - the handler takes it; v89: the snooze makes it a loan -->
                <div v-if="ackForId === inst.id" class="mt-2 flex flex-wrap items-center gap-2 rounded-xl border border-emerald-500/30 bg-emerald-500/5 px-2.5 py-2">
                  <input v-model="ackBy" placeholder="acknowledged by" class="w-36 rounded-lg border border-zinc-800 bg-zinc-950 px-2 py-1 text-[10px] text-zinc-300 outline-none focus:border-emerald-500/60" />
                  <input v-model="ackNote" placeholder="note (on it, calling now...)" class="w-44 rounded-lg border border-zinc-800 bg-zinc-950 px-2 py-1 text-[10px] text-zinc-300 outline-none focus:border-emerald-500/60" />
                  <input v-model="ackSnooze" type="number" min="0" step="0.5" placeholder="snooze hrs" class="w-24 rounded-lg border border-zinc-800 bg-zinc-950 px-2 py-1 text-[10px] text-zinc-300 outline-none focus:border-emerald-500/60" title="hold the door quiet for N hours, then it re-knocks (empty = owns the rest of the stint)" />
                  <input v-model="ackReschedule" type="number" min="0" step="5" placeholder="reschedule in min" class="w-32 rounded-lg border border-fuchsia-500/30 bg-zinc-950 px-2 py-1 text-[10px] text-zinc-300 outline-none focus:border-fuchsia-500/60" title="v98: re-knock the door at an EXPLICIT moment - now + N minutes (the human picks the time; leave empty if you set snooze hrs - one clock per receipt)" />
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

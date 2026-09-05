<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { Loader2, Phone, Webhook, Copy, Plus, Send, Ban, PlayCircle, Mic, Ear, Bot, Wand2, Users, Megaphone, Volume2, MessageSquare, Hand, Hourglass } from 'lucide-vue-next'
import { useApi } from '~/composables/useApi'

// v69: the REAL adapter surface. A channel endpoint registers a provider
// connection (Meta Cloud API, Telegram Bot API, Discord) and turns its
// NATIVE webhook into interaction-layer ingests + outbound sends. Voice
// sessions are first-class calls: state machine, barge-in, ASR/TTS turns.
// v71: the matrix completes (Telnyx SMS, the any-gateway SMS contract,
// email inbound parse + SMTP) and VOICE AGENTS compose the primitives
// (greeting, ASR engine, TTS voice, barge-in, scaffolded handler) into
// one deployable phone persona. v73: the agent gains a BRAIN (echo or an
// LLM ai_agent grounded on the SAME knowledge binding), per-agent ASR
// confidence analytics, and a REAL model installer for the offline phone.
// v74: the brain routes through a REAL LLM credential (openai, claude,
// deepseek, kimi, qwen, openrouter, ...), the speech loop is VERIFIABLE
// (piper speaks, whisper.cpp hears), and the voice stack grows multi-party
// MEETINGS (legs + merged transcript) and OUTBOUND CAMPAIGNS.
// v76: the room grows a TEXT side channel (group chat + the agent answers
// on the asking leg) and a MODERATOR speaking queue (raise hand -> call
// next grants the floor); the dialer grows voicemail DROPS (greeting_end
// triggers the message + hangup); and channel-side QUEUEING waits callers
// in line (held sessions, FIFO, seat into a meeting on the same call).

interface Endpoint {
  id: string; name: string; provider: string; channel: string; enabled: boolean
  handler_workflow_id: string | null; handler_workflow_name: string | null
  config: Record<string, string>; required_config: Record<string, any>
  webhook_url: string; events_received: number
  last_event_at: string | null; created_at: string | null
}
interface Adapter {
  id: string; channel: string; description: string
  secret_keys: string[]; credential_keys: string[]
}
interface VoiceEvent { id: string; kind: string; payload: Record<string, any>; created_at: string | null }
interface VoiceSession {
  id: string; direction: string; provider: string; call_ref: string
  from: string; to: string; state: string; end_reason: string
  conversation_id: string | null; duration_seconds: number | null
  barge_in_count: number; turn_count: number; active_tts: boolean
  started_at: string | null; events: VoiceEvent[] | null
  agent: Record<string, any> | null
}
interface VoiceAgent {
  id: string; name: string; description: string; greeting_text: string
  speech: Record<string, any>; system_prompt: string
  handler_workflow_id: string | null; handler_workflow_name: string | null
  handler_is_scaffold: boolean; wiring: Record<string, any>
  brain: Record<string, any> | null; knowledge: Record<string, any> | null
}

const { api } = useApi()
const loading = ref(true)
const pageError = ref('')
const note = ref('')  // v75: the last retry-pass note, honest about what moved
const endpoints = ref<Endpoint[]>([])
const adapters = ref<Adapter[]>([])
const sessions = ref<VoiceSession[]>([])
const selected = ref<VoiceSession | null>(null)
const copied = ref('')

const workflows = ref<any[]>([])
const datasets = ref<any[]>([])
const speechEngines = ref<any>(null)
const agents = ref<VoiceAgent[]>([])
const showCreate = ref(false)
const busy = ref(false)
const form = ref({ name: '', provider: 'telegram_bot_api', handler_workflow_id: '', secret: '', credential: '' })
const preview = ref<any>(null)
const previewTo = ref('')
const previewText = ref('Hello from py8n!')

// v71 voice agent builder state
const showAgentCreate = ref(false)
const agentBusy = ref(false)
const agentForm = ref({
  name: '', greeting_text: '', asr_provider: 'py8n_local', tts_provider: 'openai_tts',
  tts_voice: 'alloy', language: 'en-US', barge_in: true, system_prompt: '',
  handler_workflow_id: '', scaffold_handler: true, knowledge_dataset_id: '',
  brain: 'scaffold', brain_model: '', llm_credential_id: '',
})
const ASR_PROVIDERS = ['py8n_local', 'openai_whisper', 'deepgram', 'assemblyai']
const TTS_PROVIDERS = ['openai_tts', 'elevenlabs', 'piper_local', 'meta_mms']

// v73: real model installs + per-agent confidence analytics
const speechModels = ref<any>(null)
const installing = ref('')
const agentAnalytics = ref<Record<string, any>>({})
const analyticsBusy = ref('')

// v74: LLM credentials for the brain, the speech-loop verifier, meetings + campaigns
const allCredentials = ref<any[]>([])
const llmCredentials = computed(() => allCredentials.value.filter(c => c.type === 'openai_compatible' || c.type === 'anthropic'))
const verifyBusy = ref(false)
const verifyResult = ref<any>(null)
const meetings = ref<any[]>([])
const selectedMeeting = ref<any>(null)
const meetingForm = ref({ title: '', agent_id: '' })
const joinForm = ref({ label: '', channel: 'web', address: '' })
const meetingBusy = ref(false)
const campaigns = ref<any[]>([])
const selectedCampaign = ref<any>(null)
const campaignForm = ref({ name: '', agent_id: '', endpoint_id: '', targets: '',
  max_attempts: 3, delays: '15, 60, 1440', amd_mode: 'disabled', amd_on_machine: 'hangup', amd_message: '' })
const campaignBusy = ref(false)
const voiceEndpoints = computed(() => endpoints.value.filter(e => e.provider === 'telnyx_call_control'))

// v76: room chat + hand queue + channel queues
const meetingChat = ref<any[]>([])
const chatForm = ref({ text: '', participant_id: '', author: '', ask_agent: false })
const chatBusy = ref(false)
const queues = ref<any[]>([])
const selectedQueue = ref<any>(null)
const queueForm = ref({ name: '', meeting_id: '' })
const queueEntryForm = ref({ session_id: '' })
const queueBusy = ref(false)

const credTypeLabel: Record<string, string> = { openai_compatible: 'openai-compatible', anthropic: 'claude' }

const stateChip: Record<string, string> = {
  initiated: 'bg-zinc-500/10 text-zinc-300 border-zinc-500/25',
  ringing: 'bg-sky-500/10 text-sky-300 border-sky-500/25',
  in_progress: 'bg-emerald-500/10 text-emerald-300 border-emerald-500/25',
  on_hold: 'bg-amber-500/10 text-amber-300 border-amber-500/25',
  voicemail: 'bg-purple-500/10 text-purple-300 border-purple-500/25',
  ended: 'bg-rose-500/10 text-rose-300 border-rose-500/25',
}

const adapterFor = computed(() => adapters.value.find(a => a.id === form.value.provider))
const requiredSecrets = computed(() => adapterFor.value?.secret_keys || [])
const credentialKeys = computed(() => adapterFor.value?.credential_keys || [])
const primarySecret = computed(() => requiredSecrets.value[0] || '')

async function load() {
  loading.value = true
  pageError.value = ''
  try {
    const [eps, ads, vss, wfs, ags, dss, se, sm, crs, mts, cmps, qss] = await Promise.all([
      api('/channels/endpoints'), api('/channels/adapters'),
      api('/voice/sessions'), api('/workflows?limit=200'), api('/voice/agents'),
      api('/datasets?limit=200'), api('/voice/speech/engines').catch(() => null),
      api('/voice/speech/models').catch(() => null),
      api('/credentials').catch(() => []),
      api('/voice/meetings').catch(() => ({ meetings: [] })),
      api('/voice/campaigns').catch(() => ({ campaigns: [] })),
      api('/voice/queues').catch(() => ({ queues: [] })),
    ])
    endpoints.value = eps.endpoints || []
    adapters.value = ads.adapters || []
    sessions.value = vss.sessions || []
    workflows.value = wfs.workflows || wfs || []
    agents.value = ags.agents || []
    datasets.value = dss.datasets || dss || []
    speechEngines.value = se
    speechModels.value = sm
    allCredentials.value = Array.isArray(crs) ? crs : (crs.credentials || [])
    meetings.value = mts.meetings || []
    campaigns.value = cmps.campaigns || []
    queues.value = qss.queues || []
  } catch (e: any) {
    pageError.value = e?.message || 'failed to load channels'
  } finally {
    loading.value = false
  }
}

function providerLabel(id: string) {
  return ({
    meta_cloud_api: 'Meta Cloud API', telegram_bot_api: 'Telegram Bot API', discord_bot: 'Discord',
    telnyx_call_control: 'Telnyx (SIP + PSTN)', telnyx_sms: 'Telnyx SMS',
    generic_sms: 'Any-Gateway SMS', email_inbound: 'Email (parse + SMTP)',
  } as Record<string, string>)[id] || id
}

function fullWebhookUrl(path: string) {
  if (import.meta.client) return location.origin + path
  return path
}

async function copy(text: string, key: string) {
  try { await navigator.clipboard.writeText(text); copied.value = key; setTimeout(() => { copied.value = '' }, 1500) } catch { /* ignore */ }
}

async function createEndpoint() {
  if (!form.value.name || !primarySecret.value) return
  busy.value = true
  try {
    const config: Record<string, string> = { [primarySecret.value]: form.value.secret }
    if (form.value.credential && credentialKeys.value[0]) config[credentialKeys.value[0]] = form.value.credential
    await api('/channels/endpoints', {
      method: 'POST',
      body: JSON.stringify({
        name: form.value.name, provider: form.value.provider,
        handler_workflow_id: form.value.handler_workflow_id || null, config,
      }),
    })
    showCreate.value = false
    form.value = { name: '', provider: form.value.provider, handler_workflow_id: '', secret: '', credential: '' }
    await load()
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'create failed'
  } finally { busy.value = false }
}

async function toggleEndpoint(ep: Endpoint) {
  await api(`/channels/endpoints/${ep.id}`, { method: 'PUT', body: JSON.stringify({ enabled: !ep.enabled }) })
  await load()
}

async function removeEndpoint(ep: Endpoint) {
  if (!confirm(`Remove endpoint "${ep.name}"? Conversations survive.`)) return
  await api(`/channels/endpoints/${ep.id}`, { method: 'DELETE' })
  await load()
}

async function runPreview(ep: Endpoint) {
  preview.value = { ep, ...(await api(`/channels/endpoints/${ep.id}/preview-outbound`, {
    method: 'POST',
    body: JSON.stringify({ to: previewTo.value || 'chat-id', text: previewText.value || 'Hello from py8n!' }),
  })) }
}

async function openSession(s: VoiceSession) {
  selected.value = await api(`/voice/sessions/${s.id}`)
}

async function applyEvent(s: VoiceSession, kind: string) {
  try {
    await api(`/voice/sessions/${s.id}/events`, { method: 'POST', body: JSON.stringify({ kind }) })
    await load()
    if (selected.value?.id === s.id) await openSession(s)
  } catch (e: any) { pageError.value = e?.data?.detail || e?.message || 'event refused' }
}

async function bargeIn(s: VoiceSession) {
  try {
    await api(`/voice/sessions/${s.id}/barge-in`, { method: 'POST' })
    await load()
    if (selected.value?.id === s.id) await openSession(s)
  } catch (e: any) { pageError.value = e?.data?.detail || e?.message || 'barge-in refused' }
}

async function createAgent() {
  if (!agentForm.value.name) return
  agentBusy.value = true
  try {
    await api('/voice/agents', {
      method: 'POST',
      body: JSON.stringify({
        name: agentForm.value.name, greeting_text: agentForm.value.greeting_text,
        asr_provider: agentForm.value.asr_provider, tts_provider: agentForm.value.tts_provider,
        tts_voice: agentForm.value.tts_voice, language: agentForm.value.language,
        barge_in: agentForm.value.barge_in, system_prompt: agentForm.value.system_prompt,
        handler_workflow_id: agentForm.value.handler_workflow_id || null,
        scaffold_handler: !agentForm.value.handler_workflow_id && agentForm.value.scaffold_handler,
        knowledge_dataset_id: agentForm.value.knowledge_dataset_id || null,
        brain: agentForm.value.handler_workflow_id ? undefined : agentForm.value.brain,
        brain_model: agentForm.value.brain_model || '',
        llm_credential_id: (!agentForm.value.handler_workflow_id && agentForm.value.brain === 'ai_agent' && agentForm.value.llm_credential_id) ? agentForm.value.llm_credential_id : null,
      }),
    })
    showAgentCreate.value = false
    agentForm.value = { name: '', greeting_text: '', asr_provider: agentForm.value.asr_provider,
      tts_provider: agentForm.value.tts_provider, tts_voice: 'alloy', language: 'en-US',
      barge_in: true, system_prompt: '', handler_workflow_id: '', scaffold_handler: true,
      knowledge_dataset_id: '', brain: 'scaffold', brain_model: '', llm_credential_id: '' }
    await load()
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'agent create failed'
  } finally { agentBusy.value = false }
}

async function removeAgent(a: VoiceAgent) {
  if (!confirm(`Delete voice agent "${a.name}"? Sessions keep the config they copied.`)) return
  await api(`/voice/agents/${a.id}`, { method: 'DELETE' })
  await load()
}

async function installModel(slug: string) {
  if (!confirm(`Download + install the "${slug}" model now? The download is real and blocking.`)) return
  installing.value = slug
  try {
    await api('/voice/speech/models/install', { method: 'POST', body: JSON.stringify({ slug }) })
    speechModels.value = await api('/voice/speech/models')
    speechEngines.value = await api('/voice/speech/engines').catch(() => null)
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'model install failed'
  } finally { installing.value = '' }
}

async function showAnalytics(a: VoiceAgent) {
  analyticsBusy.value = a.id
  try {
    agentAnalytics.value = { ...agentAnalytics.value, [a.id]: await api(`/voice/agents/${a.id}/analytics`) }
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'analytics failed'
  } finally { analyticsBusy.value = '' }
}

function trendColor(d: string) {
  return ({ improving: 'text-emerald-300', stable: 'text-sky-300', degrading: 'text-amber-300', unknown: 'text-zinc-400' } as Record<string, string>)[d] || 'text-zinc-400'
}

// v74: the speech-loop verifier, meetings, campaigns
async function runVerify() {
  verifyBusy.value = true
  verifyResult.value = null
  try {
    verifyResult.value = await api('/voice/speech/verify', { method: 'POST', body: JSON.stringify({}) })
  } catch (e: any) {
    verifyResult.value = { ok: false, error: e?.data?.detail || e?.message || 'verify failed' }
  } finally { verifyBusy.value = false }
}

async function loadMeetingDetail(m: any) {
  selectedMeeting.value = await api(`/voice/meetings/${m.id}`)
  try {
    meetingChat.value = (await api(`/voice/meetings/${m.id}/chat?limit=50`)).messages || []
  } catch { meetingChat.value = [] }
}

// v76: the room's TEXT side channel
async function postChat(m: any) {
  if (!chatForm.value.text.trim()) return
  chatBusy.value = true
  try {
    await api(`/voice/meetings/${m.id}/chat`, {
      method: 'POST',
      body: JSON.stringify({
        text: chatForm.value.text,
        participant_id: chatForm.value.participant_id || null,
        author: chatForm.value.author || undefined,
        ask_agent: chatForm.value.ask_agent,
      }),
    })
    chatForm.value = { text: '', participant_id: chatForm.value.participant_id, author: '', ask_agent: false }
    await loadMeetingDetail(m)
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'chat failed'
  } finally { chatBusy.value = false }
}

// v76: the moderator's speaking queue
async function raiseHand(m: any, p: any, note = '') {
  meetingBusy.value = true
  try {
    await api(`/voice/meetings/${m.id}/hand`, {
      method: 'POST', body: JSON.stringify({ participant_id: p.id, note }) })
    await loadMeetingDetail(m)
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'raise hand failed'
  } finally { meetingBusy.value = false }
}

async function lowerHand(m: any, p: any) {
  meetingBusy.value = true
  try {
    await api(`/voice/meetings/${m.id}/hand/${p.id}`, { method: 'DELETE' })
    await loadMeetingDetail(m)
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'lower hand failed'
  } finally { meetingBusy.value = false }
}

async function callNextHand(m: any) {
  meetingBusy.value = true
  try {
    await api(`/voice/meetings/${m.id}/hand/next`, { method: 'POST' })
    await loadMeetingDetail(m)
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'call next failed'
  } finally { meetingBusy.value = false }
}

// v76: channel queues - the waiting room on the channel side
async function createQueue() {
  queueBusy.value = true
  try {
    const created = await api('/voice/queues', {
      method: 'POST',
      body: JSON.stringify({ name: queueForm.value.name,
                              meeting_id: queueForm.value.meeting_id || null }) })
    queueForm.value = { name: '', meeting_id: '' }
    await load()
    await openQueue(created)
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'queue create failed'
  } finally { queueBusy.value = false }
}

async function openQueue(q: any) {
  selectedQueue.value = await api(`/voice/queues/${q.id}`)
}

async function enqueueSession(q: any) {
  if (!queueEntryForm.value.session_id) return
  queueBusy.value = true
  try {
    const res = await api(`/voice/queues/${q.id}/entries`, {
      method: 'POST', body: JSON.stringify({ session_id: queueEntryForm.value.session_id }) })
    queueEntryForm.value = { session_id: '' }
    selectedQueue.value = res
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'enqueue failed'
  } finally { queueBusy.value = false }
}

async function seatQueueNext(q: any) {
  queueBusy.value = true
  try {
    const res = await api(`/voice/queues/${q.id}/next`, { method: 'POST', body: JSON.stringify({}) })
    note.value = res.note || 'seated'
    await load()
    await openQueue(res)
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'seat failed'
  } finally { queueBusy.value = false }
}

async function queueLeave(q: any, entry: any) {
  queueBusy.value = true
  try {
    const res = await api(`/voice/queues/${q.id}/entries/${entry.id}/leave`, { method: 'POST' })
    selectedQueue.value = res
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'leave failed'
  } finally { queueBusy.value = false }
}

async function toggleQueueState(q: any) {
  queueBusy.value = true
  try {
    const res = await api(`/voice/queues/${q.id}/state`, {
      method: 'POST', body: JSON.stringify({ state: q.state === 'open' ? 'closed' : 'open' }) })
    selectedQueue.value = res
    await load()
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'state change failed'
  } finally { queueBusy.value = false }
}

async function createMeeting() {
  meetingBusy.value = true
  try {
    const created = await api('/voice/meetings', {
      method: 'POST',
      body: JSON.stringify({ title: meetingForm.value.title, agent_id: meetingForm.value.agent_id || null }),
    })
    meetingForm.value = { title: '', agent_id: '' }
    await load()
    await loadMeetingDetail(created)
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'meeting create failed'
  } finally { meetingBusy.value = false }
}

async function joinMeeting(m: any) {
  if (!joinForm.value.label) return
  meetingBusy.value = true
  try {
    const res = await api(`/voice/meetings/${m.id}/join`, {
      method: 'POST',
      body: JSON.stringify({
        label: joinForm.value.label, channel: joinForm.value.channel,
        address: joinForm.value.address || '',
      }),
    })
    joinForm.value = { label: '', channel: joinForm.value.channel, address: '' }
    await load()
    await loadMeetingDetail(res.meeting)
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'join failed'
  } finally { meetingBusy.value = false }
}

async function endMeeting(m: any) {
  if (!confirm('End the meeting? Every live leg hangs up.')) return
  await api(`/voice/meetings/${m.id}/end`, { method: 'POST' })
  await load()
  await loadMeetingDetail(m)
}

async function loadCampaignDetail(c: any) {
  selectedCampaign.value = await api(`/voice/campaigns/${c.id}`)
}

function parseTargets(text: string) {
  return text.split('\n').map(l => l.trim()).filter(Boolean).map(l => {
    const [address, name] = l.split(',').map(s => (s || '').trim())
    return name ? { address, name } : { address }
  })
}

async function createCampaign() {
  meetingBusy.value = true
  try {
    const delays = campaignForm.value.delays.split(',').map(s => parseInt(s.trim(), 10)).filter(n => Number.isFinite(n))
    const created = await api('/voice/campaigns', {
      method: 'POST',
      body: JSON.stringify({
        name: campaignForm.value.name, agent_id: campaignForm.value.agent_id,
        endpoint_id: campaignForm.value.endpoint_id || null,
        targets: parseTargets(campaignForm.value.targets),
        config: {
          retry: { max_attempts: campaignForm.value.max_attempts,
                   delays_minutes: delays.length ? delays : [15, 60, 1440],
                   retry_on: ['no_answer'] },
          amd: { mode: campaignForm.value.amd_mode,
                 on_machine: campaignForm.value.amd_on_machine,
                 ...(campaignForm.value.amd_message ? { voicemail_message: campaignForm.value.amd_message } : {}) },
        },
      }),
    })
    campaignForm.value = { name: '', agent_id: '', endpoint_id: '', targets: '',
      max_attempts: 3, delays: '15, 60, 1440', amd_mode: 'disabled', amd_on_machine: 'hangup', amd_message: '' }
    await load()
    await loadCampaignDetail(created)
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'campaign create failed'
  } finally { meetingBusy.value = false }
}

async function startCampaign(c: any) {
  campaignBusy.value = true
  try {
    const res = await api(`/voice/campaigns/${c.id}/start`, { method: 'POST', body: JSON.stringify({}) })
    await load()
    await loadCampaignDetail(c)
    if (res.start_note) pageError.value = ''
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'start failed'
  } finally { campaignBusy.value = false }
}

async function simulateAnswer(c: any, t: any) {
  try {
    await api(`/voice/campaigns/${c.id}/targets/${t.id}/simulate-answer`, { method: 'POST' })
    await load()
    await loadCampaignDetail(c)
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'simulate failed'
  }
}

// v75: mix + floor controls, retry passes, AMD simulation
async function setMix(m: any, p: any, key: 'muted' | 'deafened' | 'solo', val: boolean) {
  meetingBusy.value = true
  try {
    await api(`/voice/meetings/${m.id}/participants/${p.id}/mix`, {
      method: 'PATCH', body: JSON.stringify({ [key]: val }) })
    await loadMeetingDetail(m)
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'mix failed'
  } finally { meetingBusy.value = false }
}

async function setFloor(m: any, mode: 'auto' | 'directed', participantId?: string) {
  meetingBusy.value = true
  try {
    const res = await api(`/voice/meetings/${m.id}/floor`, {
      method: 'POST', body: JSON.stringify({ mode, participant_id: participantId || null }) })
    selectedMeeting.value = res.meeting
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'floor failed'
  } finally { meetingBusy.value = false }
}

async function retryCampaign(c: any, force = false) {
  campaignBusy.value = true
  try {
    const res = await api(`/voice/campaigns/${c.id}/retry`, {
      method: 'POST', body: JSON.stringify({ force }) })
    await load()
    await loadCampaignDetail(c)
    if (res.retry_note) note.value = res.retry_note
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'retry failed'
  } finally { campaignBusy.value = false }
}

async function simulateMachine(c: any, t: any, mode: boolean | string = true) {
  try {
    await api(`/voice/campaigns/${c.id}/targets/${t.id}/simulate-answer`, {
      method: 'POST', body: JSON.stringify({ as_machine: mode }) })
    await load()
    await loadCampaignDetail(c)
  } catch (e: any) {
    pageError.value = e?.data?.detail || e?.message || 'simulate failed'
  }
}

const liveSessions = computed(() => sessions.value.filter(s => s.state !== 'ended'))
const endedSessions = computed(() => sessions.value.filter(s => s.state === 'ended'))

onMounted(load)
</script>

<template>
  <div class="p-6 max-w-7xl mx-auto space-y-8">
    <header class="flex items-start justify-between gap-4">
      <div>
        <h1 class="text-2xl font-semibold text-zinc-100 flex items-center gap-2">
          <Webhook class="w-6 h-6 text-violet-400" /> Channels
        </h1>
        <p class="text-sm text-zinc-400 mt-1 max-w-3xl">
          Real provider adapters - Meta Cloud API (WhatsApp, interactive buttons included), Telegram,
          Discord, Telnyx Call Control for SIP + PSTN voice, Telnyx SMS, the any-gateway SMS contract
          and Email (inbound parse + SMTP) - each webhook-native and verified with its own
          credentials, feeding the SAME conversation layer. Voice Agents compose the voice stack
          (greeting, ASR engine, TTS voice, barge-in, scaffolded handler) into one deployable phone
          persona, bound to a KNOWLEDGE DATASET so every call answers from your data, with the LLM
          brain routed through a REAL provider credential (openai, claude, deepseek, kimi, qwen,
          openrouter, ...). Meetings give the stack legs (multi-party rooms with a merged,
          speaker-attributed transcript) and campaigns dial outbound lists through the same agents;
          the v70 media transport transcribes through the agent's engine (local whisper.cpp /
          vosk / piper bridges when installed).
        </p>
      </div>
      <div class="flex gap-2 shrink-0">
        <button class="btn btn-ghost flex items-center gap-2" @click="showAgentCreate = true">
          <Bot class="w-4 h-4" /> New voice agent
        </button>
        <button class="btn btn-primary flex items-center gap-2" @click="showCreate = true">
          <Plus class="w-4 h-4" /> New endpoint
        </button>
      </div>
    </header>

    <div v-if="pageError" class="rounded-lg border border-rose-500/30 bg-rose-500/10 text-rose-300 px-4 py-3 text-sm">{{ pageError }}</div>
    <div v-if="loading" class="flex items-center gap-2 text-zinc-400"><Loader2 class="w-4 h-4 animate-spin" /> Loading channels…</div>

    <template v-if="!loading">
      <section class="flex flex-wrap gap-3">
        <div v-for="a in adapters" :key="a.id" class="rounded-xl border border-zinc-800 bg-zinc-900/50 px-4 py-3 min-w-56">
          <div class="text-sm font-medium text-zinc-200">{{ providerLabel(a.id) }}</div>
          <div class="text-xs text-zinc-500 mt-0.5">channel: <span class="text-zinc-300">{{ a.channel }}</span></div>
          <div class="text-xs text-zinc-500 mt-1">verifies: <span class="text-zinc-300 font-mono">{{ a.secret_keys.join(', ') }}</span></div>
        </div>
      </section>

      <section class="space-y-3">
        <h2 class="text-sm font-semibold text-zinc-300 uppercase tracking-wide">Provider endpoints ({{ endpoints.length }})</h2>
        <p v-if="!endpoints.length" class="text-sm text-zinc-500">No endpoints yet - register a provider connection to receive its native webhooks.</p>
        <div v-for="ep in endpoints" :key="ep.id" class="rounded-xl border border-zinc-800 bg-zinc-900/40 p-4 space-y-3">
          <div class="flex items-center justify-between gap-3 flex-wrap">
            <div class="flex items-center gap-2">
              <span class="font-medium text-zinc-100">{{ ep.name }}</span>
              <span class="text-xs px-2 py-0.5 rounded-full border border-violet-500/25 bg-violet-500/10 text-violet-300">{{ providerLabel(ep.provider) }}</span>
              <span class="text-xs px-2 py-0.5 rounded-full border border-zinc-600/40 bg-zinc-700/20 text-zinc-300">{{ ep.channel }}</span>
              <span class="text-xs px-2 py-0.5 rounded-full border"
                    :class="ep.enabled ? 'border-emerald-500/25 bg-emerald-500/10 text-emerald-300' : 'border-rose-500/25 bg-rose-500/10 text-rose-300'">
                {{ ep.enabled ? 'enabled' : 'disabled' }}
              </span>
            </div>
            <div class="flex items-center gap-2 text-xs text-zinc-500">
              <span>{{ ep.events_received }} webhook{{ ep.events_received === 1 ? '' : 's' }} received</span>
              <button class="btn btn-ghost" @click="toggleEndpoint(ep)">{{ ep.enabled ? 'Disable' : 'Enable' }}</button>
              <button class="btn btn-ghost text-rose-300" @click="removeEndpoint(ep)"><Ban class="w-3.5 h-3.5" /></button>
            </div>
          </div>
          <div class="flex items-center gap-2 text-xs">
            <code class="font-mono text-zinc-300 bg-zinc-800/60 rounded px-2 py-1">{{ ep.webhook_url }}</code>
            <button class="btn btn-ghost" @click="copy(fullWebhookUrl(ep.webhook_url), ep.id)">
              <Copy class="w-3.5 h-3.5" /> {{ copied === ep.id ? 'Copied' : 'Copy' }}
            </button>
          </div>
          <div class="text-xs text-zinc-500">
            handler: <span class="text-zinc-300">{{ ep.handler_workflow_name || 'none bound' }}</span>
            · config: <span class="font-mono text-zinc-400">{{ Object.entries(ep.config).map(([k, v]) => `${k}=${v}`).join(', ') || '{}' }}</span>
          </div>
          <details class="text-xs">
            <summary class="cursor-pointer text-zinc-400 hover:text-zinc-200">Preview outbound request</summary>
            <div class="mt-2 flex flex-wrap items-end gap-2">
              <label class="text-zinc-500">to <input v-model="previewTo" class="input input-xs w-40" placeholder="chat id / wa id" /></label>
              <label class="text-zinc-500">text <input v-model="previewText" class="input input-xs w-56" /></label>
              <button class="btn btn-xs" @click="runPreview(ep)"><Send class="w-3 h-3" /> Build</button>
            </div>
            <pre v-if="preview && preview.ep.id === ep.id" class="mt-2 text-[11px] font-mono bg-zinc-800/60 rounded p-2 overflow-x-auto text-zinc-300">{{ JSON.stringify({ method: preview.method, url: preview.url, headers: preview.headers, json: preview.json, would_deliver: preview.would_deliver }, null, 2) }}</pre>
          </details>
        </div>
      </section>

      <section class="space-y-3">
        <h2 class="text-sm font-semibold text-zinc-300 uppercase tracking-wide flex items-center gap-2">
          <Bot class="w-4 h-4 text-fuchsia-400" /> Voice agents ({{ agents.length }})
          <span class="text-xs text-zinc-500 normal-case font-normal">greeting · ASR engine · TTS voice · barge-in · knowledge binding</span>
        </h2>
        <details v-if="speechEngines" class="text-xs">
          <summary class="cursor-pointer text-zinc-400 hover:text-zinc-200">
            Local speech engines - ASR:
            <span :class="speechEngines.asr?.local_engine_registered ? 'text-emerald-300' : 'text-amber-300'">{{ speechEngines.asr?.local_engine_registered ? 'py8n_local live' : 'not bound' }}</span>
            · TTS:
            <span :class="speechEngines.tts?.local_engine_registered ? 'text-emerald-300' : 'text-amber-300'">{{ speechEngines.tts?.local_engine_registered ? 'piper live' : 'not bound' }}</span>
            (probe details)
          </summary>
          <div class="mt-2 space-y-1 text-zinc-400">
            <p>vosk: {{ speechEngines.asr?.vosk?.note }}</p>
            <p>whisper.cpp: {{ speechEngines.asr?.['whisper.cpp']?.note }}</p>
            <p>piper: {{ speechEngines.tts?.piper?.note }}</p>
            <div class="flex items-center gap-2 pt-1">
              <button class="btn btn-ghost text-xs text-emerald-300" :disabled="verifyBusy" @click="runVerify">
                <Loader2 v-if="verifyBusy" class="w-3.5 h-3.5 animate-spin" /><Volume2 class="w-3.5 h-3.5" /> Verify the speech loop (piper speaks, the ASR hears)
              </button>
            </div>
            <div v-if="verifyResult" class="rounded-lg border border-zinc-800 bg-zinc-900/60 p-3 space-y-1">
              <template v-if="verifyResult.error"><p class="text-amber-300">{{ verifyResult.error }}</p></template>
              <template v-else>
                <p>spoken: <span class="text-zinc-200">"{{ verifyResult.spoken }}"</span> · heard: <span :class="verifyResult.ok ? 'text-emerald-300' : 'text-amber-300'">"{{ verifyResult.heard }}"</span></p>
                <p>match: {{ verifyResult.match_ratio }} ({{ verifyResult.exact ? 'exact' : 'fuzzy' }}) · confidence: {{ verifyResult.confidence }} · asr backend: {{ verifyResult.asr?.backend || verifyResult.asr?.engine }}</p>
                <p class="text-zinc-500">{{ verifyResult.note }}</p>
              </template>
            </div>
          </div>
        </details>
        <details v-if="speechModels" class="text-xs">
          <summary class="cursor-pointer text-zinc-400 hover:text-zinc-200">
            Speech models
            <span v-if="speechModels.offline_phone?.ready" class="ml-1 px-2 py-0.5 rounded-full border border-emerald-500/25 bg-emerald-500/10 text-emerald-300">offline phone ready</span>
            <span class="text-zinc-500">- download real vosk / whisper.cpp / piper artifacts and rebind</span>
          </summary>
          <div class="mt-2 space-y-2">
            <p v-if="!speechModels.offline_phone?.ready" class="text-zinc-500">
              ASR {{ speechModels.offline_phone?.asr_local ? 'local' : 'not bound' }} · TTS {{ speechModels.offline_phone?.tts_local ? 'local' : 'not bound' }}
              - install what is missing for a fully offline phone.
            </p>
            <div v-for="m in speechModels.models || []" :key="m.slug" class="flex items-center justify-between gap-3 rounded-lg border border-zinc-800 bg-zinc-900/60 px-3 py-2">
              <div class="min-w-0">
                <div class="flex items-center gap-2">
                  <span class="text-zinc-200">{{ m.title }}</span>
                  <span class="text-[10px] px-1.5 py-0.5 rounded-full border border-zinc-600/40 bg-zinc-700/20 text-zinc-400">{{ m.engine }}</span>
                  <span v-if="m.installed" class="text-[10px] px-1.5 py-0.5 rounded-full border border-emerald-500/25 bg-emerald-500/10 text-emerald-300">on disk</span>
                </div>
                <p class="text-zinc-500 truncate">{{ m.description }}</p>
                <p class="text-zinc-600">needs: {{ m.requires }} · {{ m.after }}</p>
              </div>
              <button class="btn btn-ghost text-xs shrink-0" :disabled="installing === m.slug" @click="installModel(m.slug)">
                <Loader2 v-if="installing === m.slug" class="w-3.5 h-3.5 animate-spin" />{{ m.installed ? 'Reinstall' : 'Install' }}
              </button>
            </div>
          </div>
        </details>
        <p v-if="!agents.length" class="text-sm text-zinc-500">No agents yet - create one to compose the voice primitives into a deployable phone persona.</p>
        <div v-for="a in agents" :key="a.id" class="rounded-xl border border-zinc-800 bg-zinc-900/40 p-4 space-y-3">
          <div class="flex items-center justify-between gap-3 flex-wrap">
            <div class="flex items-center gap-2 flex-wrap">
              <span class="font-medium text-zinc-100">{{ a.name }}</span>
              <span class="text-xs px-2 py-0.5 rounded-full border border-fuchsia-500/25 bg-fuchsia-500/10 text-fuchsia-300">asr: {{ a.speech.asr_provider }}<span v-if="!a.speech.asr_engine_registered" class="text-amber-400"> (unregistered)</span></span>
              <span class="text-xs px-2 py-0.5 rounded-full border border-sky-500/25 bg-sky-500/10 text-sky-300">tts: {{ a.speech.tts_provider }}/{{ a.speech.tts_voice }}</span>
              <span class="text-xs px-2 py-0.5 rounded-full border border-zinc-600/40 bg-zinc-700/20 text-zinc-300">{{ a.speech.language }}</span>
              <span class="text-xs px-2 py-0.5 rounded-full border"
                    :class="a.speech.barge_in ? 'border-emerald-500/25 bg-emerald-500/10 text-emerald-300' : 'border-zinc-600/40 bg-zinc-700/20 text-zinc-400'">
                {{ a.speech.barge_in ? 'barge-in ok' : 'no barge-in' }}
              </span>
              <span v-if="a.handler_is_scaffold" class="text-xs px-2 py-0.5 rounded-full border border-amber-500/25 bg-amber-500/10 text-amber-300">scaffolded handler</span>
              <span v-if="a.brain?.kind === 'ai_agent'" class="text-xs px-2 py-0.5 rounded-full border border-violet-500/25 bg-violet-500/10 text-violet-300">LLM brain<span v-if="a.brain.model"> · {{ a.brain.model }}</span><span v-if="a.brain.credential_name"> · via {{ a.brain.credential_name }} ({{ credTypeLabel[allCredentials.find(c => c.id === a.brain.credential_id)?.type] || 'credential' }})</span></span>
              <span v-else-if="a.brain?.kind === 'scaffold' && a.handler_is_scaffold" class="text-xs px-2 py-0.5 rounded-full border border-zinc-600/40 bg-zinc-700/20 text-zinc-400">echo brain</span>
              <span v-if="a.knowledge" class="text-xs px-2 py-0.5 rounded-full border border-teal-500/25 bg-teal-500/10 text-teal-300">knowledge: {{ a.knowledge.dataset_name || a.knowledge.dataset_id }} · top {{ a.knowledge.top_k }}</span>
            </div>
            <button class="btn btn-ghost text-rose-300 text-xs" @click="removeAgent(a)"><Ban class="w-3.5 h-3.5" /></button>
          </div>
          <div class="text-xs text-zinc-500">
            greeting: <span class="text-zinc-300">{{ a.greeting_text || 'none - the call starts silent' }}</span>
            · handler: <span class="text-zinc-300">{{ a.handler_workflow_name || 'none' }}</span>
            <span v-if="a.system_prompt"> · persona: <span class="text-zinc-400">{{ a.system_prompt.slice(0, 60) }}{{ a.system_prompt.length > 60 ? '…' : '' }}</span></span>
          </div>
          <div class="flex items-center gap-2">
            <button class="btn btn-ghost text-xs text-sky-300" :disabled="analyticsBusy === a.id" @click="showAnalytics(a)">
              <Loader2 v-if="analyticsBusy === a.id" class="w-3.5 h-3.5 animate-spin" />ASR analytics
            </button>
          </div>
          <div v-if="agentAnalytics[a.id]" class="rounded-lg border border-zinc-800 bg-zinc-900/60 p-3 text-xs space-y-1">
            <div class="flex items-center gap-3 flex-wrap text-zinc-300">
              <span>{{ agentAnalytics[a.id].turns_total }} turns</span>
              <span>mean confidence: <span class="text-zinc-100">{{ agentAnalytics[a.id].confidence.mean ?? 'n/a' }}</span></span>
              <span>weak: {{ agentAnalytics[a.id].confidence.weak_turns }} ({{ agentAnalytics[a.id].confidence.weak_turn_rate ?? 0 }} rate)</span>
              <span>directions:
                <span :class="trendColor('improving')">↑{{ agentAnalytics[a.id].directions.improving }}</span>
                <span class="text-sky-300">→{{ agentAnalytics[a.id].directions.stable }}</span>
                <span :class="trendColor('degrading')">↓{{ agentAnalytics[a.id].directions.degrading }}</span>
                <span class="text-zinc-500">?{{ agentAnalytics[a.id].directions.unknown }}</span>
              </span>
            </div>
            <div v-for="s in agentAnalytics[a.id].per_session || []" :key="s.session_id" class="flex items-center gap-2 text-zinc-500">
              <span class="font-mono">{{ s.session_id.slice(0, 8) }}</span>
              <span>{{ s.confidence.turns }} turns</span>
              <span>mean {{ s.confidence.mean ?? 'n/a' }}</span>
              <span :class="trendColor(s.direction)">{{ s.direction }}</span>
            </div>
            <p class="text-zinc-600">{{ agentAnalytics[a.id].note }}</p>
          </div>
          <details class="text-xs">
            <summary class="cursor-pointer text-zinc-400 hover:text-zinc-200">Wiring (provider webhook + media stream + knowledge)</summary>
            <div class="mt-2 space-y-1">
              <p class="text-zinc-400">{{ a.wiring.inbound_webhook }}</p>
              <p class="text-zinc-400">{{ a.wiring.media_stream }}</p>
              <p class="text-amber-300/80">{{ a.wiring.asr_note }}</p>
              <p v-if="a.wiring.knowledge_note" class="text-teal-300/80">{{ a.wiring.knowledge_note }}</p>
              <p v-if="a.wiring.brain_note" class="text-violet-300/80">{{ a.wiring.brain_note }}</p>
            </div>
          </details>
        </div>
      </section>

      <section class="space-y-3">
        <h2 class="text-sm font-semibold text-zinc-300 uppercase tracking-wide flex items-center gap-2">
          <Users class="w-4 h-4 text-emerald-400" /> Voice meetings ({{ meetings.length }})
          <span class="text-xs text-zinc-500 normal-case font-normal">multi-party legs · mix controls (mute/deafen/solo) · floor control · room chat · moderator speaking queue · merged transcript</span>
        </h2>
        <div class="flex flex-wrap items-end gap-2">
          <label class="text-xs text-zinc-500">title <input v-model="meetingForm.title" class="input input-xs w-48" placeholder="Monday standup room" /></label>
          <label class="text-xs text-zinc-500">agent
            <select v-model="meetingForm.agent_id" class="input input-xs w-44">
              <option value="">- no persona -</option>
              <option v-for="a in agents" :key="a.id" :value="a.id">{{ a.name }}</option>
            </select>
          </label>
          <button class="btn btn-ghost text-xs" :disabled="meetingBusy" @click="createMeeting"><Plus class="w-3.5 h-3.5" /> New meeting</button>
        </div>
        <p v-if="!meetings.length" class="text-sm text-zinc-500">No meetings yet - create a room, then join legs: web participants attach their media stream to the leg's session websocket, phone legs are dialed through a telnyx endpoint.</p>
        <div v-for="m in meetings" :key="m.id" class="rounded-xl border border-zinc-800 bg-zinc-900/40 p-4 space-y-2">
          <div class="flex items-center justify-between gap-3 flex-wrap">
            <div class="flex items-center gap-2 text-sm">
              <span class="text-zinc-100">{{ m.title || 'untitled room' }}</span>
              <span class="text-xs px-2 py-0.5 rounded-full border" :class="m.state === 'active' ? 'border-emerald-500/25 bg-emerald-500/10 text-emerald-300' : 'border-rose-500/25 bg-rose-500/10 text-rose-300'">{{ m.state }}</span>
              <span class="text-xs text-zinc-500">{{ m.counts?.participants }} leg(s) · {{ m.counts?.live_legs }} live</span>
              <span v-if="m.agent_name" class="text-xs text-fuchsia-300">agent: {{ m.agent_name }}</span>
            </div>
            <div class="flex items-center gap-2">
              <button class="btn btn-ghost text-xs" @click="loadMeetingDetail(m)">Open</button>
              <button v-if="m.state === 'active'" class="btn btn-ghost text-rose-300 text-xs" @click="endMeeting(m)">End</button>
            </div>
          </div>
        </div>
        <div v-if="selectedMeeting" class="rounded-xl border border-zinc-800 bg-zinc-900/60 p-4 space-y-3">
          <div class="flex items-center justify-between">
            <h3 class="text-sm text-zinc-200">{{ selectedMeeting.title || 'room' }} · transcript &amp; legs</h3>
            <button class="btn btn-ghost" @click="selectedMeeting = null">Close</button>
          </div>
          <div v-if="selectedMeeting.state === 'active'" class="flex flex-wrap items-end gap-2 text-xs">
            <label class="text-zinc-500">label <input v-model="joinForm.label" class="input input-xs w-32" placeholder="Alice (web)" /></label>
            <label class="text-zinc-500">channel
              <select v-model="joinForm.channel" class="input input-xs w-28">
                <option value="web">web</option>
                <option value="telnyx">telnyx (dial)</option>
                <option value="sip">sip (dial)</option>
              </select>
            </label>
            <label v-if="joinForm.channel !== 'web'" class="text-zinc-500">address <input v-model="joinForm.address" class="input input-xs w-40" placeholder="+15551234567 / sip:..." /></label>
            <button class="btn btn-ghost text-xs" :disabled="meetingBusy || !joinForm.label" @click="joinMeeting(selectedMeeting)"><Plus class="w-3.5 h-3.5" /> Join leg</button>
          </div>
          <div v-if="selectedMeeting.state === 'active'" class="flex flex-wrap items-center gap-2 text-xs">
            <span class="text-zinc-500">floor:</span>
            <span class="px-2 py-0.5 rounded-full border"
                  :class="selectedMeeting.floor?.mode === 'directed' ? 'border-sky-500/25 bg-sky-500/10 text-sky-300' : 'border-zinc-600/40 bg-zinc-700/20 text-zinc-300'">
              {{ selectedMeeting.floor?.mode === 'directed' ? `directed · ${selectedMeeting.floor.label}` : 'auto' }}
            </span>
            <button v-if="selectedMeeting.floor?.mode === 'directed'" class="btn btn-ghost text-xs" :disabled="meetingBusy" @click="setFloor(selectedMeeting, 'auto')">Release floor</button>
            <span class="text-zinc-600">{{ selectedMeeting.floor?.note }}</span>
          </div>
          <div class="space-y-1">
            <div v-for="p in selectedMeeting.participants || []" :key="p.id" class="flex items-center gap-2 text-xs text-zinc-400 flex-wrap">
              <span class="px-2 py-0.5 rounded-full border border-zinc-600/40 bg-zinc-700/20 text-zinc-300">{{ p.channel }}</span>
              <span class="text-zinc-200">{{ p.label }}</span>
              <span>{{ p.state }}</span>
              <span v-if="p.session_state">session: {{ p.session_state }}</span>
              <span v-if="p.last_error" class="text-amber-300">{{ p.last_error }}</span>
              <span v-if="selectedMeeting.floor?.participant_id === p.id" class="px-1.5 py-0.5 rounded-full border border-sky-500/25 bg-sky-500/10 text-sky-300">floor</span>
              <span v-if="(selectedMeeting.hand_queue?.entries || []).some((h: any) => h.participant_id === p.id)" class="px-1.5 py-0.5 rounded-full border border-amber-500/25 bg-amber-500/10 text-amber-300"><Hand class="w-3 h-3 inline" /> hand</span>
              <template v-if="selectedMeeting.state === 'active' && p.state === 'joined'">
                <button class="btn btn-ghost text-[11px] px-1.5"
                        :class="p.mix?.muted ? 'text-rose-300' : 'text-zinc-400'"
                        :disabled="meetingBusy" @click="setMix(selectedMeeting, p, 'muted', !p.mix?.muted)">{{ p.mix?.muted ? 'unmute' : 'mute' }}</button>
                <button class="btn btn-ghost text-[11px] px-1.5"
                        :class="p.mix?.deafened ? 'text-amber-300' : 'text-zinc-400'"
                        :disabled="meetingBusy" @click="setMix(selectedMeeting, p, 'deafened', !p.mix?.deafened)">{{ p.mix?.deafened ? 'undeafen' : 'deafen' }}</button>
                <button class="btn btn-ghost text-[11px] px-1.5"
                        :class="p.mix?.solo ? 'text-emerald-300' : 'text-zinc-400'"
                        :disabled="meetingBusy" @click="setMix(selectedMeeting, p, 'solo', !p.mix?.solo)">{{ p.mix?.solo ? 'unsolo' : 'solo' }}</button>
                <button v-if="selectedMeeting.floor?.participant_id !== p.id" class="btn btn-ghost text-[11px] px-1.5 text-sky-300" :disabled="meetingBusy" @click="setFloor(selectedMeeting, 'directed', p.id)">give floor</button>
                <button class="btn btn-ghost text-[11px] px-1.5 text-amber-300" :disabled="meetingBusy" @click="raiseHand(selectedMeeting, p)">raise hand</button>
              </template>
            </div>
          </div>
          <div v-if="selectedMeeting.state === 'active'" class="flex flex-wrap items-center gap-2 text-xs">
            <span class="text-zinc-500">speaking queue:</span>
            <span v-if="!selectedMeeting.hand_queue?.count" class="text-zinc-600">nobody waiting</span>
            <template v-for="h in selectedMeeting.hand_queue?.entries || []" :key="h.participant_id">
              <span class="px-2 py-0.5 rounded-full border border-amber-500/25 bg-amber-500/10 text-amber-300">
                #{{ h.position }} {{ h.label }}<span v-if="h.note"> · {{ h.note }}</span>
              </span>
              <button class="btn btn-ghost text-[11px] px-1.5 text-zinc-500" :disabled="meetingBusy" @click="lowerHand(selectedMeeting, { id: h.participant_id })">lower</button>
            </template>
            <button v-if="selectedMeeting.hand_queue?.count" class="btn btn-ghost text-xs text-sky-300" :disabled="meetingBusy" @click="callNextHand(selectedMeeting)"><Hand class="w-3.5 h-3.5" /> Call next (grants floor)</button>
          </div>
          <div class="space-y-2">
            <div class="flex items-center gap-2 text-xs text-zinc-500"><MessageSquare class="w-3.5 h-3.5" /> room chat <span v-if="selectedMeeting.counts?.chat_messages" class="text-zinc-600">{{ selectedMeeting.counts.chat_messages }} message(s)</span></div>
            <ol class="space-y-1 text-xs max-h-48 overflow-y-auto">
              <li v-for="m in meetingChat" :key="m.id" class="flex gap-2">
                <span class="w-28 shrink-0 truncate" :class="m.role === 'agent' ? 'text-fuchsia-300' : m.role === 'moderator' ? 'text-emerald-300' : 'text-sky-300'">{{ m.author }}</span>
                <span class="text-zinc-300">{{ m.text }}</span>
              </li>
            </ol>
            <div v-if="selectedMeeting.state === 'active'" class="flex flex-wrap items-center gap-2 text-xs">
              <label class="text-zinc-500">as
                <select v-model="chatForm.participant_id" class="input input-xs w-40">
                  <option value="">moderator</option>
                  <option v-for="p in (selectedMeeting.participants || []).filter((p: any) => p.state === 'joined')" :key="p.id" :value="p.id">{{ p.label }}</option>
                </select>
              </label>
              <label v-if="!chatForm.participant_id" class="text-zinc-500">name <input v-model="chatForm.author" class="input input-xs w-24" placeholder="moderator" /></label>
              <input v-model="chatForm.text" class="input input-xs w-64" placeholder="say something to the room…" @keyup.enter="postChat(selectedMeeting)" />
              <label class="text-zinc-500 flex items-center gap-1"><input type="checkbox" v-model="chatForm.ask_agent" class="checkbox checkbox-xs" /> ask the agent</label>
              <button class="btn btn-ghost text-xs" :disabled="chatBusy || !chatForm.text" @click="postChat(selectedMeeting)"><Send class="w-3.5 h-3.5" /> Send</button>
            </div>
            <p class="text-zinc-600">chat is the one channel muting never gates - a muted member can still type. ask_agent answers ON the member's leg (chat + the leg's transcript).</p>
          </div>
          <ol class="space-y-1.5 text-xs">
            <li v-for="(l, i) in selectedMeeting.transcript || []" :key="i" class="flex gap-3">
              <span class="text-zinc-500 w-36 shrink-0">{{ (l.at || '').replace('T', ' ').slice(0, 19) }}</span>
              <span class="w-40 shrink-0 truncate" :class="l.side === 'agent' ? 'text-fuchsia-300' : 'text-sky-300'">{{ l.speaker }}</span>
              <span class="text-zinc-300">{{ l.text }}</span>
            </li>
          </ol>
          <p v-for="n in selectedMeeting.notes || []" :key="n" class="text-zinc-600">{{ n }}</p>
        </div>
      </section>

      <section class="space-y-3">
        <h2 class="text-sm font-semibold text-zinc-300 uppercase tracking-wide flex items-center gap-2">
          <Megaphone class="w-4 h-4 text-amber-400" /> Outbound campaigns ({{ campaigns.length }})
          <span class="text-xs text-zinc-500 normal-case font-normal">dial a list through an agent · retry schedules · answering machine detection · honest skips</span>
        </h2>
        <div class="flex flex-wrap items-end gap-2">
          <label class="text-xs text-zinc-500">name <input v-model="campaignForm.name" class="input input-xs w-40" placeholder="Renewal reminders" /></label>
          <label class="text-xs text-zinc-500">agent
            <select v-model="campaignForm.agent_id" class="input input-xs w-44">
              <option value="">- pick an agent -</option>
              <option v-for="a in agents" :key="a.id" :value="a.id">{{ a.name }}</option>
            </select>
          </label>
          <label class="text-xs text-zinc-500">telnyx endpoint
            <select v-model="campaignForm.endpoint_id" class="input input-xs w-44">
              <option value="">- none (dials skipped honestly) -</option>
              <option v-for="e in voiceEndpoints" :key="e.id" :value="e.id">{{ e.name }}</option>
            </select>
          </label>
          <label class="text-xs text-zinc-500">targets (address, name per line)
            <textarea v-model="campaignForm.targets" class="input input-xs w-72 h-16 font-mono" placeholder="+15551234567, Alice\nsip:desk@pbx.example.com, Front desk" /></label>
          <label class="text-xs text-zinc-500">max attempts
            <select v-model.number="campaignForm.max_attempts" class="input input-xs w-16">
              <option v-for="n in [1, 2, 3, 4, 5]" :key="n" :value="n">{{ n }}</option>
            </select>
          </label>
          <label class="text-xs text-zinc-500">retry after (min)
            <input v-model="campaignForm.delays" class="input input-xs w-36" placeholder="15, 60, 1440" /></label>
          <label class="text-xs text-zinc-500">AMD
            <select v-model="campaignForm.amd_mode" class="input input-xs w-28">
              <option value="disabled">disabled</option>
              <option value="detect">detect</option>
              <option value="greeting_end">greeting_end</option>
            </select>
          </label>
          <label class="text-xs text-zinc-500">on machine
            <select v-model="campaignForm.amd_on_machine" class="input input-xs w-32">
              <option value="hangup">hangup</option>
              <option value="continue">continue</option>
              <option value="voicemail_drop">voicemail_drop</option>
            </select>
          </label>
          <label v-if="campaignForm.amd_on_machine === 'voicemail_drop'" class="text-xs text-zinc-500">drop message
            <input v-model="campaignForm.amd_message" class="input input-xs w-64" placeholder="Hi, calling about your renewal - we'll try again tomorrow." /></label>
          <button class="btn btn-ghost text-xs" :disabled="meetingBusy || !campaignForm.name || !campaignForm.agent_id || !campaignForm.targets" @click="createCampaign"><Plus class="w-3.5 h-3.5" /> New campaign</button>
        </div>
        <p v-if="!campaigns.length" class="text-sm text-zinc-500">No campaigns yet - create one, start it (real dials through the endpoint's credentials, or honest skips), and watch answered calls open sessions bound to the agent.</p>
        <div v-for="c in campaigns" :key="c.id" class="rounded-xl border border-zinc-800 bg-zinc-900/40 p-4 space-y-2">
          <div class="flex items-center justify-between gap-3 flex-wrap">
            <div class="flex items-center gap-2 text-sm">
              <span class="text-zinc-100">{{ c.name }}</span>
              <span class="text-xs px-2 py-0.5 rounded-full border border-amber-500/25 bg-amber-500/10 text-amber-300">{{ c.status }}</span>
              <span class="text-xs text-zinc-500">{{ c.progress?.total }} target(s) · {{ c.progress?.placed }} placed · {{ c.progress?.counts?.answered || 0 }} answered · {{ c.progress?.counts?.voicemail || 0 }} voicemail</span>
              <span v-if="c.progress?.retry?.eligible" class="text-xs text-sky-300">{{ c.progress.retry.due }}/{{ c.progress.retry.eligible }} retry due</span>
              <span v-if="c.agent_name" class="text-xs text-fuchsia-300">agent: {{ c.agent_name }}</span>
            </div>
            <div class="flex items-center gap-2">
              <button class="btn btn-ghost text-xs" :disabled="campaignBusy" @click="startCampaign(c)">Start</button>
              <button v-if="c.progress?.retry?.due" class="btn btn-ghost text-xs text-sky-300" :disabled="campaignBusy" @click="retryCampaign(c)">Retry due ({{ c.progress.retry.due }})</button>
              <button class="btn btn-ghost text-xs" @click="loadCampaignDetail(c)">Open</button>
            </div>
          </div>
        </div>
        <div v-if="selectedCampaign" class="rounded-xl border border-zinc-800 bg-zinc-900/60 p-4 space-y-2">
          <div class="flex items-center justify-between">
            <h3 class="text-sm text-zinc-200">{{ selectedCampaign.name }} · targets</h3>
            <div class="flex items-center gap-2">
              <button class="btn btn-ghost text-xs text-amber-300" :disabled="campaignBusy" @click="retryCampaign(selectedCampaign, true)">Retry force</button>
              <button class="btn btn-ghost" @click="selectedCampaign = null">Close</button>
            </div>
          </div>
          <p v-if="note" class="text-xs text-emerald-300">{{ note }}</p>
          <p v-if="selectedCampaign.progress?.retry" class="text-xs text-zinc-500">
            retry plan: {{ selectedCampaign.progress.retry.plan.retry_on.join('/') }} × up to {{ selectedCampaign.progress.retry.plan.max_attempts }} attempt(s) after [{{ selectedCampaign.progress.retry.plan.delays_minutes.join(', ') }}] min · {{ selectedCampaign.progress.retry.eligible }} eligible · {{ selectedCampaign.progress.retry.exhausted }} exhausted
          </p>
          <div v-for="t in selectedCampaign.targets || []" :key="t.id" class="flex items-center gap-2 text-xs text-zinc-400 flex-wrap">
            <span class="px-2 py-0.5 rounded-full border"
                  :class="t.status === 'answered' ? 'border-emerald-500/25 bg-emerald-500/10 text-emerald-300' : t.status === 'pending' ? 'border-zinc-600/40 bg-zinc-700/20 text-zinc-300' : t.status === 'voicemail' ? 'border-purple-500/25 bg-purple-500/10 text-purple-300' : 'border-amber-500/25 bg-amber-500/10 text-amber-300'">
              {{ t.status }}
            </span>
            <span class="text-zinc-200">{{ t.name || t.address }}</span>
            <span class="font-mono">{{ t.address }}</span>
            <span class="text-zinc-500">attempt {{ t.attempts }}</span>
            <span v-if="t.session_id" class="font-mono text-zinc-500">{{ t.session_id.slice(0, 8) }}</span>
            <span v-if="t.retry_at" class="text-sky-300">retry at {{ t.retry_at.replace('T', ' ').slice(5, 16) }}</span>
            <span v-if="t.amd" class="text-purple-300">amd: {{ t.amd.result }}</span>
            <span v-if="t.last_error" class="text-amber-300 truncate max-w-64">{{ t.last_error }}</span>
            <button v-if="t.status === 'pending'" class="btn btn-ghost text-xs" @click="simulateAnswer(selectedCampaign, t)">Simulate answer</button>
            <button v-if="t.status === 'pending' && selectedCampaign.config?.amd?.mode !== 'disabled'" class="btn btn-ghost text-xs text-purple-300" @click="simulateMachine(selectedCampaign, t)">Simulate machine</button>
            <button v-if="t.status === 'pending' && selectedCampaign.config?.amd?.mode === 'greeting_end'" class="btn btn-ghost text-xs text-fuchsia-300" @click="simulateMachine(selectedCampaign, t, 'greeting_end')">Simulate greeting_end</button>
            <span v-if="t.voicemail_drop" class="text-fuchsia-300 truncate max-w-72">dropped: "{{ t.voicemail_drop.message }}"</span>
          </div>
        </div>
      </section>

      <section class="space-y-3">
        <h2 class="text-sm font-semibold text-zinc-300 uppercase tracking-wide flex items-center gap-2">
          <Hourglass class="w-4 h-4 text-cyan-400" /> Channel queues ({{ queues.length }})
          <span class="text-xs text-zinc-500 normal-case font-normal">queueing &amp; waiting on the channel side · held calls · FIFO · seat into a meeting on the SAME call</span>
        </h2>
        <div class="flex flex-wrap items-end gap-2">
          <label class="text-xs text-zinc-500">name <input v-model="queueForm.name" class="input input-xs w-40" placeholder="Support line" /></label>
          <label class="text-xs text-zinc-500">destination room
            <select v-model="queueForm.meeting_id" class="input input-xs w-48">
              <option value="">- none (seat releases the call) -</option>
              <option v-for="m in meetings.filter((m: any) => m.state === 'active')" :key="m.id" :value="m.id">{{ m.title || 'room' }}</option>
            </select>
          </label>
          <button class="btn btn-ghost text-xs" :disabled="queueBusy || !queueForm.name" @click="createQueue"><Plus class="w-3.5 h-3.5" /> New queue</button>
        </div>
        <p v-if="!queues.length" class="text-sm text-zinc-500">No queues yet - a queue holds live calls in the line (session state on_hold), derives positions and wait times, and seats the head into a destination room.</p>
        <div v-for="q in queues" :key="q.id" class="rounded-xl border border-zinc-800 bg-zinc-900/40 p-4 space-y-2">
          <div class="flex items-center justify-between gap-3 flex-wrap">
            <div class="flex items-center gap-2 text-sm">
              <span class="text-zinc-100">{{ q.name }}</span>
              <span class="text-xs px-2 py-0.5 rounded-full border" :class="q.state === 'open' ? 'border-emerald-500/25 bg-emerald-500/10 text-emerald-300' : 'border-rose-500/25 bg-rose-500/10 text-rose-300'">{{ q.state }}</span>
              <span class="text-xs text-cyan-300">{{ q.depth?.waiting }} waiting</span>
              <span v-if="q.depth?.longest_wait_seconds !== null" class="text-xs text-zinc-500">longest {{ q.depth?.longest_wait_seconds }}s / {{ q.config?.max_wait_seconds }}s SLA</span>
              <span v-if="q.meeting_name" class="text-xs text-sky-300">→ {{ q.meeting_name }}</span>
            </div>
            <div class="flex items-center gap-2">
              <button class="btn btn-ghost text-xs" :disabled="queueBusy" @click="seatQueueNext(q)">Seat next</button>
              <button class="btn btn-ghost text-xs" :disabled="queueBusy" @click="toggleQueueState(q)">{{ q.state === 'open' ? 'Close' : 'Open' }}</button>
              <button class="btn btn-ghost text-xs" @click="openQueue(q)">Open</button>
            </div>
          </div>
        </div>
        <div v-if="selectedQueue" class="rounded-xl border border-zinc-800 bg-zinc-900/60 p-4 space-y-2">
          <div class="flex items-center justify-between">
            <h3 class="text-sm text-zinc-200">{{ selectedQueue.name }} · the line</h3>
            <button class="btn btn-ghost" @click="selectedQueue = null">Close</button>
          </div>
          <div class="flex flex-wrap items-center gap-2 text-xs">
            <input v-model="queueEntryForm.session_id" class="input input-xs w-72 font-mono" placeholder="live session id to enqueue (state in_progress)" />
            <button class="btn btn-ghost text-xs" :disabled="queueBusy || !queueEntryForm.session_id" @click="enqueueSession(selectedQueue)">Enqueue</button>
          </div>
          <div v-for="e in selectedQueue.entries || []" :key="e.id" class="flex items-center gap-2 text-xs text-zinc-400 flex-wrap">
            <span class="px-2 py-0.5 rounded-full border" :class="e.status === 'waiting' ? 'border-cyan-500/25 bg-cyan-500/10 text-cyan-300' : e.status === 'seated' ? 'border-emerald-500/25 bg-emerald-500/10 text-emerald-300' : 'border-zinc-600/40 bg-zinc-700/20 text-zinc-300'">{{ e.status }}</span>
            <span v-if="e.position" class="text-cyan-300">#{{ e.position }}</span>
            <span class="text-zinc-200">{{ e.label }}</span>
            <span v-if="e.session_id" class="font-mono text-zinc-500">{{ e.session_id.slice(0, 8) }}</span>
            <span v-if="e.waited_seconds !== null">{{ e.waited_seconds }}s</span>
            <span v-if="e.expired" class="text-amber-300">SLA breached</span>
            <span v-if="e.abandoned" class="text-rose-300">abandoned (caller hung up)</span>
            <button v-if="e.status === 'waiting'" class="btn btn-ghost text-[11px] px-1.5" :disabled="queueBusy" @click="queueLeave(selectedQueue, e)">Release</button>
          </div>
          <p v-for="n in selectedQueue.notes || []" :key="n" class="text-zinc-600">{{ n }}</p>
        </div>
      </section>

      <section class="space-y-3">
        <h2 class="text-sm font-semibold text-zinc-300 uppercase tracking-wide flex items-center gap-2">
          <Phone class="w-4 h-4 text-sky-400" /> Voice sessions
          <span class="text-xs text-zinc-500 normal-case font-normal">call state machine · barge-in · ASR/TTS turns</span>
        </h2>
        <p v-if="!sessions.length" class="text-sm text-zinc-500">No calls yet - open one via <code class="font-mono">POST /api/v1/voice/sessions</code> (the Twilio status callback adapter rides <code class="font-mono">/voice/webhooks/twilio/&#123;id&#125;</code>).</p>
        <div v-if="liveSessions.length" class="space-y-2">
          <div v-for="s in liveSessions" :key="s.id" class="rounded-xl border border-zinc-800 bg-zinc-900/40 p-4">
            <div class="flex items-center justify-between gap-3 flex-wrap">
              <div class="flex items-center gap-2 text-sm">
                <span class="text-xs px-2 py-0.5 rounded-full border" :class="stateChip[s.state]">{{ s.state }}</span>
                <span class="text-zinc-300">{{ s.direction }}</span>
                <span class="text-zinc-500">{{ s.from }} → {{ s.to }}</span>
                <span v-if="s.agent" class="text-xs px-2 py-0.5 rounded-full border border-fuchsia-500/25 bg-fuchsia-500/10 text-fuchsia-300">agent: {{ s.agent.voice_agent_name }}</span>
                <span v-if="s.active_tts" class="text-xs text-emerald-300 flex items-center gap-1"><Mic class="w-3 h-3" /> speaking</span>
                <span v-if="s.barge_in_count" class="text-xs text-amber-300 flex items-center gap-1"><Ear class="w-3 h-3" /> {{ s.barge_in_count }} barge-in</span>
              </div>
              <div class="flex items-center gap-2">
                <button class="btn btn-ghost text-xs" @click="bargeIn(s)">Barge-in</button>
                <button class="btn btn-ghost text-xs" @click="applyEvent(s, 'hold')">Hold</button>
                <button class="btn btn-ghost text-xs" @click="applyEvent(s, 'unhold')">Unhold</button>
                <button class="btn btn-ghost text-rose-300 text-xs" @click="applyEvent(s, 'hangup')">Hangup</button>
                <button class="btn btn-ghost text-xs" @click="openSession(s)"><PlayCircle class="w-3.5 h-3.5" /> Timeline</button>
              </div>
            </div>
          </div>
        </div>
        <details v-if="endedSessions.length" class="text-sm">
          <summary class="cursor-pointer text-zinc-400 hover:text-zinc-200">Ended calls ({{ endedSessions.length }})</summary>
          <div class="mt-2 space-y-1">
            <div v-for="s in endedSessions" :key="s.id" class="flex items-center gap-3 text-xs text-zinc-400">
              <span class="px-2 py-0.5 rounded-full border border-rose-500/25 bg-rose-500/10 text-rose-300">ended</span>
              <span class="text-zinc-300">{{ s.end_reason || 'hangup' }}</span>
              <span>{{ s.direction }} {{ s.from }} → {{ s.to }}</span>
              <span v-if="s.duration_seconds !== null">{{ s.duration_seconds }}s</span>
              <span>{{ s.turn_count }} turns · {{ s.barge_in_count }} barge-in</span>
              <button class="btn btn-ghost" @click="openSession(s)">Timeline</button>
            </div>
          </div>
        </details>
        <div v-if="selected" class="rounded-xl border border-zinc-800 bg-zinc-900/60 p-4">
          <div class="flex items-center justify-between">
            <h3 class="text-sm text-zinc-200">Call timeline <span class="text-zinc-500 font-mono">{{ selected.id.slice(0, 8) }}</span></h3>
            <button class="btn btn-ghost" @click="selected = null">Close</button>
          </div>
          <ol class="mt-3 space-y-1.5 text-xs">
            <li v-for="e in selected.events || []" :key="e.id" class="flex gap-3">
              <span class="text-zinc-500 w-40 shrink-0">{{ (e.created_at || '').replace('T', ' ').slice(0, 19) }}</span>
              <span class="font-mono text-sky-300 w-32 shrink-0">{{ e.kind }}</span>
              <span class="text-zinc-400 font-mono truncate">{{ JSON.stringify(e.payload) }}</span>
            </li>
          </ol>
        </div>
      </section>
    </template>

    <div v-if="showCreate" class="fixed inset-0 bg-black/60 flex items-center justify-center z-50 p-4" @click.self="showCreate = false">
      <div class="rounded-xl border border-zinc-700 bg-zinc-900 p-6 w-full max-w-md space-y-4">
        <h3 class="text-lg font-medium text-zinc-100">New provider endpoint</h3>
        <label class="block text-sm text-zinc-400">Name
          <input v-model="form.name" class="input mt-1 w-full" placeholder="Support WhatsApp line" /></label>
        <label class="block text-sm text-zinc-400">Provider
          <select v-model="form.provider" class="input mt-1 w-full">
            <option v-for="a in adapters" :key="a.id" :value="a.id">{{ providerLabel(a.id) }} ({{ a.channel }})</option>
          </select>
        </label>
        <label class="block text-sm text-zinc-400">Handler workflow
          <select v-model="form.handler_workflow_id" class="input mt-1 w-full">
            <option value="">- none yet -</option>
            <option v-for="w in workflows" :key="w.id" :value="w.id">{{ w.name }}</option>
          </select>
        </label>
        <label v-if="primarySecret" class="block text-sm text-zinc-400">
          <span class="font-mono">{{ primarySecret }}</span> <span class="text-zinc-600">(verifies the provider's webhook)</span>
          <input v-model="form.secret" type="password" class="input mt-1 w-full" />
        </label>
        <label v-if="credentialKeys[0]" class="block text-sm text-zinc-400">
          <span class="font-mono">{{ credentialKeys[0] }}</span> <span class="text-zinc-600">(optional - delivers outbound)</span>
          <input v-model="form.credential" type="password" class="input mt-1 w-full" />
        </label>
        <div class="flex justify-end gap-2">
          <button class="btn btn-ghost" @click="showCreate = false">Cancel</button>
          <button class="btn btn-primary" :disabled="busy || !form.name || !primarySecret" @click="createEndpoint">Create</button>
        </div>
      </div>
    </div>
    <div v-if="showAgentCreate" class="fixed inset-0 bg-black/60 flex items-center justify-center z-50 p-4" @click.self="showAgentCreate = false">
      <div class="rounded-xl border border-zinc-700 bg-zinc-900 p-6 w-full max-w-lg space-y-4 max-h-[90vh] overflow-y-auto">
        <h3 class="text-lg font-medium text-zinc-100 flex items-center gap-2"><Wand2 class="w-4 h-4 text-fuchsia-400" /> New voice agent</h3>
        <label class="block text-sm text-zinc-400">Name
          <input v-model="agentForm.name" class="input mt-1 w-full" placeholder="Front Desk" /></label>
        <label class="block text-sm text-zinc-400">Greeting (spoken when the call is answered)
          <input v-model="agentForm.greeting_text" class="input mt-1 w-full" placeholder="Hello, you have reached the front desk." /></label>
        <div class="grid grid-cols-2 gap-3">
          <label class="block text-sm text-zinc-400">ASR engine
            <select v-model="agentForm.asr_provider" class="input mt-1 w-full">
              <option v-for="p in ASR_PROVIDERS" :key="p" :value="p">{{ p }}</option>
            </select>
          </label>
          <label class="block text-sm text-zinc-400">TTS provider
            <select v-model="agentForm.tts_provider" class="input mt-1 w-full">
              <option v-for="p in TTS_PROVIDERS" :key="p" :value="p">{{ p }}</option>
            </select>
          </label>
          <label class="block text-sm text-zinc-400">TTS voice
            <input v-model="agentForm.tts_voice" class="input mt-1 w-full" /></label>
          <label class="block text-sm text-zinc-400">Language
            <input v-model="agentForm.language" class="input mt-1 w-full" placeholder="en-US" /></label>
        </div>
        <label class="block text-sm text-zinc-400">System prompt (rides the handler envelope's metadata)
          <textarea v-model="agentForm.system_prompt" class="input mt-1 w-full" rows="2"
                    placeholder="You are the polite front desk agent." /></label>
        <label class="block text-sm text-zinc-400">Handler workflow
          <select v-model="agentForm.handler_workflow_id" class="input mt-1 w-full">
            <option value="">- scaffold one for me -</option>
            <option v-for="w in workflows" :key="w.id" :value="w.id">{{ w.name }}</option>
          </select>
        </label>
        <label class="block text-sm text-zinc-400">Knowledge dataset (every turn is grounded on its rows)
          <select v-model="agentForm.knowledge_dataset_id" class="input mt-1 w-full">
            <option value="">- none (the handler answers ungrounded) -</option>
            <option v-for="d in datasets" :key="d.id" :value="d.id">{{ d.name }} ({{ d.row_count }} rows)</option>
          </select>
        </label>
        <label v-if="!agentForm.handler_workflow_id" class="block text-sm text-zinc-400">Brain (the scaffolded handler's answerer)
          <select v-model="agentForm.brain" class="input mt-1 w-full">
            <option value="scaffold">echo - deterministic, fully offline</option>
            <option value="ai_agent">ai_agent - an LLM brain grounded on the SAME knowledge binding</option>
          </select>
        </label>
        <label v-if="agentForm.brain === 'ai_agent' && !agentForm.handler_workflow_id" class="block text-sm text-zinc-400">Brain model (optional - passed to the provider)
          <input v-model="agentForm.brain_model" class="input mt-1 w-full" placeholder="default chosen by the bridge" /></label>
        <label v-if="agentForm.brain === 'ai_agent' && !agentForm.handler_workflow_id" class="block text-sm text-zinc-400">LLM credential (REAL routing - openai, claude, deepseek, kimi, qwen, openrouter, ...)
          <select v-model="agentForm.llm_credential_id" class="input mt-1 w-full">
            <option value="">- none: the brain stays on the free sandbox bridge -</option>
            <option v-for="c in llmCredentials" :key="c.id" :value="c.id">{{ c.name }} ({{ credTypeLabel[c.type] || c.type }})</option>
          </select>
        </label>
        <label class="flex items-center gap-2 text-sm text-zinc-400">
          <input v-model="agentForm.barge_in" type="checkbox" class="accent-fuchsia-500" />
          the caller may barge-in over the greeting and turns
        </label>
        <div class="flex justify-end gap-2">
          <button class="btn btn-ghost" @click="showAgentCreate = false">Cancel</button>
          <button class="btn btn-primary" :disabled="agentBusy || !agentForm.name" @click="createAgent">Create agent</button>
        </div>
      </div>
    </div>
  </div>
</template>

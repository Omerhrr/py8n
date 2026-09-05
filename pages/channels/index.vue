<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { Loader2, Phone, Webhook, Copy, Plus, Send, Ban, PlayCircle, Mic, Ear, Bot, Wand2 } from 'lucide-vue-next'
import { useApi } from '~/composables/useApi'

// v69: the REAL adapter surface. A channel endpoint registers a provider
// connection (Meta Cloud API, Telegram Bot API, Discord) and turns its
// NATIVE webhook into interaction-layer ingests + outbound sends. Voice
// sessions are first-class calls: state machine, barge-in, ASR/TTS turns.
// v71: the matrix completes (Telnyx SMS, the any-gateway SMS contract,
// email inbound parse + SMTP) and VOICE AGENTS compose the primitives
// (greeting, ASR engine, TTS voice, barge-in, scaffolded handler) into
// one deployable phone persona.

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
}

const { api } = useApi()
const loading = ref(true)
const pageError = ref('')
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
})
const ASR_PROVIDERS = ['py8n_local', 'openai_whisper', 'deepgram', 'assemblyai']
const TTS_PROVIDERS = ['openai_tts', 'elevenlabs', 'piper_local', 'meta_mms']

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
    const [eps, ads, vss, wfs, ags, dss, se] = await Promise.all([
      api('/channels/endpoints'), api('/channels/adapters'),
      api('/voice/sessions'), api('/workflows?limit=200'), api('/voice/agents'),
      api('/datasets?limit=200'), api('/voice/speech/engines').catch(() => null),
    ])
    endpoints.value = eps.endpoints || []
    adapters.value = ads.adapters || []
    sessions.value = vss.sessions || []
    workflows.value = wfs.workflows || wfs || []
    agents.value = ags.agents || []
    datasets.value = dss.datasets || dss || []
    speechEngines.value = se
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
      }),
    })
    showAgentCreate.value = false
    agentForm.value = { name: '', greeting_text: '', asr_provider: agentForm.value.asr_provider,
      tts_provider: agentForm.value.tts_provider, tts_voice: 'alloy', language: 'en-US',
      barge_in: true, system_prompt: '', handler_workflow_id: '', scaffold_handler: true,
      knowledge_dataset_id: '' }
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
          persona, bound to a KNOWLEDGE DATASET so every call answers from your data; sessions
          inherit the agent's config and the v70 media transport transcribes through the agent's
          engine (local whisper.cpp / vosk / piper bridges when installed).
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
              <span v-if="a.knowledge" class="text-xs px-2 py-0.5 rounded-full border border-teal-500/25 bg-teal-500/10 text-teal-300">knowledge: {{ a.knowledge.dataset_name || a.knowledge.dataset_id }} · top {{ a.knowledge.top_k }}</span>
            </div>
            <button class="btn btn-ghost text-rose-300 text-xs" @click="removeAgent(a)"><Ban class="w-3.5 h-3.5" /></button>
          </div>
          <div class="text-xs text-zinc-500">
            greeting: <span class="text-zinc-300">{{ a.greeting_text || 'none - the call starts silent' }}</span>
            · handler: <span class="text-zinc-300">{{ a.handler_workflow_name || 'none' }}</span>
            <span v-if="a.system_prompt"> · persona: <span class="text-zinc-400">{{ a.system_prompt.slice(0, 60) }}{{ a.system_prompt.length > 60 ? '…' : '' }}</span></span>
          </div>
          <details class="text-xs">
            <summary class="cursor-pointer text-zinc-400 hover:text-zinc-200">Wiring (provider webhook + media stream + knowledge)</summary>
            <div class="mt-2 space-y-1">
              <p class="text-zinc-400">{{ a.wiring.inbound_webhook }}</p>
              <p class="text-zinc-400">{{ a.wiring.media_stream }}</p>
              <p class="text-amber-300/80">{{ a.wiring.asr_note }}</p>
              <p v-if="a.wiring.knowledge_note" class="text-teal-300/80">{{ a.wiring.knowledge_note }}</p>
            </div>
          </details>
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

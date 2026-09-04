<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { Loader2, Phone, Webhook, Copy, Plus, Send, Ban, PlayCircle, Mic, Ear } from 'lucide-vue-next'
import { useApi } from '~/composables/useApi'

// v69: the REAL adapter surface. A channel endpoint registers a provider
// connection (Meta Cloud API, Telegram Bot API, Discord) and turns its
// NATIVE webhook into interaction-layer ingests + outbound sends. Voice
// sessions are first-class calls: state machine, barge-in, ASR/TTS turns.

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
const showCreate = ref(false)
const busy = ref(false)
const form = ref({ name: '', provider: 'telegram_bot_api', handler_workflow_id: '', secret: '', credential: '' })
const preview = ref<any>(null)
const previewTo = ref('')
const previewText = ref('Hello from py8n!')

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
    const [eps, ads, vss, wfs] = await Promise.all([
      api('/channels/endpoints'), api('/channels/adapters'),
      api('/voice/sessions'), api('/workflows?limit=200'),
    ])
    endpoints.value = eps.endpoints || []
    adapters.value = ads.adapters || []
    sessions.value = vss.sessions || []
    workflows.value = wfs.workflows || wfs || []
  } catch (e: any) {
    pageError.value = e?.message || 'failed to load channels'
  } finally {
    loading.value = false
  }
}

function providerLabel(id: string) {
  return ({ meta_cloud_api: 'Meta Cloud API', telegram_bot_api: 'Telegram Bot API', discord_bot: 'Discord', telnyx_call_control: 'Telnyx (SIP + PSTN)' } as Record<string, string>)[id] || id
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
          Discord, Telnyx Call Control for SIP + PSTN voice - each webhook-native and verified
          with its own credentials, feeding the SAME conversation layer. Voice sessions are first-class
          calls with a state machine, barge-in, the ASR/TTS contract and the v70 media transport:
          a websocket media stream (base64 mulaw/linear16) that py8n decodes, VAD-segments into
          utterances and transcribes through pluggable ASR engines.
        </p>
      </div>
      <button class="btn btn-primary shrink-0 flex items-center gap-2" @click="showCreate = true">
        <Plus class="w-4 h-4" /> New endpoint
      </button>
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
  </div>
</template>

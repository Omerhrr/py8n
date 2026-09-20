<script setup lang="ts">
// v80: THE EVENT STREAM - py8n's real-time event system, in the app.
//
// Every real-time primitive emits here: calls start and end, callers
// wait and get seated, participants join rooms, tracks publish, archives
// land, texts arrive, callbacks schedule. This page is the live tail:
// the history (GET /events, filtered) plus the websocket stream
// (WS /events/stream) so the operator SEES the platform breathe. A
// workflow with an Event Trigger node reacts to any of these types -
// this page is also the wiring reference for what to subscribe to.
const { api, streamUrl } = useApi()

const events = ref<any[]>([])
const loading = ref(false)
const error = ref('')
const typePattern = ref('')
const source = ref('')
const live = ref(false)
const liveError = ref('')
const received = ref(0)

let ws: WebSocket | null = null

const SOURCES = ['', 'voice', 'queue', 'sms', 'meeting', 'video', 'recording',
  'media', 'campaign', 'user', 'system']

async function loadHistory() {
  loading.value = true
  error.value = ''
  try {
    const params = new URLSearchParams()
    if (typePattern.value.trim()) params.set('type', typePattern.value.trim())
    if (source.value) params.set('source', source.value)
    params.set('limit', '100')
    const res = await api.get(`/events?${params.toString()}`)
    events.value = res.events || []
  } catch (e: any) {
    error.value = e?.message || String(e)
  } finally {
    loading.value = false
  }
}

function connect() {
  if (ws) return
  liveError.value = ''
  try {
    ws = new WebSocket(streamUrl('/api/v1/events/stream'))
  } catch (e: any) {
    liveError.value = e?.message || String(e)
    ws = null
    return
  }
  ws.onopen = () => { live.value = true }
  ws.onmessage = (m) => {
    try {
      const frame = JSON.parse(m.data)
      if (frame.event !== 'system_event') return
      received.value++
      events.value = [frame, ...events.value].slice(0, 200)
    } catch { /* a bad frame is skipped, never fatal */ }
  }
  ws.onerror = () => { liveError.value = 'the stream dropped - reconnect when ready' }
  ws.onclose = () => { live.value = false; ws = null }
}

function disconnect() {
  try { ws?.close() } catch {}
  ws = null
  live.value = false
}

onBeforeUnmount(() => { disconnect() })

function dt(s?: string | null) {
  if (!s) return '-'
  try { return new Date(s).toLocaleTimeString() } catch { return s }
}

function pretty(p: any) {
  if (!p || !Object.keys(p).length) return ''
  try { return JSON.stringify(p) } catch { return String(p) }
}

const sourceColor: Record<string, string> = {
  voice: 'border-sky-500/25 bg-sky-500/10 text-sky-300',
  queue: 'border-amber-500/25 bg-amber-500/10 text-amber-300',
  sms: 'border-lime-500/25 bg-lime-500/10 text-lime-300',
  meeting: 'border-indigo-500/25 bg-indigo-500/10 text-indigo-300',
  video: 'border-fuchsia-500/25 bg-fuchsia-500/10 text-fuchsia-300',
  recording: 'border-rose-500/25 bg-rose-500/10 text-rose-300',
  media: 'border-cyan-500/25 bg-cyan-500/10 text-cyan-300',
  campaign: 'border-orange-500/25 bg-orange-500/10 text-orange-300',
  user: 'border-zinc-600/40 bg-zinc-700/20 text-zinc-300',
  system: 'border-emerald-500/25 bg-emerald-500/10 text-emerald-300',
}

onMounted(() => { loadHistory(); connect() })
</script>

<template>
  <div class="p-6 max-w-6xl mx-auto space-y-5">
    <header class="flex flex-wrap items-end justify-between gap-3">
      <div>
        <h1 class="text-xl font-semibold text-zinc-100">Events</h1>
        <p class="text-sm text-zinc-400 max-w-2xl mt-1">
          The real-time event system - one first-class primitive for everything the
          platform's live layers do. Workflows subscribe with an
          <span class="font-mono text-xs bg-zinc-800 text-zinc-300 px-1 rounded">Event Trigger</span>
          node (a type pattern like <span class="font-mono text-xs">queue.*</span>);
          this tail shows what is flowing right now.
        </p>
      </div>
      <div class="flex items-center gap-2">
        <span
          class="text-xs px-2 py-0.5 rounded-full flex items-center gap-1"
          :class="live ? 'bg-emerald-500/10 text-emerald-300' : 'bg-zinc-800 text-zinc-500'">
          <span class="w-1.5 h-1.5 rounded-full" :class="live ? 'bg-emerald-500 animate-pulse' : 'bg-zinc-500'"></span>
          {{ live ? 'live' : 'offline' }}
        </span>
        <button
          class="px-3 py-1.5 rounded-lg border text-xs transition hover:bg-zinc-900"
          :class="live ? 'border-rose-500/30 text-rose-400' : 'border-emerald-500/30 text-emerald-400'"
          @click="live ? disconnect() : connect()">
          {{ live ? 'Disconnect' : 'Go live' }}
        </button>
      </div>
    </header>

    <form class="flex flex-wrap items-center gap-2" @submit.prevent="loadHistory">
      <input
        v-model="typePattern" placeholder="type pattern (queue.* / participant.joined)" maxlength="80"
        class="px-3 py-2 rounded-lg border border-zinc-700 bg-zinc-900 text-zinc-100 text-sm w-72 placeholder-zinc-500 outline-none transition focus:border-indigo-500/60">
      <select
        v-model="source"
        class="px-3 py-2 rounded-lg border border-zinc-700 bg-zinc-900 text-zinc-100 text-sm outline-none transition focus:border-indigo-500/60">
        <option v-for="s in SOURCES" :key="s" :value="s">{{ s || 'any source' }}</option>
      </select>
      <button
        type="submit" :disabled="loading"
        class="px-4 py-2 rounded-lg bg-indigo-600 text-white text-sm font-medium transition hover:bg-indigo-500 disabled:opacity-50">
        {{ loading ? 'loading…' : 'Load history' }}
      </button>
      <span v-if="received" class="text-xs text-zinc-500">{{ received }} live event(s) this session</span>
    </form>

    <p v-if="error" class="rounded-lg bg-rose-500/10 border border-rose-500/25 text-rose-300 px-4 py-2 text-sm">{{ error }}</p>
    <p v-if="liveError" class="rounded-lg bg-amber-500/10 border border-amber-500/25 text-amber-300 px-4 py-2 text-sm">{{ liveError }}</p>

    <section class="space-y-2">
      <p v-if="!events.length" class="text-sm text-zinc-500">
        Nothing here yet - make a call, wait in a line, join a room, record something,
        or emit an event of your own via POST /events.
      </p>
      <div
        v-for="e in events" :key="e.id"
        class="rounded-xl border border-zinc-800 bg-zinc-900/60 p-3">
        <div class="flex flex-wrap items-center gap-2">
          <span
            class="text-xs px-2 py-0.5 rounded-full border font-medium"
            :class="sourceColor[e.source] || 'border-zinc-600/40 bg-zinc-700/20 text-zinc-300'">
            {{ e.source }}
          </span>
          <span class="font-mono text-sm font-semibold text-zinc-100">{{ e.type }}</span>
          <span v-if="e.actor" class="text-xs text-zinc-400">by {{ e.actor }}</span>
          <span v-if="e.target_type" class="text-xs text-zinc-500">
            → {{ e.target_type }}<span v-if="e.target_id"> {{ e.target_id.slice(0, 8) }}</span>
          </span>
          <span class="ml-auto text-xs text-zinc-500">{{ dt(e.created_at) }}</span>
        </div>
        <div class="mt-1 flex flex-wrap gap-3 text-xs text-zinc-500">
          <span v-if="e.correlation_id" class="font-mono">corr {{ e.correlation_id.slice(0, 12) }}</span>
          <span v-if="e.session_id" class="font-mono">call {{ e.session_id.slice(0, 12) }}</span>
        </div>
        <pre v-if="pretty(e.payload)" class="mt-1 text-xs text-zinc-400 bg-zinc-950/60 border border-zinc-800 rounded-lg p-2 overflow-x-auto whitespace-pre-wrap break-all">{{ pretty(e.payload) }}</pre>
      </div>
    </section>
  </div>
</template>

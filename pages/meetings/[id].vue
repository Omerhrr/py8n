<script setup lang="ts">
// v79: THE BROWSER MEETING CLIENT - the room, in the app itself.
//
// A participant opens this page and joins the room as a WEB LEG: the mic
// streams linear16 audio over the session's media websocket (the v70
// transport - VAD, ASR, the agent's turns, barge-in all run unchanged),
// the platform pushes chat / queue positions / announcement audio back
// over the SAME socket (the v77 push hub), and the camera is a first-class
// v78 citizen: the track is registered, the signaling frames relay
// through the media websocket, and the pixels ride the browsers' WebRTC
// stacks peer-to-peer - py8n never carries the video itself.
//
// Everything here speaks ONLY documented endpoints + the documented
// websocket dialect; there is no side door and nothing fake.

const route = useRoute()
const meetingId = computed(() => String(route.params.id || ''))
const { api, mediaUrl, download } = useApi()

// ------------------------------------------------------------------ state
const loading = ref(true)
const error = ref('')
const meeting = ref<any>(null)
const joinLabel = ref('')
const joining = ref(false)
const me = ref<{ participantId: string; sessionId: string; label: string } | null>(null)
const wsOpen = ref(false)
const wsState = ref('')

const chat = ref<any[]>([])
const chatInput = ref('')
const askAgent = ref(false)
const queueBanner = ref<any>(null)
const lastTurn = ref<any>(null)
const recordings = ref<any[]>([])
const recordingBusy = ref(false)

// media + video internals (not rendered directly)
let ws: WebSocket | null = null
let audioCtx: AudioContext | null = null
let micSource: MediaStreamAudioSourceNode | null = null
let micProcessor: ScriptProcessorNode | null = null
let micGain: GainNode | null = null
let micStream: MediaStream | null = null
const micOn = ref(false)
const camOn = ref(false)
const screenOn = ref(false)
let localStream: MediaStream | null = null
let localTrackId = ''
const localVideo = ref<HTMLVideoElement | null>(null)
const remoteVideos = ref<{ pid: string; label: string; kind: string; stream: MediaStream }[]>([])
const peers = new Map<string, RTCPeerConnection>()
const pendingCandidates = new Map<string, any[]>()
let pollTimer: any = null

// ------------------------------------------------------------- lifecycle
onMounted(async () => {
  await refresh(true)
  const saved = sessionStorage.getItem(storeKey())
  if (saved && meeting.value?.state === 'active') {
    try { me.value = JSON.parse(saved) } catch { me.value = null }
  }
  if (me.value) await connectMedia()
  pollTimer = setInterval(() => refresh(false), 5000)
})

onBeforeUnmount(() => {
  if (pollTimer) clearInterval(pollTimer)
  teardownMedia()
  teardownVideo()
  try { ws?.close() } catch {}
})

function storeKey() { return `py8n-meeting-${meetingId.value}` }

// ------------------------------------------------------------------ data
async function refresh(full: boolean) {
  try {
    meeting.value = await api(`/voice/meetings/${meetingId.value}`)
    if (me.value) {
      const msgs = await api(`/voice/meetings/${meetingId.value}/chat?limit=100`).catch(() => ({ messages: [] }))
      if (!chat.value.length || full) chat.value = msgs.messages || []
      recordings.value = (await api(`/voice/meetings/${meetingId.value}/recordings`).catch(() => ({ recordings: [] }))).recordings || []
    }
    error.value = ''
  } catch (e: any) {
    error.value = e?.message || String(e)
  } finally {
    loading.value = false
  }
}

async function join() {
  const label = joinLabel.value.trim()
  if (!label) return
  joining.value = true
  error.value = ''
  try {
    const out = await api(`/voice/meetings/${meetingId.value}/join`, {
      method: 'POST', body: { label, channel: 'web' },
    })
    me.value = { participantId: out.participant.id, sessionId: out.participant.session_id, label }
    sessionStorage.setItem(storeKey(), JSON.stringify(me.value))
    joinLabel.value = ''
    chat.value = []
    await connectMedia()
    await refresh(true)
  } catch (e: any) {
    error.value = e?.message || String(e)
  } finally {
    joining.value = false
  }
}

async function leave() {
  if (me.value) {
    try { await api(`/voice/sessions/${me.value.sessionId}/events`, { method: 'POST', body: { kind: 'hangup', payload: { reason: 'left the room' } } }) } catch {}
  }
  sessionStorage.removeItem(storeKey())
  teardownMedia(); teardownVideo()
  try { ws?.close() } catch {}
  ws = null
  me.value = null
  await refresh(true)
}

// ------------------------------------------------------- media websocket
function connectMedia(): Promise<void> {
  return new Promise((resolve) => {
    if (!me.value) return resolve()
    try { ws?.close() } catch {}
    ws = new WebSocket(mediaUrl(me.value.sessionId))
    ws.onopen = () => {
      wsOpen.value = true
      // the stream's dialect: linear16 16k mono from the browser mic
      ws?.send(JSON.stringify({
        event: 'start',
        start: {
          streamSid: `py8n-web-${Math.random().toString(36).slice(2, 10)}`,
          callSid: me.value?.sessionId || '',
          customParameters: { encoding: 'linear16', sample_rate: 16000 },
        },
      }))
      resolve()
    }
    ws.onmessage = (ev) => {
      let frame: any = null
      try { frame = JSON.parse(ev.data) } catch { return }
      handleFrame(frame)
    }
    ws.onclose = () => { wsOpen.value = false; wsState.value = '' }
    ws.onerror = () => { wsOpen.value = false }
  })
}

function handleFrame(f: any) {
  if (!f || typeof f !== 'object') return
  switch (f.event) {
    case 'connected':
      wsState.value = `connected v${f.version || ''} · asr: ${f.asr_engine || 'none'}${f.asr_engine_registered ? '' : ' (not registered in this process)'}`.trim()
      break
    case 'chat':
      if (f.message?.meeting_id === meetingId.value || f.message?.meeting_id === undefined) {
        chat.value.push(f.message)
      }
      break
    case 'queue_position':
      queueBanner.value = f
      break
    case 'audio':
      playWav(f.audio_b64)
      break
    case 'turn':
      lastTurn.value = f
      break
    case 'video_signal':
      handleSignal(String(f.from || ''), f.data).catch(() => {})
      break
    case 'video_track':
      // another leg published/unpublished - the publisher offers to us; the
      // picture itself refreshes through the room poll
      break
    default:
      break
  }
}

function sendSignal(toPid: string, data: any) {
  ws?.send(JSON.stringify({ event: 'video_signal', to: toPid, data }))
}

function sendMediaFrame(pcm: Int16Array) {
  if (!ws || ws.readyState !== WebSocket.OPEN) return
  ws.send(JSON.stringify({
    event: 'media',
    media: { payload: toB64(pcm), encoding: 'linear16', sample_rate: 16000, track: 'inbound' },
  }))
}

// -------------------------------------------------------------- the mic
async function toggleMic() {
  error.value = ''
  try {
    if (micOn.value) {
      teardownMedia()
      micOn.value = false
      return
    }
    micStream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true },
    })
    audioCtx = new AudioContext()
    micSource = audioCtx.createMediaStreamSource(micStream)
    micProcessor = audioCtx.createScriptProcessor(4096, 1, 1)
    micGain = audioCtx.createGain()
    micGain.gain.value = 0 // silent monitor tap - ScriptProcessor needs a destination
    micProcessor.onaudioprocess = (e) => {
      const f32 = e.inputBuffer.getChannelData(0)
      const pcm = downsampleToInt16(f32, audioCtx?.sampleRate || 48000, 16000)
      sendMediaFrame(pcm)
    }
    micSource.connect(micProcessor)
    micProcessor.connect(micGain)
    micGain.connect(audioCtx.destination)
    micOn.value = true
  } catch (e: any) {
    error.value = `mic: ${e?.message || e}`
  }
}

function downsampleToInt16(f32: Float32Array, from: number, to: number): Int16Array {
  const ratio = Math.max(1, from / to)
  const outLen = Math.floor(f32.length / ratio)
  const out = new Int16Array(outLen)
  for (let i = 0; i < outLen; i++) {
    const v = Math.max(-1, Math.min(1, f32[Math.floor(i * ratio)]))
    out[i] = v < 0 ? v * 0x8000 : v * 0x7fff
  }
  return out
}

function toB64(buf: Int16Array): string {
  const bytes = new Uint8Array(buf.buffer, buf.byteOffset, buf.byteLength)
  let bin = ''
  const step = 0x8000
  for (let i = 0; i < bytes.length; i += step) {
    bin += String.fromCharCode(...Array.from(bytes.subarray(i, i + step)))
  }
  return btoa(bin)
}

function playWav(b64: string) {
  try {
    const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0))
    const url = URL.createObjectURL(new Blob([bytes], { type: 'audio/wav' }))
    const audio = new Audio(url)
    audio.onended = () => URL.revokeObjectURL(url)
    audio.play().catch(() => {})
  } catch {}
}

function teardownMedia() {
  try { micProcessor?.disconnect() } catch {}
  try { micSource?.disconnect() } catch {}
  try { micGain?.disconnect() } catch {}
  micStream?.getTracks().forEach((t) => t.stop())
  micProcessor = null; micSource = null; micGain = null; micStream = null
  try { audioCtx?.close() } catch {}
  audioCtx = null
  micOn.value = false
}

// ------------------------------------------------------- first-class video
async function toggleCamera() {
  error.value = ''
  try {
    if (camOn.value || screenOn.value) {
      await unpublishLocal()
      return
    }
    localStream = await navigator.mediaDevices.getUserMedia({ video: true })
    await publishLocal('camera')
  } catch (e: any) {
    error.value = `camera: ${e?.message || e}`
  }
}

async function toggleScreen() {
  error.value = ''
  try {
    if (screenOn.value || camOn.value) {
      await unpublishLocal()
      return
    }
    localStream = await (navigator.mediaDevices as any).getDisplayMedia({ video: true })
    await publishLocal('screen')
  } catch (e: any) {
    error.value = `screen: ${e?.message || e}`
  }
}

async function publishLocal(kind: 'camera' | 'screen') {
  if (!me.value || !localStream) return
  localTrackId = `trk-${Math.random().toString(36).slice(2, 12)}`
  await api(`/voice/meetings/${meetingId.value}/video/publish`, {
    method: 'POST',
    body: { participant_id: me.value.participantId, track_id: localTrackId, kind },
  })
  if (kind === 'camera') camOn.value = true; else screenOn.value = true
  if (localVideo.value) {
    localVideo.srcObject = localStream
    localVideo.muted = true
  }
  // THE PUBLISHER OFFERS to every other joined leg: one direction keeps
  // the offer/answer dance glare-free in this mesh
  const others = (meeting.value?.participants || []).filter(
    (p: any) => p.id !== me.value?.participantId && p.state === 'joined')
  for (const p of others) await offerTo(p.id, localStream)
}

async function offerTo(pid: string, stream: MediaStream) {
  const pc = ensurePc(pid)
  stream.getTracks().forEach((t) => {
    try { pc.addTrack(t, stream) } catch {}
  })
  const offer = await pc.createOffer()
  await pc.setLocalDescription(offer)
  sendSignal(pid, { type: 'offer', sdp: offer.sdp })
}

async function unpublishLocal() {
  if (me.value && localTrackId) {
    try {
      await api(`/voice/meetings/${meetingId.value}/video/unpublish`, {
        method: 'POST',
        body: { participant_id: me.value.participantId, track_id: localTrackId },
      })
    } catch {}
  }
  teardownVideo()
}

function ensurePc(pid: string): RTCPeerConnection {
  let pc = peers.get(pid)
  if (pc) return pc
  pc = new RTCPeerConnection({ iceServers: [] }) // host candidates; plug the owner's TURN here for NAT traversal
  pc.onicecandidate = (e) => {
    if (e.candidate) sendSignal(pid, { candidate: e.candidate.toJSON() })
  }
  pc.ontrack = (e) => {
    const label = (meeting.value?.participants || []).find((p: any) => p.id === pid)?.label || pid
    const kind = localStream?.getVideoTracks().some((t) => t.readyState === 'live') ? 'camera' : 'screen'
    const stream = e.streams[0] || new MediaStream([e.track])
    const hit = remoteVideos.value.find((v) => v.pid === pid)
    if (hit) { hit.stream = stream } else { remoteVideos.value.push({ pid, label, kind, stream }) }
  }
  peers.set(pid, pc)
  return pc
}

async function handleSignal(fromPid: string, data: any) {
  if (!data || typeof data !== 'object') return
  if (data.type === 'offer') {
    // we are the answerer: remote video comes in, nothing of ours goes out
    const pc = ensurePc(fromPid)
    await pc.setRemoteDescription({ type: 'offer', sdp: data.sdp })
    for (const c of pendingCandidates.get(fromPid) || []) {
      try { await pc.addIceCandidate(c) } catch {}
    }
    pendingCandidates.delete(fromPid)
    const answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)
    sendSignal(fromPid, { type: 'answer', sdp: answer.sdp })
  } else if (data.type === 'answer') {
    const pc = peers.get(fromPid)
    if (pc) await pc.setRemoteDescription({ type: 'answer', sdp: data.sdp })
  } else if (data.candidate) {
    const pc = peers.get(fromPid)
    if (pc && pc.remoteDescription) {
      try { await pc.addIceCandidate(data.candidate) } catch {}
    } else {
      const q = pendingCandidates.get(fromPid) || []
      q.push(data.candidate)
      pendingCandidates.set(fromPid, q)
    }
  }
}

function teardownVideo() {
  peers.forEach((pc) => { try { pc.close() } catch {} })
  peers.clear()
  pendingCandidates.clear()
  remoteVideos.value = []
  localStream?.getTracks().forEach((t) => t.stop())
  localStream = null
  localTrackId = ''
  camOn.value = false
  screenOn.value = false
  if (localVideo.value) localVideo.srcObject = null
}

// ------------------------------------------------------------------ chat
async function sendChat() {
  const text = chatInput.value.trim()
  if (!text || !me.value) return
  chatInput.value = ''
  try {
    const out = await api(`/voice/meetings/${meetingId.value}/chat`, {
      method: 'POST',
      body: { participant_id: me.value.participantId, author: me.value.label, text, ask_agent: askAgent.value },
    })
    chat.value.push(out.message)
  } catch (e: any) {
    error.value = e?.message || String(e)
  }
}

// ----------------------------------------------------------- hand / floor
async function toggleHand() {
  if (!me.value) return
  try {
    const raised = (meeting.value?.hand_queue?.entries || []).some(
      (e: any) => e.participant_id === me.value?.participantId)
    if (raised) {
      await api(`/voice/meetings/${meetingId.value}/hand/${me.value.participantId}`, { method: 'DELETE' })
    } else {
      await api(`/voice/meetings/${meetingId.value}/hand`, {
        method: 'POST', body: { participant_id: me.value.participantId, note: '' },
      })
    }
    await refresh(false)
  } catch (e: any) {
    error.value = e?.message || String(e)
  }
}

async function callNextHand() {
  try {
    await api(`/voice/meetings/${meetingId.value}/hand/next`, { method: 'POST' })
    await refresh(false)
  } catch (e: any) {
    error.value = e?.message || String(e)
  }
}

// ------------------------------------------------------------- recordings
async function startRecording() {
  recordingBusy.value = true
  error.value = ''
  try {
    await api(`/voice/meetings/${meetingId.value}/recordings`, {
      method: 'POST', body: { name: `${meeting.value?.title || 'room'} - ${new Date().toLocaleString()}` },
    })
    await refresh(true)
  } catch (e: any) {
    error.value = e?.message || String(e)
  } finally {
    recordingBusy.value = false
  }
}

async function stopRecording(rec: any) {
  recordingBusy.value = true
  error.value = ''
  try {
    await api(`/voice/meetings/${meetingId.value}/recordings/${rec.id}/stop`, { method: 'POST' })
    await refresh(true)
  } catch (e: any) {
    error.value = e?.message || String(e)
  } finally {
    recordingBusy.value = false
  }
}

function downloadArchive(rec: any, what: 'transcript' | 'chat') {
  const id = what === 'transcript' ? rec.artifacts?.transcript : rec.artifacts?.chat
  if (!id) return
  const ext = what === 'transcript' ? 'md' : 'json'
  download(`/artifacts/${id}/content`, `meeting-archive-${rec.id.slice(0, 8)}-${what}.${ext}`)
}

// ---------------------------------------------------------------- helpers
const myParticipant = computed(() =>
  (meeting.value?.participants || []).find((p: any) => p.id === me.value?.participantId))

const activeRecording = computed(() =>
  (recordings.value || []).find((r: any) => r.state === 'recording'))

const iHoldTheFloor = computed(() =>
  meeting.value?.floor?.participant_id === me.value?.participantId)

function isRaised(p: any) {
  const entries = meeting.value?.hand_queue?.entries || []
  const idx = entries.findIndex((e: any) => e.participant_id === p.id)
  return idx >= 0 ? idx + 1 : undefined
}

function dt(s?: string | null) {
  if (!s) return '-'
  try { return new Date(s).toLocaleTimeString() } catch { return s }
}
</script>

<template>
  <div class="max-w-6xl mx-auto px-4 py-8 space-y-5">
    <header class="flex flex-wrap items-center justify-between gap-3">
      <div>
        <NuxtLink to="/meetings" class="text-xs text-indigo-600 hover:underline">← all meetings</NuxtLink>
        <h1 class="text-2xl font-bold text-slate-800">
          {{ meeting?.title || 'Room' }}
          <span
            class="ml-2 text-xs px-2 py-0.5 rounded-full align-middle"
            :class="meeting?.state === 'active' ? 'bg-emerald-100 text-emerald-700' : 'bg-slate-100 text-slate-500'">
            {{ meeting?.state }}
          </span>
        </h1>
        <p v-if="wsState" class="text-xs text-slate-400">{{ wsState }}</p>
      </div>
      <div v-if="me && meeting?.state === 'active'" class="flex items-center gap-2">
        <button
          :class="micOn ? 'bg-emerald-600 text-white' : 'bg-white border border-slate-300 text-slate-700'"
          class="px-3 py-1.5 rounded-lg text-sm hover:opacity-90"
          @click="toggleMic">
          {{ micOn ? 'Mic on' : 'Mic off' }}
        </button>
        <button
          :class="camOn ? 'bg-emerald-600 text-white' : 'bg-white border border-slate-300 text-slate-700'"
          class="px-3 py-1.5 rounded-lg text-sm hover:opacity-90"
          @click="toggleCamera">
          {{ camOn ? 'Camera on' : 'Camera off' }}
        </button>
        <button
          :class="screenOn ? 'bg-emerald-600 text-white' : 'bg-white border border-slate-300 text-slate-700'"
          class="px-3 py-1.5 rounded-lg text-sm hover:opacity-90"
          @click="toggleScreen">
          {{ screenOn ? 'Sharing' : 'Share screen' }}
        </button>
        <button
          :class="isRaised(myParticipant) ? 'bg-amber-500 text-white' : 'bg-white border border-slate-300 text-slate-700'"
          class="px-3 py-1.5 rounded-lg text-sm hover:opacity-90"
          @click="toggleHand">
          {{ isRaised(myParticipant) ? 'Lower hand' : 'Raise hand' }}
        </button>
        <button class="px-3 py-1.5 rounded-lg bg-rose-600 text-white text-sm hover:bg-rose-700" @click="leave">
          Leave
        </button>
      </div>
    </header>

    <p v-if="error" class="rounded-lg bg-rose-50 border border-rose-200 text-rose-700 px-4 py-2 text-sm">{{ error }}</p>

    <div v-if="queueBanner" class="rounded-lg bg-amber-50 border border-amber-200 text-amber-800 px-4 py-2 text-sm">
      <b>{{ queueBanner.queue_name }}</b>: you are #{{ queueBanner.position }} of {{ queueBanner.depth }}
      (waited {{ queueBanner.waited_seconds }}s) - {{ queueBanner.text }}
      <button class="ml-2 underline text-xs" @click="queueBanner = null">dismiss</button>
    </div>

    <!-- join panel -->
    <section v-if="!me && meeting?.state === 'active'" class="rounded-xl border border-slate-200 bg-white p-6 shadow-sm max-w-md space-y-3">
      <h2 class="font-semibold text-slate-800">Join this room from the browser</h2>
      <p class="text-xs text-slate-500">
        You become a web leg: your mic streams over the session's media websocket
        (VAD + ASR + the agent's turns), the room's chat and announcements are pushed
        to you live, and your camera registers as a first-class video track
        (WebRTC peer-to-peer - py8n relays the signaling, never the pixels).
      </p>
      <form class="flex gap-2" @submit.prevent="join">
        <input
          v-model="joinLabel" maxlength="140" placeholder="your name in the room…"
          class="flex-1 px-3 py-2 rounded-lg border border-slate-300 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-400">
        <button
          type="submit" :disabled="joining || !joinLabel.trim()"
          class="px-4 py-2 rounded-lg bg-indigo-600 text-white text-sm font-medium hover:bg-indigo-700 disabled:opacity-50">
          {{ joining ? 'joining…' : 'Join' }}
        </button>
      </form>
    </section>

    <p v-if="loading && !meeting" class="text-sm text-slate-400">loading…</p>

    <div v-if="meeting" class="grid gap-5 lg:grid-cols-3">
      <!-- the video grid -->
      <section class="lg:col-span-2 space-y-4">
        <div class="grid gap-3 sm:grid-cols-2">
          <div v-if="camOn || screenOn" class="rounded-xl overflow-hidden bg-slate-900 border border-slate-700">
            <video ref="localVideo" autoplay playsinline class="w-full aspect-video object-cover" />
            <p class="text-xs text-slate-300 px-2 py-1">
              you ({{ screenOn ? 'screen' : 'camera' }}){{ iHoldTheFloor ? ' · holds the floor' : '' }}
            </p>
          </div>
          <div
            v-for="v in remoteVideos" :key="v.pid"
            class="rounded-xl overflow-hidden bg-slate-900 border border-slate-700">
            <video
              :ref="(el: any) => { if (el) { el.srcObject = v.stream; el.muted = true; el.play?.().catch(() => {}) } }"
              autoplay playsinline class="w-full aspect-video object-cover" />
            <p class="text-xs text-slate-300 px-2 py-1">{{ v.label }} ({{ v.kind }})</p>
          </div>
        </div>

        <!-- participants -->
        <div class="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
          <h2 class="text-sm font-semibold text-slate-700 mb-2">
            Participants ({{ meeting.counts?.joined ?? 0 }} joined)
            <span v-if="meeting.floor?.participant_id" class="ml-2 text-xs font-normal text-amber-600">
              floor: {{ meeting.floor?.label || meeting.floor.participant_id }}
            </span>
          </h2>
          <ul class="space-y-1 text-sm">
            <li
              v-for="p in meeting.participants" :key="p.id"
              class="flex items-center gap-2"
              :class="p.id === me?.participantId ? 'text-indigo-700 font-medium' : 'text-slate-600'">
              <span class="inline-block w-2 h-2 rounded-full"
                :class="p.state === 'joined' ? 'bg-emerald-500' : 'bg-slate-300'" />
              {{ p.label || p.address || p.id.slice(0, 8) }}
              <span class="text-xs text-slate-400">[{{ p.channel }} · {{ p.session_state || p.state }}]</span>
              <span v-if="p.id === meeting.floor?.participant_id" class="text-xs text-amber-600">floor</span>
              <span v-if="isRaised(p)" class="text-xs text-amber-500">✋ #{{ isRaised(p) }}</span>
              <span v-if="(meeting.video?.screen_holders || []).includes(p.label)" class="text-xs text-sky-600">screen</span>
              <button
                v-if="meeting.state === 'active' && (meeting.hand_queue?.entries || []).length"
                class="ml-auto text-xs px-2 py-0.5 rounded border border-slate-200 hover:bg-slate-50"
                @click="callNextHand">
                call next hand
              </button>
            </li>
          </ul>
        </div>

        <!-- live transcript (derived from the legs' timelines) -->
        <div class="rounded-xl border border-slate-200 bg-white p-4 shadow-sm max-h-72 overflow-y-auto">
          <h2 class="text-sm font-semibold text-slate-700 mb-2">Live transcript <span class="text-xs text-slate-400">(derived at read time)</span></h2>
          <p v-if="!meeting.transcript?.length" class="text-xs text-slate-400">nothing said yet.</p>
          <ul class="space-y-1 text-sm">
            <li v-for="(ln, i) in meeting.transcript || []" :key="i">
              <span class="text-xs text-slate-400">{{ dt(ln.at) }}</span>
              <b :class="ln.side === 'agent' ? 'text-violet-700' : 'text-slate-700'">{{ ln.speaker }}</b>:
              {{ ln.text }}
            </li>
          </ul>
        </div>
      </section>

      <!-- the side panel: chat + recordings -->
      <section class="space-y-4">
        <div class="rounded-xl border border-slate-200 bg-white p-4 shadow-sm flex flex-col max-h-96">
          <h2 class="text-sm font-semibold text-slate-700 mb-2">Room chat</h2>
          <div class="flex-1 overflow-y-auto space-y-2 mb-2">
            <p v-if="!chat.length" class="text-xs text-slate-400">
              no messages yet{{ me ? ' - say something' : ' (join to speak)' }}.
            </p>
            <div v-for="m in chat" :key="m.id" class="text-sm">
              <b class="text-slate-700">{{ m.author }}</b>
              <span class="text-xs text-slate-400">[{{ m.role }}] {{ dt(m.created_at) }}</span>
              <p class="text-slate-600">{{ m.text }}</p>
            </div>
          </div>
          <form v-if="me && meeting.state === 'active'" class="space-y-1" @submit.prevent="sendChat">
            <input
              v-model="chatInput" maxlength="2000" placeholder="type to the room…"
              class="w-full px-3 py-2 rounded-lg border border-slate-300 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-400">
            <label class="text-xs text-slate-500 flex items-center gap-1">
              <input v-model="askAgent" type="checkbox"> ask the room agent (it answers in chat and on your leg)
            </label>
          </form>
        </div>

        <div class="rounded-xl border border-slate-200 bg-white p-4 shadow-sm space-y-2">
          <div class="flex items-center justify-between">
            <h2 class="text-sm font-semibold text-slate-700">Recording</h2>
            <button
              v-if="meeting.state === 'active' && !activeRecording"
              :disabled="recordingBusy"
              class="px-3 py-1.5 rounded-lg bg-rose-600 text-white text-xs hover:bg-rose-700 disabled:opacity-50"
              @click="startRecording">
              ● Record
            </button>
            <button
              v-if="activeRecording"
              :disabled="recordingBusy"
              class="px-3 py-1.5 rounded-lg bg-slate-700 text-white text-xs hover:bg-slate-800 disabled:opacity-50"
              @click="stopRecording(activeRecording)">
              ■ Stop
            </button>
          </div>
          <p v-if="activeRecording" class="text-xs text-emerald-700">
            recording since {{ dt(activeRecording.started_at) }} - transcript + chat +
            web-leg audio are archived when it stops.
          </p>
          <div v-for="rec in recordings" :key="rec.id" class="text-xs text-slate-500 border-t border-slate-100 pt-1">
            <p class="truncate"><b>{{ rec.name }}</b> ({{ rec.state }})</p>
            <p>
              {{ rec.counts?.transcript_lines }} lines · {{ rec.counts?.chat_lines }} chat ·
              {{ rec.counts?.audio_legs }} audio leg(s)
              <button v-if="rec.artifacts?.transcript" class="underline ml-1" @click="downloadArchive(rec, 'transcript')">transcript</button>
              <button v-if="rec.artifacts?.chat" class="underline ml-1" @click="downloadArchive(rec, 'chat')">chat</button>
            </p>
          </div>
          <p v-if="!recordings.length" class="text-xs text-slate-400">
            no recordings yet - Record archives this room's words and web-leg audio.
          </p>
        </div>

        <div class="rounded-xl border border-slate-200 bg-slate-50 p-4 text-xs text-slate-500 space-y-1">
          <p><b>How this room works:</b> py8n owns the room, the transcript, the chat,
          the floor and the archives. Your browser carries the audio (media websocket)
          and the video (WebRTC peer-to-peer - the signaling relays through py8n, the
          pixels never touch it). Phone legs ride their carrier's media plane.</p>
        </div>
      </section>
    </div>
  </div>
</template>


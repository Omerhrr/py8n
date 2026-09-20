<script setup lang="ts">
// v79: the meetings shelf - the rooms py8n owns and the ARCHIVES they
// left behind. The operator console (channels page) manages the
// machinery; this is the participant's door: open a room, join it from
// the browser (mic + camera over the media websocket + WebRTC), and find
// every recording / transcription archive afterwards.
const { api, download } = useApi()

const meetings = ref<any[]>([])
const recordings = ref<any[]>([])
const loading = ref(true)
const error = ref('')
const creating = ref(false)
const title = ref('')

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [mts, recs] = await Promise.all([
      api.get('/voice/meetings?limit=50').catch(() => ({ meetings: [] })),
      api.get('/voice/recordings?limit=50').catch(() => ({ recordings: [] })),
    ])
    meetings.value = mts.meetings || []
    recordings.value = recs.recordings || []
  } catch (e: any) {
    error.value = e?.message || String(e)
  } finally {
    loading.value = false
  }
}

async function createRoom() {
  if (!title.value.trim()) return
  creating.value = true
  error.value = ''
  try {
    const m = await api.post('/voice/meetings', { title: title.value.trim() })
    title.value = ''
    navigateTo(`/meetings/${m.id}`)
  } catch (e: any) {
    error.value = e?.message || String(e)
  } finally {
    creating.value = false
  }
}

async function endMeeting(m: any) {
  error.value = ''
  try { await api.post(`/voice/meetings/${m.id}/end`) } catch (e: any) { error.value = e?.message || String(e) }
  await load()
}

function dt(s?: string | null) {
  if (!s) return '-'
  try { return new Date(s).toLocaleString() } catch { return s }
}

function downloadArchive(rec: any, what: 'transcript' | 'chat') {
  const id = what === 'transcript' ? rec.artifacts?.transcript : rec.artifacts?.chat
  if (!id) return
  const ext = what === 'transcript' ? 'md' : 'json'
  download(`/artifacts/${id}/content`, `meeting-archive-${rec.id.slice(0, 8)}-${what}.${ext}`)
}

onMounted(load)
</script>

<template>
  <div class="max-w-6xl mx-auto px-4 py-8 space-y-6">
    <header class="flex flex-wrap items-center justify-between gap-3">
      <div>
        <h1 class="text-2xl font-bold text-zinc-100">Meetings</h1>
        <p class="text-sm text-zinc-400">
          Join a room from the browser - mic, camera, screen, chat and the floor - and
          find every recording / transcription archive here afterwards.
        </p>
      </div>
      <form class="flex items-center gap-2" @submit.prevent="createRoom">
        <input
          v-model="title" placeholder="new room title…" maxlength="200"
          class="px-3 py-2 rounded-lg border border-zinc-700 bg-zinc-900 text-zinc-100 text-sm w-64 placeholder-zinc-500 outline-none transition focus:border-indigo-500/60">
        <button
          type="submit" :disabled="creating || !title.trim()"
          class="px-4 py-2 rounded-lg bg-indigo-600 text-white text-sm font-medium transition hover:bg-indigo-500 disabled:opacity-50">
          {{ creating ? 'creating…' : 'New room' }}
        </button>
      </form>
    </header>

    <p v-if="error" class="rounded-lg bg-rose-500/10 border border-rose-500/25 text-rose-300 px-4 py-2 text-sm">{{ error }}</p>

    <section class="space-y-3">
      <h2 class="text-sm font-semibold uppercase tracking-wide text-zinc-500">Rooms</h2>
      <p v-if="loading" class="text-sm text-zinc-500">loading…</p>
      <p v-else-if="!meetings.length" class="text-sm text-zinc-500">
        No rooms yet - create one above and open it to join from this browser.
      </p>
      <div v-else class="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        <div
          v-for="m in meetings" :key="m.id"
          class="rounded-xl border bg-zinc-900/60 p-4 flex flex-col gap-2"
          :class="m.state === 'active' ? 'border-emerald-500/30' : 'border-zinc-800 opacity-80'">
          <div class="flex items-center justify-between">
            <NuxtLink :to="`/meetings/${m.id}`" class="font-semibold text-zinc-100 hover:text-indigo-400 truncate">
              {{ m.title || '(untitled room)' }}
            </NuxtLink>
            <span
              class="text-xs px-2 py-0.5 rounded-full"
              :class="m.state === 'active' ? 'bg-emerald-500/10 text-emerald-300' : 'bg-zinc-800 text-zinc-500'">
              {{ m.state }}
            </span>
          </div>
          <p class="text-xs text-zinc-500">
            {{ m.counts?.joined ?? 0 }} joined · {{ m.counts?.participants ?? 0 }} legs
            <span v-if="m.counts?.chat_messages"> · {{ m.counts.chat_messages }} chat</span>
            <span v-if="m.video?.counts?.live_tracks"> · {{ m.video.counts.live_tracks }} live video</span>
          </p>
          <p class="text-xs text-zinc-500">created {{ dt(m.created_at) }}</p>
          <div class="mt-auto flex items-center gap-2 pt-2">
            <NuxtLink
              :to="`/meetings/${m.id}`"
              class="px-3 py-1.5 rounded-lg bg-indigo-600 text-white text-xs font-medium transition hover:bg-indigo-500">
              {{ m.state === 'active' ? 'Open room' : 'Open archive view' }}
            </NuxtLink>
            <button
              v-if="m.state === 'active'"
              class="px-3 py-1.5 rounded-lg border border-rose-500/30 text-rose-400 text-xs transition hover:bg-rose-500/10"
              @click="endMeeting(m)">
              End
            </button>
          </div>
        </div>
      </div>
    </section>

    <section class="space-y-3">
      <h2 class="text-sm font-semibold uppercase tracking-wide text-zinc-500">Recording & transcription archives</h2>
      <p v-if="!recordings.length" class="text-sm text-zinc-500">
        Nothing archived yet - open a room, press Record, and the transcript + chat +
        web-leg audio land here when the recording stops.
      </p>
      <div v-else class="space-y-2">
        <div
          v-for="rec in recordings" :key="rec.id"
          class="rounded-xl border border-zinc-800 bg-zinc-900/60 p-4 flex flex-wrap items-center gap-3">
          <div class="min-w-0 flex-1">
            <p class="font-medium text-zinc-100 truncate">{{ rec.name }}</p>
            <p class="text-xs text-zinc-500">
              {{ rec.state }} · {{ rec.counts?.transcript_lines }} transcript lines ·
              {{ rec.counts?.chat_lines }} chat · {{ rec.counts?.audio_legs }} audio leg(s)
              · started {{ dt(rec.started_at) }}
            </p>
          </div>
          <button
            v-if="rec.artifacts?.transcript"
            class="px-3 py-1.5 rounded-lg border border-zinc-700 text-zinc-300 text-xs transition hover:bg-zinc-800"
            @click="downloadArchive(rec, 'transcript')">
            Transcript .md
          </button>
          <button
            v-if="rec.artifacts?.chat"
            class="px-3 py-1.5 rounded-lg border border-zinc-700 text-zinc-300 text-xs transition hover:bg-zinc-800"
            @click="downloadArchive(rec, 'chat')">
            Chat .json
          </button>
          <span
            v-for="a in rec.artifacts?.audio?.filter((x: any) => x.captured) || []"
            :key="a.artifact_id"
            class="text-xs text-zinc-500">
            leg audio {{ Math.round((a.duration_ms || 0) / 1000) }}s
          </span>
        </div>
      </div>
    </section>
  </div>
</template>

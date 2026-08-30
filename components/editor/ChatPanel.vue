<script setup lang="ts">
// v25: editor chat panel — talks to a workflow's Chat Trigger.
// v26: sends via POST /chat/{id}/stream (SSE) and renders LIVE progress —
// one chip per node as it starts/finishes — then types out the reply.
// Each conversation owns a stable session_id (used by downstream agent
// nodes for per-session memory); "New conversation" rotates it.
import { nextTick, ref, watch } from 'vue'
import { MessageCircle, X, SendHorizontal, RotateCcw, Loader2, Check } from 'lucide-vue-next'

const props = defineProps<{
  workflowId: string
  open: boolean
  welcomeMessage?: string
}>()

const emit = defineEmits<{ (e: 'close'): void }>()

interface ChatMsg {
  role: 'user' | 'assistant'
  content: string
  ts: number
  error?: boolean
}
interface StepChip {
  nodeId: string
  name: string
  status: 'running' | 'success' | 'error'
  durationMs?: number
}

const messages = ref<ChatMsg[]>([])
const draft = ref('')
const sending = ref(false)
const steps = ref<StepChip[]>([])
const sessionId = ref(newSessionId())
let typeTimer: ReturnType<typeof setInterval> | null = null

function newSessionId(): string {
  try {
    return `chat-${crypto.randomUUID()}`
  } catch {
    return `chat-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`
  }
}

function startNewConversation() {
  if (typeTimer) { clearInterval(typeTimer); typeTimer = null }
  sessionId.value = newSessionId()
  messages.value = []
  steps.value = []
}

function streamUrl(): string {
  const config = useRuntimeConfig()
  const mode = (config.public.gatewayMode as string) || 'gateway'
  const apiPort = (config.public.apiPort as string) || '8000'
  const base = `/api/v1/chat/${props.workflowId}/stream`
  return mode === 'gateway' ? `${base}?XTransformPort=${apiPort}` : base
}

function extractReply(body: any): string {
  if (body == null) return ''
  if (typeof body === 'string') return body
  if (typeof body.reply === 'string') return body.reply
  if (typeof body.answer === 'string') return body.answer
  return JSON.stringify(body.output ?? body, null, 1)
}

async function scrollBottomSmooth() {
  await nextTick()
  const el = document.getElementById('py8n-chat-scroll')
  if (el) el.scrollTop = el.scrollHeight
}

// typewriter reveal of an assistant message that is already in the list
function typeOut(target: string) {
  const msg = messages.value[messages.value.length - 1]
  if (!msg) return
  let shown = 0
  if (typeTimer) clearInterval(typeTimer)
  typeTimer = setInterval(() => {
    shown = Math.min(target.length, shown + 3)
    msg.content = target.slice(0, shown)
    const el = document.getElementById('py8n-chat-scroll')
    if (el) el.scrollTop = el.scrollHeight
    if (shown >= target.length) {
      if (typeTimer) clearInterval(typeTimer)
      typeTimer = null
    }
  }, 18)
}

async function sendFallback(text: string) {
  // pre-stream behavior: one blocking POST, no live progress
  const { api } = useApi()
  const body = await api.post(`/chat/${props.workflowId}`, {
    message: text,
    session_id: sessionId.value,
  })
  messages.value.push({ role: 'assistant', content: extractReply(body) || '(empty reply)', ts: Date.now() })
}

function upsertChip(frame: any) {
  const id = frame.node_id || frame.node_name
  const existing = steps.value.find((s) => s.nodeId === id)
  if (frame.status === 'running') {
    if (!existing) {
      steps.value.push({ nodeId: id, name: frame.node_name || frame.node_id, status: 'running' })
    }
  } else if (existing) {
    existing.status = frame.status === 'error' ? 'error' : 'success'
    existing.durationMs = frame.duration_ms
  } else {
    steps.value.push({ nodeId: id, name: frame.node_name || frame.node_id, status: frame.status === 'error' ? 'error' : 'success', durationMs: frame.duration_ms })
  }
}

async function send() {
  const text = draft.value.trim()
  if (!text || sending.value) return
  draft.value = ''
  messages.value.push({ role: 'user', content: text, ts: Date.now() })
  sending.value = true
  steps.value = []
  await scrollBottomSmooth()

  try {
    const res = await fetch(streamUrl(), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: text, session_id: sessionId.value }),
    })
    const ctype = res.headers.get('content-type') || ''

    if (!res.ok || !ctype.includes('text/event-stream')) {
      // guards (404/409/422) answer with plain JSON before any stream
      let detail = `chat request failed (${res.status})`
      try {
        const j = await res.json()
        if (j?.detail) detail = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail)
      } catch { /* not JSON */ }
      messages.value.push({ role: 'assistant', content: String(detail).includes('inactive') ? 'Workflow is inactive — activate it ("Triggers on" toggle) to chat.' : `⚠ ${detail}`, ts: Date.now(), error: true })
      return
    }

    // read the SSE body incrementally
    const reader = res.body!.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    let terminal: { event: string; data: any } | null = null
    while (terminal === null) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      let idx: number
      while ((idx = buffer.indexOf('\n\n')) !== -1) {
        const block = buffer.slice(0, idx)
        buffer = buffer.slice(idx + 2)
        let ev = '', dataLine = ''
        for (const line of block.split('\n')) {
          if (line.startsWith('event: ')) ev = line.slice(7).trim()
          else if (line.startsWith('data: ')) dataLine = line.slice(6)
        }
        if (!ev) continue
        let data: any = null
        try { data = JSON.parse(dataLine) } catch { data = {} }
        if (ev === 'node') upsertChip(data)
        else if (ev === 'done' || ev === 'error' || ev === 'timeout') { terminal = { event: ev, data }; break }
        await scrollBottomSmooth()
      }
    }

    if (terminal?.event === 'done') {
      steps.value.forEach((s) => { if (s.status === 'running') s.status = 'success' })
      messages.value.push({ role: 'assistant', content: '', ts: Date.now() })
      typeOut(terminal.data.reply || '(empty reply)')
    } else if (terminal?.event === 'error') {
      messages.value.push({ role: 'assistant', content: `⚠ ${terminal.data.error}`, ts: Date.now(), error: true })
    } else if (terminal?.event === 'timeout') {
      messages.value.push({ role: 'assistant', content: `⚠ No reply within ${terminal.data.after_seconds}s — the workflow keeps running in the background.`, ts: Date.now(), error: true })
    } else {
      // stream ended without a terminal frame — fall back to the plain endpoint
      await sendFallback(text)
    }
  } catch (err: any) {
    try {
      await sendFallback(text)
    } catch (err2: any) {
      const detail = err2?.data?.detail || err2?.message || err?.message || 'chat request failed'
      messages.value.push({
        role: 'assistant',
        content: String(detail).includes('inactive') ? 'Workflow is inactive — activate it ("Triggers on" toggle) to chat.' : `⚠ ${detail}`,
        ts: Date.now(),
        error: true,
      })
    }
  } finally {
    sending.value = false
    await scrollBottomSmooth()
  }
}

watch(() => props.open, async (open) => {
  if (open) await scrollBottomSmooth()
})
</script>

<template>
  <Teleport to="body">
    <transition
      enter-active-class="transition duration-200 ease-out"
      enter-from-class="translate-x-8 opacity-0"
      leave-active-class="transition duration-150 ease-in"
      leave-to-class="translate-x-8 opacity-0"
    >
      <div
        v-if="open"
        class="fixed bottom-12 right-4 z-40 flex h-[520px] w-[360px] flex-col overflow-hidden rounded-2xl border border-zinc-800 bg-zinc-950/95 shadow-2xl shadow-black/60 backdrop-blur"
      >
        <!-- header -->
        <div class="flex items-center justify-between border-b border-zinc-800 px-3.5 py-2.5">
          <div class="flex items-center gap-2">
            <span class="flex h-6 w-6 items-center justify-center rounded-lg bg-emerald-500/15">
              <MessageCircle class="h-3.5 w-3.5 text-emerald-400" />
            </span>
            <div class="leading-tight">
              <div class="text-xs font-semibold text-zinc-100">Chat</div>
              <div class="font-mono text-[9px] text-zinc-500">{{ sessionId.slice(0, 18) }}…</div>
            </div>
          </div>
          <div class="flex items-center gap-1">
            <button
              class="rounded-md p-1.5 text-zinc-500 transition hover:bg-zinc-800 hover:text-zinc-300"
              title="New conversation (rotates session id — agent memory starts fresh)"
              @click="startNewConversation"
            >
              <RotateCcw class="h-3.5 w-3.5" />
            </button>
            <button
              class="rounded-md p-1.5 text-zinc-500 transition hover:bg-zinc-800 hover:text-zinc-300"
              title="Close chat"
              @click="emit('close')"
            >
              <X class="h-3.5 w-3.5" />
            </button>
          </div>
        </div>

        <!-- messages -->
        <div id="py8n-chat-scroll" class="flex-1 space-y-2.5 overflow-y-auto px-3.5 py-3">
          <div v-if="!messages.length && welcomeMessage" class="flex justify-start">
            <div class="max-w-[85%] whitespace-pre-wrap rounded-xl rounded-bl-sm border border-zinc-800 bg-zinc-900 px-3 py-2 text-xs text-zinc-300">
              {{ welcomeMessage }}
            </div>
          </div>
          <div
            v-for="(m, i) in messages"
            :key="i"
            class="flex"
            :class="m.role === 'user' ? 'justify-end' : 'justify-start'"
          >
            <div
              class="max-w-[85%] whitespace-pre-wrap rounded-xl px-3 py-2 text-xs"
              :class="m.role === 'user'
                ? 'rounded-br-sm bg-emerald-600 text-white'
                : m.error
                  ? 'rounded-bl-sm border border-amber-500/40 bg-amber-950/60 text-amber-200'
                  : 'rounded-bl-sm border border-zinc-800 bg-zinc-900 text-zinc-200'"
            >{{ m.content }}</div>
          </div>

          <!-- v26: live run progress — one chip per node -->
          <div v-if="steps.length" class="space-y-1.5 rounded-xl border border-zinc-800/80 bg-zinc-900/50 px-3 py-2">
            <div v-for="s in steps" :key="s.nodeId" class="flex items-center gap-2 text-[10px]">
              <Loader2 v-if="s.status === 'running'" class="h-3 w-3 shrink-0 animate-spin text-amber-400" />
              <span v-else class="flex h-3 w-3 shrink-0 items-center justify-center rounded-full"
                :class="s.status === 'success' ? 'bg-emerald-500/20' : 'bg-rose-500/20'">
                <Check v-if="s.status === 'success'" class="h-2 w-2 text-emerald-400" />
                <span v-else class="text-rose-400 leading-none">×</span>
              </span>
              <span class="truncate" :class="s.status === 'running' ? 'text-zinc-300' : 'text-zinc-500'">{{ s.name }}</span>
              <span v-if="s.durationMs != null" class="ml-auto shrink-0 font-mono text-zinc-600">{{ (s.durationMs / 1000).toFixed(2) }}s</span>
            </div>
          </div>
        </div>

        <!-- composer -->
        <div class="border-t border-zinc-800 p-2.5">
          <div class="flex items-end gap-2 rounded-xl border border-zinc-800 bg-zinc-900 px-2.5 py-2 focus-within:border-zinc-600">
            <textarea
              v-model="draft"
              rows="1"
              placeholder="Type a message…"
              class="max-h-24 flex-1 resize-none bg-transparent text-xs text-zinc-200 outline-none placeholder:text-zinc-600"
              @keydown.enter.exact.prevent="send"
            />
            <button
              class="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-emerald-600 text-white transition hover:bg-emerald-500 disabled:opacity-40"
              :disabled="sending || !draft.trim()"
              title="Send"
              @click="send"
            >
              <SendHorizontal class="h-3.5 w-3.5" />
            </button>
          </div>
          <div class="mt-1.5 px-0.5 text-[9px] text-zinc-600">
            live progress stream · reply typed out as it arrives
          </div>
        </div>
      </div>
    </transition>
  </Teleport>
</template>

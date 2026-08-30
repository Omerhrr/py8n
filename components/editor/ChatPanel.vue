<script setup lang="ts">
// v25: editor chat panel — talks to a workflow's Chat Trigger via
// POST /api/v1/chat/{workflow_id}. Each conversation owns a stable
// session_id (used by downstream agent nodes for per-session memory);
// "New conversation" rotates it so memory starts fresh.
import { nextTick, ref, watch } from 'vue'
import { MessageCircle, X, SendHorizontal, RotateCcw, Loader2, AlertTriangle } from 'lucide-vue-next'

const props = defineProps<{
  workflowId: string
  open: boolean
  welcomeMessage?: string
}>()

const emit = defineEmits<{ (e: 'close'): void }>()

interface ChatMsg {
  role: 'user' | 'assistant' | 'system'
  content: string
  ts: number
  error?: boolean
}

const messages = ref<ChatMsg[]>([])
const draft = ref('')
const sending = ref(false)
const sessionId = ref(newSessionId())

function newSessionId(): string {
  try {
    return `chat-${crypto.randomUUID()}`
  } catch {
    return `chat-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`
  }
}

function startNewConversation() {
  sessionId.value = newSessionId()
  messages.value = []
}

function extractReply(body: any): string {
  if (body == null) return ''
  if (typeof body === 'string') return body
  if (typeof body.reply === 'string') return body.reply
  if (typeof body.answer === 'string') return body.answer
  return JSON.stringify(body.output ?? body, null, 1)
}

async function send() {
  const text = draft.value.trim()
  if (!text || sending.value) return
  draft.value = ''
  messages.value.push({ role: 'user', content: text, ts: Date.now() })
  sending.value = true
  try {
    const { api } = useApi()
    const body = await api.post(`/chat/${props.workflowId}`, {
      message: text,
      session_id: sessionId.value,
    })
    messages.value.push({ role: 'assistant', content: extractReply(body) || '(empty reply)', ts: Date.now() })
  } catch (err: any) {
    const detail = err?.data?.detail || err?.message || 'chat request failed'
    messages.value.push({
      role: 'assistant',
      content: String(detail).includes('inactive')
        ? 'Workflow is inactive — activate it ("Triggers on" toggle) to chat.'
        : `⚠ ${detail}`,
      ts: Date.now(),
      error: true,
    })
  } finally {
    sending.value = false
    await nextTick()
    scrollToBottom()
  }
}

function scrollToBottom() {
  const el = document.getElementById('py8n-chat-scroll')
  if (el) el.scrollTop = el.scrollHeight
}

watch(() => props.open, async (open) => {
  if (open) await nextTick(scrollToBottom)
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
          <div v-if="sending" class="flex justify-start">
            <div class="flex items-center gap-2 rounded-xl rounded-bl-sm border border-zinc-800 bg-zinc-900 px-3 py-2 text-xs text-zinc-500">
              <Loader2 class="h-3 w-3 animate-spin" /> workflow is running…
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
          <div class="mt-1.5 flex items-center gap-1 px-0.5 text-[9px] text-zinc-600">
            <AlertTriangle v-if="false" class="h-2.5 w-2.5" />
            <span>one run per message · reply = last node output (or Respond to Webhook)</span>
          </div>
        </div>
      </div>
    </transition>
  </Teleport>
</template>
